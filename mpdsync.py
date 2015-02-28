#!/usr/bin/env python

# mpdsync.py
# Originally written by Nick Pegg <https://github.com/nickpegg/mpdsync>
# Rewritten and updated to use python-mpd2 by Adam Porter <adam@alphapapa.net>

import argparse
from collections import defaultdict
import logging
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

    def __init__(self, data=None, length=None, name=None, printDebug=False):
        self.log = logging.getLogger(self.__class__.__name__)

        # TODO: Add weighted average.  Might be better than using the range.

        self.name = name
        self.length = length
        self.max = 0
        self.min = 0
        self.range = 0
        self.average = 0
        self.printDebug = printDebug

        # TODO: Isn't there a more Pythonic way to do this?
        if data:
            super(AveragedList, self).__init__(data)
            self._updateStats()
        else:
            super(AveragedList, self).__init__()

    def _reprstr(self):
        return 'name:%s  average:%s  range:%s  max:%s  min:%s' % (
            self.name,
            pad(round(self.average, 3)), pad(round(self.range, 3)),
            pad(round(self.max, 3)), pad(round(self.min, 3)))

    def __repr__(self):
        return self._reprstr()

    def __str__(self):
        return self._reprstr()

    def append(self, *args):
        super(AveragedList, self).append(*args)
        self._updateStats()

    def clear(self):
        while len(self) > 0:
            self.pop()

    def extend(self, *args):
        super(AveragedList, self).extend(*args)
        self._updateStats()

    def insert(self, *args):
        super(AveragedList, self).insert(*args)

        if len(self) > self.length:
            self.pop()
        self._updateStats()

    def _pad(self, number):
        return "{:.3f}".format(number)

    def _updateStats(self):
        self.average = sum(self) / len(self)
        self.max = max(self)
        self.min = min(self)
        self.range = round(self.max - self.min, 3)

        if self.printDebug:
            self.log.debug(self)

class Client(mpd.MPDClient):

    def __init__(self, host, port=DEFAULT_PORT, password=None, latency=0):
        self.log = logging.getLogger(self.__class__.__name__)

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

        self.syncLoopLocked = False
        self.hasBeenSynced = False

        self.lastSong = None
        self.song = None
        self.currentSongFiletype = None
        self.currentSongAdjustments = 0
        self.currentSongDifferences = AveragedList(name='currentSongDifferences', length=20)

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

        self.adjustments = AveragedList(name='adjustments', length=20)
        self.initialPlayTimes = AveragedList(name='%s.initialPlayTimes' % self.host, length=20)

        # Record adjustments by file type to see if there's a pattern
        self.fileTypeAdjustments = defaultdict(AveragedList)

        self.playedSinceLastPlaylistUpdate = False

        self.reSeekedTimes = 0

    def ping(self):
        self.pings.insert(0, timeFunction(super(Client, self).ping))

    def checkConnection(self):

        # I don't know why this is necessary, but for some reason the slave connections tend to get dropped.
        try:
            self.ping()
        except Exception as e:
            self.log.debug('Connection to "%s" seems to be down.  Trying to reconnect...' % self.host)

            # Try to reconnect
            try:
                self.disconnect()  # Maybe this will help it reconnect next time around...
            except Exception as e:
                self.log.debug("Couldn't DISconnect from client %s.  SIGH: %s", self.host, e)
            try:
                self.connect()
            except Exception as e:
                self.log.critical('Unable to reconnect to "%s"' % self.host)

                return False
            else:
                self.log.debug('Reconnected to "%s"' % self.host)
                return True
        else:
            self.log.debug("Connection still up to %s", self.host)
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
                self.log.debug('INITIAL PLAY: initial=True')
            else:
                self.log.debug('INITIAL PLAY: self.playedSinceLastPlaylistUpdate == False')

            # Adjust for average play latency
            if self.latency:
                adjustBy = self.latency
            elif self.initialPlayTimes.average:
                self.log.debug("Using play time average")
                adjustBy = self.initialPlayTimes.average
            else:
                self.log.debug("Using average ping; average is: %s" % self.initialPlayTimes.average)
                adjustBy = self.pings.average

            self.log.debug('Adjusting initial play by %s seconds' % adjustBy)

            self.status()

            # Execute in command list
            try:
                self.command_list_ok_begin()
            except mpd.CommandListError as e:
                # Server was already in a command list; probably a lost client connection, so try again
                self.log.debug("mpd.CommandListError: %s" % e.message)
                self.command_list_end()
                self.command_list_ok_begin()

            if round(adjustBy, 3) > 0:  # Only if difference is > 1 ms
                tries = 0

                # Wait for the server to...catch up?
                while self.elapsed is None and tries < 10:  # 2 seconds worth
                    time.sleep(0.2)
                    self.status()
                    self.log.debug(self.song)
                    tries += 1

                self.seek(self.song, self.elapsed + round(adjustBy, 3))
            super(Client, self).play()
            self.command_list_end()

        else:
            super(Client, self).play()

        self.playedSinceLastPlaylistUpdate = True

    def seek(self, song, elapsed):
        '''Wrapper to update song and elapsed when seek() is called.'''
        self.log.debug('Client.seek()')

        self.song = song
        self.elapsed = elapsed
        super(Client, self).seek(self.song, self.elapsed)

    def status(self):
        self.currentStatus = super(Client, self).status()

        # Wrap whole thing in try/except because of MPD protocol errors...sigh
        try:

            # Not sure why, but sometimes this ends up as None when the track or playlist is changed...?
            if self.currentStatus:
                self.playlistLength = int(self.currentStatus['playlistlength'])

                self.song = self.currentStatus['song'] if 'song' in self.currentStatus else None

                if self.playlist:
                    self.currentSongFiletype = self.playlist[int(self.song)].split('.')[-1]
                    self.log.debug('Current filetype: %s', self.currentSongFiletype)

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

        except Exception as e:
            self.log.error("Unable to get status for client %s: %s", self.host, e)
            self.checkConnection()

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

        self.log.debug('Average ping for %s: %s seconds' % (self.host, self.pings.average))

