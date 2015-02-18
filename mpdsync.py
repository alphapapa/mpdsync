#!/usr/bin/env python

# mpdsync.py
# Originally written by Nick Pegg <https://github.com/nickpegg/mpdsync>
# Rewritten and updated to use python-mpd2 by Adam Porter <adam@alphapapa.net>

import argparse
import logging as log
import os
import re
import sys
from threading import Thread
import time

import mpd  # Using python-mpd2

# Verify python-mpd2 is being used
if mpd.VERSION < (0, 5, 4):
    print 'ERROR: This script requires python-mpd2 >= 0.5.4.'
    sys.exit(1)

DEFAULT_PORT = 6600
FILE_PREFIX_RE = re.compile('^file: ')

class AveragedList(list):
    def __init__(self, data=None, length=None):

        # TODO: Isn't there a more Pythonic way to do this?
        if data:
            super(AveragedList, self).__init__(data)
            self.updateAverage()
        else:
            super(AveragedList, self).__init__()
            self.average = 0  # None might make more sense, but None doesn't work in abs()
        
        self.length = length

    def insert(self, *args):
        super(AveragedList, self).insert(*args)
        if len(self) > self.length:
            self.pop()
        self.updateAverage()
            
    def updateAverage(self):
        self.average = round(sum(self) / len(self), 3)
        
class Client(mpd.MPDClient):

    def __init__(self, host, port=DEFAULT_PORT, password=None, latency=0):
        super(Client, self).__init__()

        if '/' in host:
            host, latency = host.split('/')
            
        # Split host/port
        if ':' in host:
            host, port = host.split(':')

        self.host = host
        self.port = port
        self.password = password

        self.latency = float(latency)

        self.currentStatus = None
        self.playlist = None
        self.playlistLength = None
        self.playlistVersion = None

        self.song = None

        self.consume = False
        self.random = False
        self.repeat = False
        self.single = False

        self.state = None
        self.playing = None
        self.paused = None

        self.elapsed = None

        self.averagePing = None
        self.elapsedDifferences = AveragedList(length=10)  # 5 is not quite enough to smooth it out
        self.excessiveDifferences = AveragedList(length=5)
        self.initialPlayTimes = AveragedList(length=5)

        self.playedSinceLastPlaylistUpdate = False

        self.reSeekedTimes = 0

    def checkConnection(self):

        # I don't know why this is necessary, but for some reason the slave connections tend to get dropped.
        try:
            self.ping()
        except Exception as e:
            log.debug('Connection to "%s" seems to be down.  Trying to reconnect...' % self.host)

            # Try to reconnect
            try:
                self.connect()
            except Exception as e:
                log.debug('Unable to reconnect to "%s"' % self.host)
            else:
                log.debug('Reconnected to "%s"' % self.host)

    def connect(self):
        super(Client, self).connect(self.host, self.port)

        if self.password:
            super(Client, self).password(self.password)

        self.testPing()

    def getPlaylist(self):
        self.playlist = super(Client, self).playlist()

    def pause(self):
        super(Client, self).pause()
        self.playing = False
        self.paused = True
        
    def play(self, initial=False):
        if initial:  # or self.playedSinceLastPlaylistUpdate == False:
            if initial:
                log.debug('INITIAL PLAY: initial=True')
            else:
                log.debug('INITIAL PLAY: self.playedSinceLastPlaylistUpdate == False')

            # Adjust for average play latency
            if self.latency:
                adjustBy = self.latency
            elif self.initialPlayTimes.average:
                log.debug("Using play time average")
                adjustBy = self.initialPlayTimes.average
            else:
                log.debug("Using average ping; average is: %s" % self.initialPlayTimes.average)
                adjustBy = self.averagePing

            log.debug('Adjusting initial play by %s seconds' % adjustBy)

            # Execute in command list
            try:
                self.command_list_ok_begin()
            except mpd.CommandListError as e:
                # Server was already in a command list; probably a lost client connection, so try again
                log.debug("mpd.CommandListError: %s" % e.message)
                self.command_list_end()
                self.command_list_ok_begin()
                
            if round(adjustBy, 3) > 0:  # Only if difference is > 1 ms
                log.debug('SEEKING')
                self.status()
                print "ELAPSED", self.elapsed 
                self.seek(self.song, self.elapsed + round(adjustBy, 3))
            super(Client, self).play()
            self.command_list_end()

        else:
            super(Client, self).play()

        self.playedSinceLastPlaylistUpdate = True    

    def seek(self, song, elapsed):
        '''Wrapper to update song and elapsed when seek() is called.'''
        log.debug('Client.seek()')

        self.song = song
        self.elapsed = elapsed
        super(Client, self).seek(self.song, self.elapsed)
        
    def status(self):
        self.currentStatus = super(Client, self).status()

        # Not sure why, but sometimes this ends up as None when the track or playlist is changed...?
        if self.currentStatus:
            self.playlistLength = int(self.currentStatus['playlistlength'])

            self.song = self.currentStatus['song'] if 'song' in self.currentStatus else None

            self.consume = True if self.currentStatus['consume'] == '1' else False
            self.random = True if self.currentStatus['random'] == '1' else False
            self.repeat = True if self.currentStatus['repeat'] == '1' else False
            self.single = True if self.currentStatus['single'] == '1' else False

            self.state = self.currentStatus['state']
            self.playing = True if self.state == 'play' else False
            self.paused = True if self.state == 'pause' else False

            self.elapsed = round(float(self.currentStatus['elapsed']), 3) if 'elapsed' in self.currentStatus else None

        else:
            self.playlistLength = None

            self.song = None

            self.consume = False
            self.random = False
            self.repeat = False
            self.single = False

            self.state = None
            self.playing = False
            self.paused = False

            self.elapsed = None
            
        # TODO: Add other attributes, e.g. {'playlistlength': '55',
        # 'playlist': '3868', 'repeat': '0', 'consume': '0',
        # 'mixrampdb': '0.000000', 'random': '0', 'state': 'stop',
        # 'volume': '-1', 'single': '0'}

    def testPing(self):
        times = []
        for i in range(5):
            times.append(timeFunction(self.ping))
            time.sleep(0.05)

        self.averagePing = sum(times) / len(times)  # Store as seconds, not ms

        log.debug('Average ping for %s: %s seconds' % (self.host, self.averagePing))
        
