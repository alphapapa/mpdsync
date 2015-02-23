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

def pad(number):
    return "{:.3f}".format(number)

class AveragedList(list):
    def __init__(self, data=None, length=None, name=None, printDebug=True):

        self.name = name

        # TODO: Isn't there a more Pythonic way to do this?
        if data:
            super(AveragedList, self).__init__(data)
            self._updateAverage()
        else:
            super(AveragedList, self).__init__()
            self.average = 0  # None might make more sense, but None doesn't work in abs()

            self.length = length
            self.printDebug = printDebug

    def insert(self, *args):
        super(AveragedList, self).insert(*args)

        if len(self) > self.length:
            self.pop()
        self._updateAverage()

        self.max = max(self)
        self.min = min(self)
        self.range = round(self.max - self.min, 3)

        if self.printDebug:
            log.debug('AveragedList %s:  average:%s  Max:%s  Min:%s  Range:%s  %s',
                      self.name, self._pad(round(self.average, 3)),
                      self._pad(self.max), self._pad(self.min), self._pad(self.range), self)

    def _pad(self, number):
        return "{:.3f}".format(number)

    def _updateAverage(self):
        self.average = sum(self) / len(self)

class Client(mpd.MPDClient):

    def __init__(self, host, port=DEFAULT_PORT, password=None, latency=0):
        super(Client, self).__init__()

        # Command timeout...should never take longer than a second, I
        # think...
        self.timeout = 10

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

        self.hasBeenSynced = False

        self.lastSong = None
        self.song = None

        self.consume = False
        self.random = False
        self.repeat = False
        self.single = False

        self.state = None
        self.playing = None
        self.paused = None

        self.duration = None
        self.elapsed = None

        self.pings = AveragedList(name='%s pings' % self.host, length=10, printDebug=False)

        self.elapsedDifferences = AveragedList(name='elapsedDifferences', length=20)  # 5 is not quite enough to smooth it out
        self.excessiveDifferences = AveragedList(name='excessiveDifferences', length=5)
        self.initialPlayTimes = AveragedList(name='initialPlayTimes', length=5)

        self.currentSongAdjustments = []
        self.currentSongDifferences = AveragedList(name='currentSongDifferences', length=20)
        self.adjustments = AveragedList(name='adjustments', length=20)

        self.playedSinceLastPlaylistUpdate = False

        self.reSeekedTimes = 0

    def ping(self):
        self.pings.insert(0, timeFunction(super(Client, self).ping))

    def checkConnection(self):

        # I don't know why this is necessary, but for some reason the slave connections tend to get dropped.
        try:
            self.ping()
        except Exception as e:
            log.debug('Connection to "%s" seems to be down.  Trying to reconnect...' % self.host)

            # Try to reconnect
            self.disconnect()  # Maybe this will help it reconnect next time around...
            try:
                self.connect()
            except Exception as e:
                log.critical('Unable to reconnect to "%s"' % self.host)

                return False
            else:
                log.debug('Reconnected to "%s"' % self.host)
                return True
        else:
            log.debug("Connection still up to %s", self.host)
            return True

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
                adjustBy = self.pings.average

            log.debug('Adjusting initial play by %s seconds' % adjustBy)

            self.status()

            # Execute in command list
            try:
                self.command_list_ok_begin()
            except mpd.CommandListError as e:
                # Server was already in a command list; probably a lost client connection, so try again
                log.debug("mpd.CommandListError: %s" % e.message)
                self.command_list_end()
                self.command_list_ok_begin()

            if round(adjustBy, 3) > 0:  # Only if difference is > 1 ms
                tries = 0

                # Wait for the server to...catch up?
                while self.elapsed is None and tries < 10:  # 2 seconds worth
                    time.sleep(0.2)
                    self.status()
                    log.debug(self.song)
                    tries += 1

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

            self.duration = round(float(self.currentStatus['duration']), 3) if 'duration' in self.currentStatus else None
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
            self.ping()
            time.sleep(0.05)

        self.maxDifference = self.pings.average * 5

        log.debug('Average ping for %s: %s seconds' % (self.host, self.pings.average))

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

                try:
                    slave.seek(self.song, self.elapsed)  # Seek to current master position before playing
                except Exception as e:
                    log.error("Couldn't seek slave %s:", slave.host, e)
                    return False

                if not slave.play(initial=True):
                    log.critical("Couldn't play slave: %s", slave.host)
                    return False

                # Wait a moment and then check the difference
                time.sleep(0.2)
                playLatency = self.compareElapsed(self, slave)

                if not playLatency:
                    # This probably means the slave isn't playing at all for some reason
                    log.error('No playLatency for slave "%s"', slave.host)

                    slave.stop()  # Maybe...?

                    return False

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
            log.debug("Adjusting %s by slave.latency: %s", slave.host, slave.latency)
            adjustBy = slave.latency

        elif len(slave.currentSongAdjustments) < 1 or len(slave.currentSongDifferences) < 3:
            # Adjusting a song for the first time or not enough measurements

            if len(slave.currentSongAdjustments) < 1:
                log.debug("First adjustment for song on slave %s", slave.host)
            elif len(slave.currentSongAdjustments) < 5:
                log.debug("Less than 5 adjustments made to song on slave %s", slave.host)

            if len(slave.adjustments) < 5:
                # Less than 5 adjustments made to slave in total; use averagePing
                adjustBy = slave.pings.average
                log.debug("Less than 5 total adjustments to slave %s; adjusting by average ping: %s", slave.host, adjustBy)
            else:
                # Adjust by average adjustment Slightly smaller than
                # the average difference to keep it from ballooning
                # and swinging back and forth
                adjustBy = slave.adjustments.average
                log.debug("Adjusting %s by average adjustment: %s", slave.host, adjustBy)

        else:
            # Not the first adjustment to the song
            adjustBy = (slave.currentSongDifferences.average * 0.5) * -1
            log.debug("Adjusting %s by currentSongDifferences.average: %s", slave.host, adjustBy)

            # if abs(adjustBy) < 0.1:
            #     log.debug('adjustBy was < 0.1 (%s); setting to 0' % adjustBy)
            #     adjustBy = 0

        # Sometimes the adjustment goes haywire.  If it's greater than 1% of the song duration, reset
        if slave.duration and adjustBy > slave.duration * 0.01:
            log.debug('Adjustment to %s too large:%s  Resetting to average ping:%s', slave.host, adjustBy, slave.pings.average)
            adjustBy = slave.pings.average

        # The amount of time that the slave lags behind the server seems to consistently vary by song or filetype!
        #adjustBy = -0.2  # Just use 0

        #log.debug("Adjusting by: %s" % adjustBy)


        self.status()

        adjustTo = round(self.elapsed - adjustBy, 3)
        if adjustTo < 0:
            log.debug("adjustTo for %s was < 0 (%s); skipping adjustment", slave.host, adjustTo)
            return

        log.debug('Master elapsed:%s  Adjusting %s to:%s (song: %s)' % (self.elapsed, slave.host, adjustTo, self.song))

        # For some reason this is getting weird errors like
        # "mpd.ProtocolError: Got unexpected return value: 'volume:
        # 0'", and I don't want the script to quit, so this way it
        # should try again
        try:
            # Seek to current playing position, adjusted for latency
            slave.seek(self.song, adjustTo)
        except Exception as e:
            log.error("Unable to seek slave %s: %s", slave.host, e)

            # Try to redo the whole slave
            slave.playlistVersion = None
            self.syncPlaylists()

            # Clear song adjustments to prevent wild jittering after seek timeouts
            slave.currentSongAdjustments = []
            slave.checkConnection()

        else:
            slave.adjustments.insert(0, adjustBy)
            slave.currentSongAdjustments.insert(0, adjustBy)

            # Reset song differences (maybe this or just cutting it in
            # half will help prevent too many consecutive adjustments
            while len(slave.currentSongDifferences) > 0:
                slave.currentSongDifferences.pop()

            self.compareElapsed(self, slave)

    def syncPlaylists(self):

        # Get master info
        self.status()
        self.getPlaylist()

        # Sync slaves
        for slave in self.slaves:

            # Reconnect if necessary (slave connections tend to drop for
            # some reason)
            if not slave.checkConnection():
                # If it can't reconnect...we can't sync the playlists,
                # or it will raise an exception
                raise Exception

            if not slave.hasBeenSynced:
                # Do a full sync the first time

                # Start command list
                slave.command_list_ok_begin()

                # Clear playlist
                slave.clear()

                # Add tracks
                for song in self.playlist:
                    slave.add(FILE_PREFIX_RE.sub('', song))

                # Execute command list
                result = slave.command_list_end()

                if not result:
                    log.critical("Couldn't add tracks to playlist on slave: %s", slave.host)
                    continue
                else:
                    log.debug("Added to playlist on slave %s, result:", slave.host, result)

                    slave.hasBeenSynced = True

            else:
                # Sync playlist changes
                changes = self.plchanges(slave.playlistVersion)

                # Start command list
                slave.command_list_ok_begin()

                for change in changes:
                    log.debug("Making change: %s" % change)

                    # Add new tracks
                    log.debug('Adding to slave:"%s" file:"%s" at pos:%s' % (slave.host, change['file'], change['pos']))
                    slave.addid(change['file'], change['pos'])  # Save the song ID from the slave

                # Execute command list
                try:
                    results = slave.command_list_end()
                except mpd.ProtocolError as e:
                    if e.message == "Got unexpected 'OK'":
                        log.error("mpd.ProtocolError: Got unexpected 'OK'")
                        continue  # Maybe it will work next time around...

                # Add tags for remote tracks with tags in playlist
                slave.command_list_ok_begin()
                for num, change in enumerate(changes):
                    if 'http' in change['file']:
                        for tag in ['artist', 'album', 'title', 'genre']:
                            if tag in change:

                                # results is a list of song IDs that were added; it corresponds to changes
                                slave.addtagid(int(results[num]), tag, change[tag])
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
            # Wrap entire loop in a try/except so the thread won't
            # die, I hope


            if self.playing:

                # Check each slave's average difference
                for slave in self.slaves:

                    if len(slave.currentSongDifferences) >= 5:
                        # At least 5 measurements for current
                        # song

                        if len(slave.currentSongDifferences) >= 10:
                            # 10 or more; use one-quarter of the range
                            maxDifference = (slave.currentSongDifferences.range / 4)
                        else:
                            # 5-9; use one-half of the range
                            maxDifference = (slave.currentSongDifferences.range / 2)

                        if maxDifference < slave.currentSongDifferences.max / 2:
                            # At least half the max to prevent the
                            # occasional small range from causing
                            # unnecessary syncs
                            maxDifference = slave.currentSongDifferences.max / 2

                    elif slave.pings.average:
                        # Use average ping
                        maxDifference = (slave.pings.average * float(10))

                    else:
                        # This shouldn't happen, but if it does,
                        # use 0.1 until we have something better
                        maxDifference = 0.1

                    log.debug("maxDifference for slave %s: %s", slave.host, pad(round(maxDifference, 3)))

                    # Compare the master and slave's elapsed time
                    self.compareElapsed(masterTester, slave)

                    # Adjust if difference is too big
                    if abs(slave.currentSongDifferences.average) > maxDifference:
                        log.info('Resyncing player to minimize difference for slave %s' % slave.host)

                        try:
                            # Use the masterTester object because
                            # the main master object is in idle
                            masterTester.reSeekPlayer(slave)
                            slave.reSeekedTimes += 1

                            log.debug("Client %s now reseeked %s times" % (slave.host, slave.reSeekedTimes))

                        except Exception as e:
                            log.error("Got exception: %s: %s" % (e, e.message))  # Sigh...

                            try:
                                slave.checkConnection()
                            except:
                                log.error("Couldn't reconnect to slave %s", slave.host)


            # Sleep...
            time.sleep(2)

    def compareElapsed(self, master, slave):
        # Wrap entire function in a try/except so it won't make the
        # script fail if python-mpd gets out of sync with command
        # results

        try:
            masterPing = round(timeFunction(master.ping), 3)
            slavePing = round(timeFunction(slave.ping), 3)

            ping = abs(masterPing - slavePing)
            log.debug("Last ping time: %s" % (ping))

            # Threaded
            # Thread(target=master.status).start()
            # Thread(target=slave.status).start()
            # time.sleep(master.averagePing * 2)

            master.status()

            slaveStatusLatency = round(timeFunction(slave.status), 3)
            log.debug("slaveStatusLatency:%s", slaveStatusLatency)

            # If song changed, reset differences
            if slave.lastSong != slave.song:
                log.debug("Song changed (%s -> %s); resetting %s.currentSongDifferences", slave.lastSong, slave.song, slave.host)

                slave.currentSongAdjustments = []
                slave.currentSongDifferences = AveragedList(name='%s currentSongDifferences' % slave.host, length=20)
                slave.lastSong = slave.song

            if slave.elapsed:

                # Seems like it makes sense to add the slaveStatusLatency,
                # but I seem to be observing that the opposite is the
                # case...
                difference = round(master.elapsed - slave.elapsed + slaveStatusLatency, 3)

                # If difference is too big, discard it, probably I/O
                # latency on the other end or something
                # if (len(slave.currentSongDifferences) > 1 and
                #     (slave.currentSongDifferences.average != 0) and
                #     (abs(difference) > (abs(slave.currentSongDifferences.average) * 100))):

                #     log.debug("Difference too great, discarding: %s  Average:%s" % (difference, slave.currentSongDifferences.average))
                #     return 0

                slave.currentSongDifferences.insert(0, difference)

                log.debug('Master elapsed:%s  %s elapsed:%s  Difference:%s',
                          master.elapsed, slave.host, slave.elapsed,
                          difference)

                return difference

        except Exception as e:
            log.error("compareElapsed() caught exception: %s", e)
            return False

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