class Master(Client):
    def __init__(self, host, password=None, latencyAdjust=False):
        self.log = logging.getLogger(self.__class__.__name__)

        super(Master, self).__init__(host, password=password)

        self.slaves = []
        self.slaveDifferences = AveragedList(name='slaveDifferences', length=10, printDebug=False)
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
            self.log.error('Unable to connect to slave: %s:%s: %s', slave.host, slave.port, e)
        else:
            self.log.debug('Connected to slave: %s' % slave.host)

            self.slaves.append(slave)
            slave.status()  # Get initial status (this is not automatic upon connection)

    def syncAll(self):
        self.syncPlaylists()
        self.syncOptions()
        self.syncPlayers()

        if self.latencyAdjust:
            self.log.debug('Starting elapsedLoop')
            self.elapsedLoop = Thread(target=self.runElapsedLoop)
            self.elapsedLoop.daemon = True
            self.elapsedLoop.start()

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

                # Compare playlists; don't clear if they're the same
                slave.getPlaylist()
                if slave.playlist != self.playlist:
                    # Playlists differ
                    self.log.debug("Playlist differs on slave %s; syncing...", slave.host)

                    #Start command list
                    slave.command_list_ok_begin()

                    # Clear playlist
                    slave.clear()

                    # Add tracks
                    for song in self.playlist:
                        slave.add(FILE_PREFIX_RE.sub('', song))

                    # Execute command list
                    result = slave.command_list_end()

                    if not result:
                        self.log.critical("Couldn't add tracks to playlist on slave: %s", slave.host)
                        continue
                    else:
                        self.log.debug("Added to playlist on slave %s, result: %s", slave.host, result)

                        slave.hasBeenSynced = True

                else:
                    # Playlists are the same
                    slave.hasBeenSynced = True

                    self.log.debug("Playlist is the same on slave %s", slave.host)


            else:
                # Sync playlist changes
                # TODO: if slave.playlistVersion is None, handle it
                changes = self.plchanges(slave.playlistVersion)

                # Start command list
                slave.command_list_ok_begin()

                for change in changes:
                    self.log.debug("Making change: %s" % change)

                    # Add new tracks
                    self.log.debug('Adding to slave:"%s" file:"%s" at pos:%s' % (slave.host, change['file'], change['pos']))
                    slave.addid(change['file'], change['pos'])  # Save the song ID from the slave

                # Execute command list
                try:
                    results = slave.command_list_end()
                except mpd.ProtocolError as e:
                    if e.message == "Got unexpected 'OK'":
                        self.log.error("mpd.ProtocolError: Got unexpected 'OK'")
                        continue  # Maybe it will work next time around...

                if not results:
                    # This should not happen.  SIGH.
                    self.log.error("SIGH.")
                    continue

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
                        self.log.error("mpd.ProtocolError: Got unexpected 'OK'")

                # Update slave info
                slave.status()

                # Truncate the slave playlist to the same length as the master
                if self.playlistLength == 0:
                    slave.clear()
                elif self.playlistLength < slave.playlistLength:
                    self.log.debug("Deleting from %s to %s" % (self.playlistLength - 1, slave.playlistLength - 1))
                    slave.delete((self.playlistLength - 1, slave.playlistLength - 1))

                # Check result
                slave.status()
                if slave.playlistLength != self.playlistLength:
                    self.log.error("Playlist lengths don't match: %s / %s" % (slave.playlistLength, self.playlistLength))

                # Make sure the slave's playing status still matches the
                # master (for some reason, deleting a track lower-numbered
                # than the currently playing track makes the slaves stop
                # playing)
                if slave.state != self.state:
                    self.syncPlayer(slave)

            # Update slave playlist version number to match the master's
            slave.playlistVersion = self.playlistVersion
            self.playedSinceLastPlaylistUpdate = False

    def syncOptions(self):
        pass

    def syncPlayers(self):

        # SOMEDAY: do this in parallel?
        for slave in self.slaves:
            self.syncPlayer(slave)

    def syncPlayer(self, slave):

        tries = 0
        while tries < 5:

            try:
                # Update master info
                self.status()

                # Reconnect if connection dropped
                slave.checkConnection()

                # Sync player status
                if self.playing:

                    if slave.playing and slave.song == self.song:
                        self.log.debug('Playing slave %s, master already playing same song' % slave.host)

                        # BUG: If -l is not set, then this won't sync the playing position.

                    else:
                        # Slave not playing, or playing a different song
                        self.log.debug('Playing slave %s, initial=True' % slave.host)

                        try:
                            slave.seek(self.song, self.elapsed)  # Seek to current master position before playing
                        except Exception as e:
                            self.log.error("Couldn't seek slave %s:", slave.host, e)
                            return False

                        if not slave.play(initial=True):
                            self.log.critical("Couldn't play slave: %s", slave.host)
                            return False

                        # Wait a moment and then check the difference
                        time.sleep(0.2)
                        playLatency = self.compareElapsed(self, slave)

                        if not playLatency:
                            # This probably means the slave isn't playing at all for some reason
                            self.log.error('No playLatency for slave "%s"', slave.host)

                            slave.stop()  # Maybe...?

                            return False

                        self.log.debug('Client %s took %s seconds to start playing' % (slave.host, playLatency))

                        # Update initial play times
                        slave.initialPlayTimes.insert(0, playLatency)

                        self.log.debug('Average initial play time for client %s: %s seconds'
                                  % (slave.host, slave.initialPlayTimes.average))

                elif self.paused:
                    slave.pause()
                else:
                    slave.stop()

            except Exception as e:
                self.log.error("Unable to syncPlayer.  Tries:%s", tries)
                tries += 1
            else:
                return True

    def reSeekPlayer(self, slave):
        # Update master info
        self.status()

        # Reconnect if connection dropped
        slave.checkConnection()

        # Choose adjustment
        if slave.latency:
            # Use the user-set adjustment
            self.log.debug("Adjusting %s by slave.latency: %s", slave.host, slave.latency)

            adjustBy = slave.latency

        else:
            # Calculate adjustment automatically

            if slave.currentSongAdjustments < 1:
                # First adjustment for song
                self.log.debug("First adjustment for song on slave %s", slave.host)

                if len(slave.adjustments) > 5:
                    # Adjust by average adjustment slightly reduced to
                    # avoid swinging back and forth
                    adjustBy = slave.adjustments.average * 0.75

                    self.log.debug("Adjusting %s by average adjustment: %s", slave.host, adjustBy)
                else:
                    # Adjust by average ping
                    adjustBy = slave.pings.average

                    self.log.debug("Adjusting %s by average ping: %s", slave.host, adjustBy)
            else:
                # Not the first adjustment for song
                self.log.debug("Not first adjustment for song on slave %s", slave.host)

                if len(slave.currentSongDifferences) < 5:
                    # Not enough measurements for this song
                    self.log.debug("Not enough measurements for song on slave %s; adjusting by average ping", slave.host)

                    adjustBy = slave.pings.average

                else:
                    # Enough measurements for song
                    self.log.debug("%s measurements for song on slave %s", len(slave.currentSongDifferences), slave.host)

                    if slave.currentSongAdjustments > 10:
                        # Too many adjustments for this song.  Try average
                        # ping to settle back down.  Some songs just don't
                        # seek reliably or something.
                        self.log.debug("Too many adjustments for song on slave %s; adjusting by average ping", slave.host)

                        adjustBy = slave.pings.average

                        # Reduce number of currentSongAdjustments to
                        # give it a chance to use song adjustments
                        # again instead of reusing average ping until
                        # the song ends; but don't reset it
                        # completely, because if using the average
                        # song adjustment isn't working, it might be
                        # better to use the ping sooner
                        slave.currentSongAdjustments = 5

                    else:
                        # <10 adjustments for song

                        # Adjust by less than the average difference to gradually
                        # hone in on the right adjustment.  0.5 is ok for local
                        # files, but too small for remote ones; trying 0.75
                        adjustBy = (slave.currentSongDifferences.average * 0.75) * -1

                        self.log.debug("Adjusting %s by currentSongDifferences.average: %s", slave.host, adjustBy)

            # Sometimes the adjustment goes haywire.  If it's greater than 1% of the song duration, reset
            if ((slave.duration and adjustBy > slave.duration * 0.01)
                or adjustBy > 1):
                self.log.debug('Adjustment to %s too large:%s  Adjusting by average ping:%s',
                               slave.host, adjustBy, slave.pings.average)
                adjustBy = slave.pings.average

            #  Not sure if I should update the master's position
            #  again; that might require redoing the calculations.
            #  Commenting out for now: self.status()

            # Calculate position
            position = round(self.elapsed - adjustBy, 3)
            if position < 0:
                self.log.debug("Position for %s was < 0 (%s); skipping adjustment", slave.host, position)
                return

            self.log.debug('Master elapsed:%s  Adjusting %s to:%s (song: %s)',
                           self.elapsed, slave.host, position, self.song)

        # For some reason this is getting weird errors like
        # "mpd.ProtocolError: Got unexpected return value: 'volume:
        # 0'", and I don't want the script to quit, so wrapping it in
        # a try/except should help it try again
        try:
            # Try to seek to current playing position, adjusted for latency
            slave.seek(self.song, position)

        except Exception as e:
            # Seek failed
            self.log.error("Unable to seek slave %s: %s", slave.host, e)

            # Try to completely resync the slave
            slave.playlistVersion = None
            self.syncPlaylists()

            # Clear song adjustments to prevent wild jittering after seek timeouts
            slave.currentSongAdjustments = 0
            slave.checkConnection()

        else:
            # Seek succeeded
            slave.currentSongAdjustments += 1

            # Don't record adjustment if it's just the average ping
            if adjustBy != slave.pings.average:
                slave.adjustments.insert(0, adjustBy)
                slave.fileTypeAdjustments[slave.currentSongFiletype].append(round(adjustBy, 3))
                self.log.debug("Adjustments for filetype %s: %s",
                               slave.currentSongFiletype, slave.fileTypeAdjustments[slave.currentSongFiletype])

            # Reset song differences (maybe this or just cutting it in
            # half will help prevent too many consecutive adjustments
            while len(slave.currentSongDifferences) > 5:
                slave.currentSongDifferences.pop()

            # I think I should have commented this out, doing so for
            # now: self.compareElapsed(self, slave)

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

                    # Don't run the loop if an earlier one is already
                    # waiting for a result (this might be helping to
                    # cause the MPD protocol errors)
                    if slave.syncLoopLocked:
                        self.log.debug("syncLoopLocked for slave %s", slave.host)
                        continue
                    else:
                        slave.syncLoopLocked = True

                    # Compare the master and slave's elapsed time
                    self.compareElapsed(masterTester, slave)

                    # Calculate maxDifference
                    if len(slave.currentSongDifferences) >= 5:
                        # At least 5 measurements for current
                        # song

                        if len(slave.currentSongDifferences) >= 10:
                            # 10 or more measurements; use one-quarter
                            # of the range
                            maxDifference = slave.currentSongDifferences.range / 4

                            # Add half the average to prevent it from
                            # being too small and causing excessive
                            # reseeks.  For some songs the range can
                            # be very small, like 38ms, but for others
                            # it can be consistently 100-200 ms.
                            maxDifference += (abs(slave.currentSongDifferences.average) / 2)
                        else:
                            # 5-9 measurements; use one-half of the
                            # range
                            maxDifference = slave.currentSongDifferences.range / 2

                        # Use at least half of the biggest difference
                        # to prevent the occasional small range from
                        # causing unnecessary syncs.  This may not be
                        # necessary with adding half the average a few
                        # lines up, but it may be a good extra
                        # precaution.
                        minimumMaxDifference = (max([abs(slave.currentSongDifferences.max),
                                                     abs(slave.currentSongDifferences.min)])
                                                * 0.5)
                        if maxDifference < minimumMaxDifference:
                            maxDifference = minimumMaxDifference

                    elif slave.pings.average:
                        # Use average ping

                        # Use the larger of master or slave ping so the script can run on either one
                        maxDifference = (max([slave.pings.average, masterTester.pings.average])
                                         * 30)

                        if maxDifference > 0.2:
                            # But don't go over 100 ms; if it's that
                            # high, better to just resync again

                            # But this does not work well for remote
                            # files.  The difference sometimes never
                            # gets lower than the maxDifference, so it
                            # resyncs forever, and always by the
                            # average ping or average slave
                            # adjustment, which basically loops doing
                            # the same syncs forever.  Sigh.
                            maxDifference = 0.2

                    else:
                        # This shouldn't happen, but if it does, use
                        # 0.2 until we have something better.  0.2
                        # seems like a lot, and for local files it is,
                        # but remote files don't seek as quickly or
                        # consistently, so 0.1 can cause excessive
                        # reseeking in a short period of time
                        maxDifference = 0.2

                    self.log.debug("maxDifference for slave %s: %s", slave.host, pad(round(maxDifference, 3)))

                    # Adjust if difference is too big
                    if abs(slave.currentSongDifferences.average) > maxDifference:
                        self.log.info('Resyncing player to minimize difference for slave %s' % slave.host)

                        try:
                            # Use the masterTester object because
                            # the main master object is in idle
                            masterTester.reSeekPlayer(slave)
                            slave.reSeekedTimes += 1

                            # Give it a moment to settle before looping again
                            time.sleep(1)

                            self.log.debug("Client %s now reseeked %s times" % (slave.host, slave.reSeekedTimes))

                        except Exception as e:
                            self.log.error("%s: %s", type(e), e)  # Sigh...
                            raise e

                            try:
                                slave.checkConnection()
                            except:
                                self.log.error("Couldn't reconnect to slave %s", slave.host)

                    slave.syncLoopLocked = False

                # Sleep for 400ms for each measurement, but at least 500ms
                sleepTime = 0.4 * len(slave.currentSongDifferences)
                if sleepTime < 0.5:
                    sleepTime = 0.5

                # Print comparison between two slaves
                if len(self.slaves) > 1:
                    self.slaveDifferences.insert(0, abs(self.slaves[0].currentSongDifferences.average \
                                                        - self.slaves[1].currentSongDifferences.average))
                    self.log.debug("Average difference between slaves 1 and 2: %s", pad(round(self.slaveDifferences.average, 3)))

            else:
                # Not playing
                sleepTime = 2

            # BUG: If there are multiple slaves, this sleeps for
            # whatever sleepTime was set to for the last slave
            self.log.debug('Sleeping for %s seconds', sleepTime)

            time.sleep(sleepTime)

    def compareElapsed(self, master, slave):
        # Wrap entire function in a try/except so it won't make the
        # script fail if python-mpd gets out of sync with command
        # results

        try:
            masterPing = round(timeFunction(master.ping), 3)
            slavePing = round(timeFunction(slave.ping), 3)

            ping = abs(masterPing - slavePing)
            self.log.debug("Last ping time: %s" % (ping))

            # Get master status and time how long it takes
            masterStatusLatency = round(timeFunction(master.status), 3)

            self.log.debug("masterStatusLatency:%s", masterStatusLatency)

            # Get slave status and time how long it takes
            slaveStatusLatency = round(timeFunction(slave.status), 3)

            self.log.debug("slaveStatusLatency:%s", slaveStatusLatency)

            # If song changed, reset differences
            if slave.lastSong != slave.song:
                self.log.debug("Song changed (%s -> %s); resetting %s.currentSongDifferences", slave.lastSong, slave.song, slave.host)

                slave.currentSongAdjustments = 0
                slave.currentSongDifferences = AveragedList(name='%s currentSongDifferences' % slave.host, length=20)
                slave.lastSong = slave.song

            if slave.elapsed:

                # Seems like it makes sense to add the slaveStatusLatency,
                # but I seem to be observing that the opposite is the
                # case...
                difference = round((master.elapsed + masterStatusLatency) - (slave.elapsed + slaveStatusLatency), 3)

                # If difference is too big, discard it, probably I/O
                # latency on the other end or something
                # if (len(slave.currentSongDifferences) > 1 and
                #     (slave.currentSongDifferences.average != 0) and
                #     (abs(difference) > (abs(slave.currentSongDifferences.average) * 100))):

                #     self.log.debug("Difference too great, discarding: %s  Average:%s" % (difference, slave.currentSongDifferences.average))
                #     return 0

                slave.currentSongDifferences.insert(0, difference)

                # "Difference" is aligned with the average below
                self.log.debug('Master/%s elapsed:%s/%s  Difference:%s',
                          slave.host, master.elapsed, slave.elapsed, difference)
                self.log.debug(slave.currentSongDifferences)

                return difference

        except Exception as e:
            self.log.error("compareElapsed() caught exception: %s", e)
            slave.checkConnection()  # Maybe this will help
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
                        help="Monitor latency between master and slaves and try to keep slaves' playing position in sync with the master's")
    parser.add_argument("-v", "--verbose", action="count", dest="verbose", help="Be verbose, up to -vv")
    args = parser.parse_args()

    # Setup logging
    if args.verbose == 1:
        LOG_LEVEL = logging.INFO
    elif args.verbose >=2:
        LOG_LEVEL = logging.DEBUG
    else:
        LOG_LEVEL = logging.WARNING

    logging.basicConfig(level=LOG_LEVEL, format="%(levelname)s: %(name)s: %(message)s")
    log = logging.getLogger()

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

    try:
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
    except KeyboardInterrupt:
        log.debug("Interrupted.  Filetype adjustments for slave 0: %s",
                  [{a: master.slaves[0].fileTypeAdjustments[a]}
                   for a in master.slaves[0].fileTypeAdjustments])

if __name__ == '__main__':
    sys.exit(main())