class Master(Client):

    def __init__(self, host, password=None, latencyAdjust=False):
        super(Master, self).__init__(host, password=password)

        self.slaves = []
        self.elapsedLoopRunning = False
        self.latencyAdjust = latencyAdjust

    def status(self):
        super(Master, self).status()

        # Use the master's reported playlist version (for the slaves
        # we update it manually to match the master)
        self.playlistVersion = self.currentStatus['playlist']

    def addSlave(self, host, password=None):
        slave = Client(host, password=password)

        # Connect to slave
        try:
            slave.connect()
        except Exception as e:
            log.error('Unable to connect to slave: %s:%s' % (slave.host, slave.port))
        else:
            log.debug('Connected to slave: %s' % slave.host)

            self.slaves.append(slave)
            slave.status()  # Get initial status (this is not automatic upon connection)

    def syncAll(self):
        self.syncPlaylists()
        self.syncOptions()
        self.syncPlayers()

        if self.latencyAdjust:
            log.debug('Starting elapsedLoop')
            self.elapsedLoop = Thread(target=self.runElapsedLoop)
            self.elapsedLoop.daemon = True
            self.elapsedLoop.start()

    def syncOptions(self):
        pass

    def syncPlayers(self):

        # SOMEDAY: do this in parallel?
        for slave in self.slaves:
            self.syncPlayer(slave)

    def syncPlayer(self, slave):

        # Update master info
        self.status()

        # Reconnect if connection dropped
        slave.checkConnection()

        # Sync player status
        if self.playing:

            if slave.playing:
                log.debug('Playing slave %s, master already playing' % slave.host)

                self.reSeekPlayer(slave)

            else:
                log.debug('Playing slave %s, initial=True' % slave.host)
                slave.seek(self.song, self.elapsed)  # Seek to current master position before playing
                slave.play(initial=True)

                # Wait a moment and then check the difference
                time.sleep(0.2)
                playLatency = self.compareElapsed(self, slave)

                log.debug('Client %s took %s seconds to start playing' % (slave.host, playLatency))

                # Update initial play times
                slave.initialPlayTimes.insert(0, playLatency)

                log.debug('Average initial play time for client %s: %s seconds'
                          % (slave.host, slave.initialPlayTimes.average))
            
        elif self.paused:
            slave.pause()
        else:
            slave.stop()

    def reSeekPlayer(self, slave):
        # Update master info
        self.status()

        # Reconnect if connection dropped
        slave.checkConnection()

        # Update master info again, to seek more closely to the master's current position
        if slave.latency:
            adjustBy = slave.latency
        else:
            adjustBy = slave.elapsedDifferences.average * -1
            if abs(adjustBy) < 0.1:
                log.debug('adjustBy was < 0.1 (%s); setting to 0' % adjustBy)
                adjustBy = 0

        adjustBy = 0  # Just use 0
        log.debug("Adjusting by: %s" % adjustBy)

        self.status()

        log.debug('Master elapsed:%s  Adjusting to:%s' % (self.elapsed, round(self.elapsed - adjustBy, 3)))

        slave.seek(self.song, round(self.elapsed - adjustBy, 3))  # Seek to current playing position, adjusted for latency

        # Reset averages
        slave.elapsedDifferences = AveragedList(length=10)

        self.compareElapsed(self, slave)

    def syncPlaylists(self):

        # Get master info
        self.status()
        self.getPlaylist()

        # Sync slaves
        for slave in self.slaves:

            # Reconnect if necessary (slave connections tend to drop for
            # some reason)
            slave.checkConnection()

            if slave.playlistVersion is None:
                # Do a full sync the first time

                # Start command list
                slave.command_list_ok_begin()

                # Clear playlist
                slave.clear()

                # Add tracks
                for song in self.playlist:
                    slave.add(FILE_PREFIX_RE.sub('', song))

                # Execute command list
                slave.command_list_end()

            else:
                # Sync playlist changes

                # Start command list
                slave.command_list_ok_begin()

                for change in self.plchanges(slave.playlistVersion):
                    log.debug("Making change: %s" % change)

                    # Add new tracks
                    log.debug('Adding to slave:"%s" file:"%s" at pos:%s' % (slave.host, change['file'], change['pos']))
                    slave.addid(change['file'], change['pos'])

                # Execute command list
                try:
                    slave.command_list_end()
                except mpd.ProtocolError as e:
                    if e.message == "Got unexpected 'OK'":
                        log.error("mpd.ProtocolError: Got unexpected 'OK'")

                # Update slave info
                slave.status()

                # Truncate the slave playlist to the same length as the master
                if self.playlistLength == 0:
                    slave.clear()
                elif self.playlistLength < slave.playlistLength:
                    log.debug("Deleting from %s to %s" % (self.playlistLength - 1, slave.playlistLength - 1))
                    slave.delete((self.playlistLength - 1, slave.playlistLength - 1))

                # Check result
                slave.status()
                if slave.playlistLength != self.playlistLength:
                    log.error("Playlist lengths don't match: %s / %s" % (slave.playlistLength, self.playlistLength))

                # Make sure the slave's playing status still matches the
                # master (for some reason, deleting a track lower-numbered
                # than the currently playing track makes the slaves stop
                # playing)
                if slave.state != self.state:
                    self.syncPlayer(slave)

            # Update slave playlist version number to match the master's
            slave.playlistVersion = self.playlistVersion
            self.playedSinceLastPlaylistUpdate = False

    def runElapsedLoop(self):
        if self.elapsedLoopRunning == True:
            return False
        
        self.elapsedLoopRunning = True

        # Make new connection to master server that won't idle()
        masterTester = Master(self.host)
        masterTester.connect()

        while True:
            
            if self.playing:

                # Check each slave's average difference
                for slave in self.slaves:
                    if slave.latency:
                        maxDifference = abs(slave.latency) * 2
                    else:
                        maxDifference = 0.05

                    maxDifference = 0.1  # Just use 0.1
                        
                    self.compareElapsed(masterTester, slave)

                    # Adjust if difference is too big
                    if abs(slave.elapsedDifferences.average) > maxDifference:  # 0.1 seems too big...but less may sync too much...

                        # Add to list of last 10 excessive differences
                        # (to help account for that the difference
                        # after starting a new track tends to be high,
                        # so if we could do a good adjustment when a
                        # track is first played, it might not need to
                        # be resynced)
                        slave.excessiveDifferences.insert(0, slave.elapsedDifferences.average)
                            
                        log.info('Resyncing player to minimize difference for slave %s' % slave.host)
                        try:
                            masterTester.reSeekPlayer(slave)  # Use the masterTester object because the main master object is in idle
                            slave.reSeekedTimes += 1
                            log.debug("Client %s now reseeked %s times" % (slave.host, slave.reSeekedTimes))
                        except Exception as e:
                            log.error("Got exception: %s: %s" % (e, e.message))

            # Sleep...
            time.sleep(1)

    def compareElapsed(self, master, slave):
        masterPing = round(timeFunction(master.ping), 3)
        slavePing = round(timeFunction(slave.ping), 3)

        ping = masterPing if masterPing > slavePing else slavePing
        log.debug("Last ping time: %s" % (ping))

        # Threaded
        # Thread(target=master.status).start()
        # Thread(target=slave.status).start()
        # time.sleep(master.averagePing * 2)

        master.status()
        slave.status()
        
        if slave.elapsed:
            difference = round(master.elapsed - slave.elapsed + ping, 3)

            # If difference is too big, discard it, probably I/O
            # latency on the other end or something
            if (len(slave.elapsedDifferences) > 1 and
                (slave.elapsedDifferences.average != 0) and
                (abs(difference) > (abs(slave.elapsedDifferences.average) * 100))):
                
                log.debug("Difference too great, discarding: %s  Average:%s" % (difference, slave.elapsedDifferences.average))
                return 0
    
            slave.elapsedDifferences.insert(0, difference)

            log.debug('Master elapsed:%s  Slave elapsed:%s  Difference:%s'
                      % (master.elapsed, slave.elapsed,
                         difference))

            log.debug("Average difference for slave %s: %s" % (slave.host, slave.elapsedDifferences.average))

            return difference
            
def timeFunction(f):
    t1 = time.time()
    f()
    t2 = time.time()
    return t2 - t1
            
def main():

    # Parse args
    parser = argparse.ArgumentParser(
            description='Syncs multiple mpd servers.')
    parser.add_argument('-m', '--master',
                        dest='master',
                        help='Name or address of master server, optionally with port in HOST:PORT format')
    parser.add_argument('-s', '--slaves',
                        dest="slaves", nargs='*',
                        help='Name or address of slave servers, optionally with port in HOST:PORT/LATENCY format')
    parser.add_argument('-p', '--password', default=None,
                        dest="password",
                        help='Password to connect to servers with')
    parser.add_argument('-l', '--latency-adjust',
                        dest="latencyAdjust", action="store_true",
                        help="Monitor latency between master and slaves and try to keep slaves' playing position in sync within 0.05 seconds")
    parser.add_argument("-v", "--verbose", action="count", dest="verbose", help="Be verbose, up to -vv")
    args = parser.parse_args()

    # Setup logging
    if args.verbose == 1:
        LOG_LEVEL = log.INFO
    elif args.verbose >=2:
        LOG_LEVEL = log.DEBUG
    else:
        LOG_LEVEL = log.WARNING
    log.basicConfig(level=LOG_LEVEL, format="%(levelname)s: %(message)s")

    log.debug('Using python-mpd version: %s' % str(mpd.VERSION))
    log.debug("Args: %s" % args)

    # Check args
    if not args.master:
        log.error("Please provide a master server with -m.")
        return False

    if not args.slaves:
        log.error("Please provide at least one slave server with -c.")
        return False

    # Connect to the master server
    master = Master(args.master, password=args.password, latencyAdjust=args.latencyAdjust)

    try:
        master.connect()
    except Exception as e:
        log.error('Unable to connect to master server: %s' % e)
        return False
    else:
        log.debug('Connected to master server.')

    # Connect to slaves
    for slave in args.slaves:
        master.addSlave(slave, password=args.password)

    # Make sure there is at least one slave connected
    if not master.slaves:
        log.error("Couldn't connect to any slaves.")
        return False

    # Sync master and slaves
    master.syncAll()

    # Enter sync loop
    while True:
        # Wait for something to happen
        subsystems = master.idle()

        # Sync stuff
        for subsystem in subsystems:
            if subsystem == 'playlist':
                log.debug("Subsystem update: playlist")
                master.syncPlaylists()
            elif subsystem == 'player':
                log.debug("Subsystem update: player")
                master.syncPlayers()
            elif subsystem == 'options':
                log.debug("Subsystem update: options")
                master.syncOptions()


if __name__ == '__main__':
    sys.exit(main())
