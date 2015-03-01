#!/usr/bin/env python

# mpdsync.py
# Originally written by Nick Pegg <https://github.com/nickpegg/mpdsync>
# Rewritten and updated to use python-mpd2 by Adam Porter <adam@alphapapa.net>

import argparse
from collections import defaultdict
import logging
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

class myFloat(float):
    '''Rounds and pads to 3 decimal places when printing.'''

    def __init__(self, num, roundBy=3):
        super(myFloat, self).__init__(num)
        self.roundBy = roundBy

    def __abs__(self):
        '''Override this so it will return another myFloat, which will print
        rounded and padded.'''

        return myFloat(float.__abs__(self))

    def __str__(self):
        return "{:.3f}".format(round(self, self.roundBy))

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

    def __str__(self):
        return 'name:%s average:%s range:%s max:%s min:%s' % (
            self.name, self.average, self.range, self.max, self.min)

    __repr__ = __str__

    def append(self, arg):
        arg = myFloat(arg)
        super(AveragedList, self).append(arg)
        self._updateStats()

    def clear(self):
        while len(self) > 0:
            self.pop()

    def extend(self, *args):
        args = [[myFloat(a) for l in args for a in l]]
        super(AveragedList, self).extend(*args)
        self._updateStats()

    def insert(self, pos, *args):
        args = [myFloat(a) for a in args]
        super(AveragedList, self).insert(pos, *args)

        while len(self) > self.length:
            self.pop()
        self._updateStats()

    def _updateStats(self):
        self.average = myFloat(sum(self) / len(self))
        self.max = myFloat(max(self))
        self.min = myFloat(min(self))
        self.range = myFloat(self.max - self.min)

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

        for attr in ['currentStatus', 'lastSong', 'song',
                     'currentSongFiletype', 'playlist',
                     'playlistLength', 'playlistVersion',
                     'hasBeenSynced', 'state', 'playing', 'paused',
                     'duration', 'elapsed' 'consume', 'random',
                     'repeat', 'single']:
            setattr(self, attr, None)

        self.syncLoopLocked = False
        self.hasBeenSynced = False
        self.playedSinceLastPlaylistUpdate = False

        self.currentSongShouldSeek = True
        self.currentSongAdjustments = 0
        self.currentSongDifferences = AveragedList(name='currentSongDifferences', length=10)

        self.pings = AveragedList(name='%s.pings' % self.host, length=10)

        self.adjustments = AveragedList(name='%sadjustments' % self.host, length=20)
        self.initialPlayTimes = AveragedList(name='%s.initialPlayTimes' % self.host, length=20, printDebug=True)
        self.reSeekedTimes = 0

        # Record adjustments by file type to see if there's a pattern
        self.fileTypeAdjustments = defaultdict(AveragedList)

        # TODO: Record each song's number of adjustments in a list (by
        # filename), and print on exit.  This way I can play a short
        # playlist in a loop and see if there is a pattern with
        # certain songs being consistently bad at syncing and seeking.

    def ping(self):
        '''Pings the daemon and records how long it took.'''

        self.pings.insert(0, timeFunction(super(Client, self).ping))

    def checkConnection(self):
        '''Pings the daemon and tries to reconnect if necessary.'''

        # I don't know why this is necessary, but for some reason the slave connections tend to get dropped.
        try:
            self.ping()

        except Exception as e:
            self.log.debug('Connection to "%s" seems to be down.  Trying to reconnect...', self.host)

            # Try to disconnect first
            try:
                self.disconnect()  # Maybe this will help it reconnect next time around...
            except Exception as e:
                self.log.debug("Couldn't DISconnect from client %s.  SIGH: %s", self.host, e)

            # Try to reconnect
            try:
                self.connect()
            except Exception as e:
                self.log.critical('Unable to reconnect to "%s"', self.host)

                return False
            else:
                self.log.debug('Reconnected to "%s"', self.host)

                return True

        else:
            self.log.debug("Connection still up to %s", self.host)

            return True

    def connect(self):
        '''Connects to the daemon, sets the password if necessary, and tests
        the ping time.'''

        super(Client, self).connect(self.host, self.port)

        if self.password:
            super(Client, self).password(self.password)

        self.testPing()

    def getPlaylist(self):
        '''Gets the playlist from the daemon.'''

        self.playlist = super(Client, self).playlist()

    def pause(self):
        '''Pauses the daemon and tracks the playing state.'''

        super(Client, self).pause()
        self.playing = False
        self.paused = True

    def play(self, initial=False):
        '''Plays the daemon, adjusting starting position as necessary.'''

        # FIXME: I was checking if (self.playedSinceLastPlaylistUpdate
        # == False), but I removed that code.  I'm not sure if it's
        # still necessary.

        if initial:
            # Slave is not already playing, or is playing a different song
            self.log.debug("%s.play(initial=True)", self.host)

            # Calculate adjustment
            if self.latency:
                # Use user-set adjustment
                adjustBy = self.latency
            elif self.initialPlayTimes.average:
                self.log.debug("Adjusting by average initial play time")

                adjustBy = self.initialPlayTimes.average
            else:
                self.log.debug("Adjusting by average ping")

                adjustBy = self.pings.average

            adjustBy = myFloat(adjustBy)

            self.log.debug('Adjusting initial play by %s seconds', adjustBy)

            # Update status (not sure if this is still necessary, but
            # it might help avoid race conditions or something)
            self.status()

            # Execute in command list
            # TODO: Is a command list necessary or helpful here?
            try:
                self.command_list_ok_begin()
            except mpd.CommandListError as e:
                # Server was already in a command list; probably a lost client connection, so try again
                self.log.debug("mpd.CommandListError: %s", e)

                self.command_list_end()
                self.command_list_ok_begin()

            # Adjust starting position if necessary
            # TODO: Is it necessary or good to make sure it's a
            # positive adjustment?  There seem to be some tracks that
            # require negative adjustments, but I don't know if that
            # would be the case when playing from a stop
            if adjustBy > 0:
                tries = 0

                # Wait for the server to...catch up?  I don't remember
                # exactly why this code is here, because it seems like
                # the master shouldn't be behind the slaves, but I
                # suppose it could happen on song changes
                while self.elapsed is None and tries < 10:
                    time.sleep(0.2)
                    self.status()
                    self.log.debug(self.song)
                    tries += 1

                # Seek to the adjusted playing position
                self.seek(self.song, self.elapsed + adjustBy)

            # Issue the play command
            super(Client, self).play()

            # Execute command list
            result = self.command_list_end()

        else:
            # Slave is already playing current song
            self.log.debug("%s.play(initial=False)", self.host)

            # Issue the play command
            result = super(Client, self).play()

        # TODO: Not sure if this is still necessary to track...
        self.playedSinceLastPlaylistUpdate = True

        return result

    def seek(self, song, elapsed):
        '''Seeks daemon to a position and updates local attributes for current
        song and elapsed time.'''

        self.song = song
        self.elapsed = elapsed
        super(Client, self).seek(self.song, self.elapsed)

    def status(self):
        '''Gets daemon's status and updates local attributes.'''

        self.currentStatus = super(Client, self).status()

        # Wrap whole thing in try/except because of MPD protocol errors...sigh
        try:

            # Not sure why, but sometimes this ends up as None when the track or playlist is changed...?
            if self.currentStatus:
                # Status response received

                self.song = self.currentStatus['song'] if 'song' in self.currentStatus else None

                for attr in ['duration', 'elapsed']:
                    val = myFloat(self.currentStatus[attr]) if attr in self.currentStatus else None
                    setattr(self, attr, val)

                self.playlistLength = int(self.currentStatus['playlistlength'])
                if self.playlist:
                    self.currentSongFiletype = self.playlist[int(self.song)].split('.')[-1]

                    self.log.debug('Current filetype: %s', self.currentSongFiletype)

                for attr in ['consume', 'random', 'repeat', 'single']:
                    val = True if self.currentStatus[attr] == '1' else False
                    setattr(self, attr, val)

                self.state = self.currentStatus['state']
                self.playing = True if self.state == 'play' else False
                self.paused = True if self.state == 'pause' else False

            else:
                # None?  Sigh...

                self.playlistLength = None

                self.song = None
                self.duration = None
                self.elapsed = None

                self.consume = False
                self.random = False
                self.repeat = False
                self.single = False

                self.state = None
                self.playing = False
                self.paused = False

        except Exception as e:
            # No status response :(
            self.log.error("Unable to get status for client %s: %s", self.host, e)

            # Try to reconnect
            self.checkConnection()

        # TODO: Add other attributes, e.g. {'playlistlength': '55',
        # 'playlist': '3868', 'repeat': '0', 'consume': '0',
        # 'mixrampdb': '0.000000', 'random': '0', 'state': 'stop',
        # 'volume': '-1', 'single': '0'}

    def testPing(self):
        '''Pings the daemon 5 times and sets the initial maxDifference.'''

        for i in range(5):
            self.ping()
            time.sleep(0.1)

        self.maxDifference = myFloat(self.pings.average * 5)

        self.log.debug('Average ping for %s: %s seconds; setting maxDifference: %s',
                       self.host, self.pings.average, self.maxDifference)

class Master(Client):
    def __init__(self, host, password=None, adjustLatency=False):
        self.log = logging.getLogger(self.__class__.__name__)

        super(Master, self).__init__(host, password=password)

        self.slaves = []
        self.slaveDifferences = AveragedList(name='slaveDifferences', length=10)
        self.elapsedLoopRunning = False
        self.adjustLatency = adjustLatency

    def status(self):
        '''Gets the master's status and updates its playlistVersion attribute.'''

        super(Master, self).status()

        # Use the master's reported playlist version (for the slaves
        # we update it manually to match the master)
        self.playlistVersion = self.currentStatus['playlist']

    def addSlave(self, host, password=None):
        '''Connects to a slave, gets its status, and adds it to the list of
        slaves.'''

        slave = Client(host, password=password)

        # Connect to slave
        try:
            slave.connect()
        except Exception as e:
            self.log.error('Unable to connect to slave: %s:%s: %s', slave.host, slave.port, e)
        else:
            self.log.debug('Connected to slave: %s' % slave.host)

            self.slaves.append(slave)

            # Get initial status (this is not automatic upon connection)
            slave.status()

    def syncAll(self):
        '''Syncs all slaves completely.'''

        self.syncPlaylists()
        self.syncOptions()
        self.syncPlayers()

        # Run the sync thread
        if self.adjustLatency:
            self.log.debug('Starting elapsedLoop')

            self.elapsedLoop = Thread(target=self.runElapsedLoop)
            self.elapsedLoop.daemon = True
            self.elapsedLoop.start()

    def syncPlaylists(self):
        '''Syncs all slaves' playlists.'''

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
                        # Remove "file: " from song filename
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
                    self.log.debug("Playlist is the same on slave %s", slave.host)

                    slave.hasBeenSynced = True

            else:
                # Slave has been synced before; sync playlist changes

                # TODO: if slave.playlistVersion is None, handle it

                # Get list of changes
                changes = self.plchanges(slave.playlistVersion)

                # Start command list
                slave.command_list_ok_begin()

                # Make changes
                for change in changes:
                    self.log.debug('Adding to slave:"%s" file:"%s" at pos:%s', slave.host, change['file'], change['pos'])

                    slave.addid(change['file'], change['pos'])

                # Execute command list
                try:
                    results = slave.command_list_end()
                except mpd.ProtocolError as e:
                    if e.message == "Got unexpected 'OK'":
                        self.log.exception("mpd.ProtocolError: Got unexpected 'OK'")

                        continue  # Maybe it will work next time around...

                # Check results
                if not results:
                    # This should not happen.  SIGH.
                    self.log.error("SIGH.")

                    continue

                # Add tags for remote tracks (e.g. files streaming
                # over HTTP) that have tags in playlist
                slave.command_list_ok_begin()
                for num, change in enumerate(changes):
                    if 'http' in change['file']:
                        for tag in ['artist', 'album', 'title', 'genre']:
                            if tag in change:

                                # results is a list of song IDs that
                                # were added; it corresponds to
                                # changes
                                slave.addtagid(int(results[num]), tag, change[tag])
                try:
                    slave.command_list_end()
                except mpd.ProtocolError as e:
                    if e.message == "Got unexpected 'OK'":
                        self.log.exception("mpd.ProtocolError: Got unexpected 'OK'")

                # Update slave status
                slave.status()

                # Truncate the slave playlist to the same length as the master
                if self.playlistLength == 0:
                    slave.clear()
                elif self.playlistLength < slave.playlistLength:
                    self.log.debug("Deleting from %s to %s",
                                   self.playlistLength - 1, slave.playlistLength - 1)

                    slave.delete((self.playlistLength - 1, slave.playlistLength - 1))

                # Check result
                slave.status()
                if slave.playlistLength != self.playlistLength:
                    self.log.error("Playlist lengths don't match for slave %s: %s / %s",
                                   slave.host, slave.playlistLength, self.playlistLength)

                # Make sure the slave's playing status still matches the
                # master (for some reason, deleting a track lower-numbered
                # than the currently playing track makes the slaves stop
                # playing)
                if slave.state != self.state:
                    # Resync the slave
                    self.syncPlayer(slave)

            # Update slave playlist version number to match the master's
            slave.playlistVersion = self.playlistVersion

            # Not sure if this is still necessary
            self.playedSinceLastPlaylistUpdate = False

    def syncOptions(self):
        '''TBI: Sync player options (e.g. random).'''
        pass

    def syncPlayers(self):
        '''Syncs all slaves' player status.'''

        # SOMEDAY: do this in parallel?
        for slave in self.slaves:
            self.syncPlayer(slave)

    def syncPlayer(self, slave):
        '''Sync's a slave's player status.'''

        # Try 5 times
        tries = 0
        while tries < 5:

            try:
                # Update master info
                self.status()

                # Reconnect if connection dropped
                slave.checkConnection()

                # Sync player status
                if self.playing:

                    # Don't re-sync if the slave is already playing the same song
                    if slave.playing and slave.song == self.song:
                        self.log.debug('Playing slave %s, master already playing same song' % slave.host)

                        # BUG: If -l is not set, then this won't sync the playing position.

                    else:
                        # Slave not playing, or playing a different song
                        self.log.debug('Playing slave %s, initial=True' % slave.host)

                        # Seek to current master position before playing
                        try:
                            slave.seek(self.song, self.elapsed)
                        except Exception as e:
                            self.log.error("Couldn't seek slave %s:", slave.host, e)
                            return False

                        # Play it
                        if not slave.play(initial=True):
                            self.log.critical("Couldn't play slave: %s", slave.host)
                            return False

                        # Wait a moment and then check the difference
                        time.sleep(0.2)
                        playLatency = self._compareElapsed(self, slave)

                        if not playLatency:
                            # This probably means the slave isn't playing at all for some reason
                            self.log.error('No playLatency for slave "%s"', slave.host)

                            slave.stop()  # Maybe...?

                            return False

                        self.log.debug('Client %s took %s seconds to start playing' % (slave.host, playLatency))

                        # Update initial play times
                        slave.initialPlayTimes.insert(0, playLatency)

                elif self.paused:
                    slave.pause()
                else:
                    slave.stop()

            except Exception as e:
                self.log.error("Unable to syncPlayer for slave %s.  Tries:%s  Error:%s", slave.host, tries, e)
                tries += 1
            else:
                # Sync succeeded
                return True

    def reSeekPlayer(self, slave):
        '''Seeks the slave to the master's playing position.'''

        # TODO: Idea: Maybe part of the problem is that MPD can't seek
        # songs precisely, but only to certain places, like between
        # frames, or something like that.  So maybe if the master and
        # slaves were both seeked to the same value initially, it
        # would cause them to both seek to about the same place, once
        # or twice, instead of the slaves trying repeatedly to get a
        # good sync.

        # Update master info
        self.status()

        # Reconnect if connection dropped
        slave.checkConnection()

        # Choose adjustment
        if slave.latency:
            # Use the user-set adjustment
            self.log.debug("Adjusting %s by slave.latency: %s", slave.host, slave.latency)

            adjustBy = myFloat(slave.latency)

        else:
            # Calculate adjustment automatically

            if slave.currentSongAdjustments < 1:
                # First adjustment for song
                self.log.debug("First adjustment for song on slave %s", slave.host)

                # Wait until at least 2 measurements
                if len(slave.currentSongDifferences) < 2:
                    self.log.debug('Only %s measurements for current song; skipping adjustment', len(slave.currentSongDifferences))

                    return False

                if len(slave.adjustments) > 5:
                    # More than 5 total adjustments made to this
                    # slave.  Adjust by average adjustment, slightly
                    # reduced to avoid swinging back and forth
                    adjustBy = myFloat(slave.adjustments.average * 0.75)

                    self.log.debug("Adjusting %s by average adjustment: %s", slave.host, adjustBy)
                else:
                    # 5 or fewer total adjustments made to this slave.
                    # Adjust by average ping
                    adjustBy = slave.pings.average

                    self.log.debug("Adjusting %s by average ping: %s", slave.host, adjustBy)
            else:
                # Not the first adjustment for song
                self.log.debug("Not first adjustment for song on slave %s", slave.host)

                if len(slave.currentSongDifferences) < 3:
                    # Not enough measurements for this song
                    self.log.debug("Not enough measurements (%s) for song on slave %s; adjusting by average ping",
                                   len(slave.currentSongDifferences), slave.host)

                    adjustBy = slave.pings.average

                else:
                    # Enough measurements for song
                    self.log.debug("%s measurements for song on slave %s",
                                   len(slave.currentSongDifferences), slave.host)

                    # TODO: Record timestamp of each adjustment.  If
                    # it's been a long time since the last adjustment,
                    # it may be a case of the range suddenly dropping
                    # when a measurement fell off the list, causing
                    # the maxDifference to suddenly drop, causing
                    # another sync.  But sometimes this causes an
                    # unnecessary sync, and this leads to a cascade of
                    # resyncs based on ping...which may mess up a
                    # well-synced song.  :(

                    if slave.currentSongAdjustments > 10:
                        # Too many adjustments for this song.  Try average
                        # ping to settle back down.  Some songs just don't
                        # seek reliably or something.
                        self.log.debug("Too many adjustments (%s) for song on slave %s; adjusting by average ping",
                                       slave.currentSongAdjustments, slave.host)

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
                        # Just the right number of measurements for
                        # this song.

                        # (trying without this) Adjust by less than
                        # the average difference to gradually hone in
                        # on the right adjustment.  0.5 is ok for
                        # local files, but too small for remote ones;
                        # trying 0.75.  I think the result should NOT
                        # be inverted, but I had it being inverted
                        # before...and it seemed to work... or did it?
                        # I'm beginning to wonder if seek-latency is
                        # so wild that the best way to sync is to just
                        # sync with no adjustment or ping-only
                        # adjustment and redo it a few times until you
                        # get lucky and the difference is small.

                        # Invert the difference to adjust in the
                        # opposite direction of the difference.
                        adjustBy = myFloat(slave.currentSongDifferences.average * -1)

                        self.log.debug("Adjusting %s by currentSongDifferences.average: %s", slave.host, adjustBy)

            # TODO: If the difference is too great, it seems that the
            # slave MPD has hit a bug where it thinks that song
            # duration is shorter than it is and won't seek past that
            # point.  Stopping the slave playback and re-playing the
            # track seems to fix it.

            # Sometimes the adjustment goes haywire.  If it's greater
            # than 1% of the song duration, or greater than 1 second, adjust by average ping
            absAdjustBy = abs(adjustBy)
            if ((slave.duration and absAdjustBy > slave.duration * 0.01)
                or absAdjustBy > 1):
                self.log.debug('Adjustment to %s too large:%s  Adjusting by average ping:%s',
                               slave.host, adjustBy, slave.pings.average)

                adjustBy = slave.pings.average

            #  Not sure if I should update the master's position
            #  again; that might require redoing the calculations.
            #  Commenting out for now: self.status()

            # Calculate position
            position = myFloat(self.elapsed - adjustBy)
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
            # Try to seek to current playing position, adjusted for
            # latency
            slave.seek(self.song, position)

        except Exception as e:
            # Seek failed
            self.log.error("Unable to seek slave %s: %s", slave.host, e)

            # Try to completely resync the slave
            slave.playlistVersion = None
            self.syncPlaylists()

            # Clear song adjustments to prevent wild jittering after
            # seek timeouts
            slave.currentSongAdjustments = 0
            slave.checkConnection()

        else:
            # Seek succeeded
            slave.currentSongAdjustments += 1

            # Don't record adjustment if it's just the average ping
            if adjustBy != slave.pings.average:
                # It wasn't; record it
                slave.adjustments.insert(0, adjustBy)
                slave.fileTypeAdjustments[slave.currentSongFiletype].append(adjustBy)

            # Reset song differences (maybe this or just cutting it in
            # half will help prevent too many consecutive adjustments
            while len(slave.currentSongDifferences) > 5:
                slave.currentSongDifferences.pop()

    def runElapsedLoop(self):
        '''Runs a loop trying to keep the slaves in sync with the master.'''

        # Don't run the loop on top of itself
        if self.elapsedLoopRunning == True:
            return False

        self.elapsedLoopRunning = True

        # Make new connection to master server that won't idle()
        self.masterTester = Master(self.host)
        self.masterTester.connect()

        # Run the loop
        while True:

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
                        # Lock the slave
                        slave.syncLoopLocked = True

                    # TODO: If finished seeking, stop the loop until
                    # the next track (and restart it on track change)

                    self._seekIfNecessary(slave)

                    # Unlock the slave
                    slave.syncLoopLocked = False

                # Sleep for 400ms for each measurement, but at least 500ms
                sleepTime = 0.4 * len(slave.currentSongDifferences)
                if sleepTime < 0.5:
                    sleepTime = 0.5

                # Print comparison between two slaves
                if len(self.slaves) > 1:
                    self.slaveDifferences.insert(0, abs(self.slaves[0].currentSongDifferences.average \
                                                        - self.slaves[1].currentSongDifferences.average))
                    self.log.debug("Average difference between slaves 1 and 2: %s",
                                   self.slaveDifferences.average)

            else:
                # Not playing; sleep 2 seconds
                sleepTime = 2

            # BUG: If there are multiple slaves, this sleeps for
            # whatever sleepTime was set to for the last slave.  Not a
            # big deal, but might need fixing.
            self.log.debug('Sleeping for %s seconds', sleepTime)

            time.sleep(sleepTime)

    def _seekIfNecessary(self, slave):
        '''Reseeks the slave if necessary.'''

        # Compare the master and slave's elapsed time
        self._compareElapsed(self.masterTester, slave)

        # Don't seek if we're finished seeking this song
        if not slave.currentSongShouldSeek:
            self.log.debug('Not seeking this song anymore')

            return

        # Calculate maxDifference
        if len(slave.currentSongDifferences) >= 5:
            # At least 5 measurements for current
            # song

            if len(slave.currentSongDifferences) >= 10:
                # 10 or more measurements; use one-quarter of the
                # range
                maxDifference = myFloat(slave.currentSongDifferences.range / 4)

                # Add half the average to prevent it from being too
                # small and causing excessive reseeks.  For some songs
                # the range can be very small, like 38ms, but for
                # others it can be consistently 100-200 ms.
                maxDifference += (abs(slave.currentSongDifferences.average) / 2)
            else:
                # 5-9 measurements; use one-half of the
                # range
                maxDifference = myFloat(slave.currentSongDifferences.range / 2)

            # Use at least half of the biggest difference to prevent
            # the occasional small range from causing unnecessary
            # syncs.  This may not be necessary with adding half the
            # average a few lines up, but it may be a good extra
            # precaution.
            minimumMaxDifference = (max([abs(slave.currentSongDifferences.max),
                                         abs(slave.currentSongDifferences.min)])
                                    * 0.5)
            if maxDifference < minimumMaxDifference:
                self.log.debug('maxDifference too small (%s); setting maxDifference to '
                               'half of the biggest difference', maxDifference)

                maxDifference = myFloat(minimumMaxDifference)

        elif slave.pings.average:
            # Use average ping

            # Use the larger of master or slave ping so the script can run on either one
            maxDifference = myFloat(max([slave.pings.average,
                                         self.masterTester.pings.average])
                                    * 30)

            # But don't go over 100 ms; if it's that high, better to
            # just resync again
            if maxDifference > 0.2:
                # But this does not work well for remote files.  The
                # difference sometimes never gets lower than the
                # maxDifference, so it resyncs forever, and always by
                # the average ping or average slave adjustment, which
                # basically loops doing the same syncs forever.  Sigh.
                maxDifference = 0.2

        else:
            # This shouldn't happen, but if it does, use 0.2 until we
            # have something better.  0.2 seems like a lot, and for
            # local files it is, but remote files don't seek as
            # quickly or consistently, so 0.1 can cause excessive
            # reseeking in a short period of time
            maxDifference = 0.2

        self.log.debug("maxDifference for slave %s: %s", slave.host, maxDifference)

        # If the average difference is less than 30ms, and there are
        # enough measurements, stop seeking until the next song.  Do
        # this down here so we can still watch measurements.
        if (len(slave.currentSongDifferences) >= 10
            and abs(slave.currentSongDifferences.average) < 0.030):
            self.log.debug('Average difference below 30ms; not seeking this song anymore')

            slave.currentSongShouldSeek = False
            return

        # Adjust if difference is too big
        absAverage = abs(slave.currentSongDifferences.average)
        if absAverage > maxDifference:
            self.log.debug('Average song difference (%s) > maxDifference (%s)',
                           absAverage, maxDifference)

            # Don't reseek if the current difference is
            # both less than the average difference and
            # within 50% of it; hopefully this will help
            # avoid excessive reseeking
            absCurrentDifference = abs(slave.currentSongDifferences[0])
            if (absCurrentDifference < absAverage
                and (absCurrentDifference / absAverage) < 0.5):

                self.log.debug('Current difference (%s) < average difference (%s); not reseeking',
                               absCurrentDifference, absAverage)

            else:
                # Do the reseek
                self.log.info('Resyncing player to minimize difference for slave %s',
                              slave.host)

                try:
                    # Use the masterTester object because the main
                    # master object is in idle
                    self.masterTester.reSeekPlayer(slave)
                    slave.reSeekedTimes += 1

                    # Give it a moment to settle before looping again
                    time.sleep(1)

                    self.log.debug("Client %s now reseeked %s times", slave.host, slave.reSeekedTimes)

                except Exception as e:
                    # Seek failed...sigh...
                    self.log.error("%s: %s", type(e), e)

                    # Try to reconnect if necessary
                    try:
                        slave.checkConnection()
                    except:
                        self.log.error("Couldn't reconnect to slave %s", slave.host)

    def _compareElapsed(self, master, slave):
        '''Compares the master's and the slave's playing position and sets
        their attributes accordingly, then returns the difference.'''

        # TODO: Some songs MPD seems unable to sync properly.  If the
        # seek value is higher than a certain position, it will always
        # seek to around the same value, meaning it basically can't
        # seek past a certain point in the song.  This leads to
        # endless syncs, and it means the slave seeks to the same
        # place over and over again until the master moves on to the
        # next song.  This causes the slave to sound like a broken
        # record.  Perhaps charming in a nostalgic kind of way, but
        # not exactly desirable...  So I guess I need to try to detect
        # this somehow.  Sigh.

        # Check pings
        masterPing = myFloat(timeFunction(master.ping))
        slavePing = myFloat(timeFunction(slave.ping))
        ping = myFloat(abs(masterPing - slavePing))

        self.log.debug("Last ping time for slave %s: %s", slave.host, ping)

        # Get master status and time how long it takes
        masterStatusLatency = myFloat(timeFunction(master.status))

        self.log.debug("masterStatusLatency:%s", masterStatusLatency)

        # Get slave status and time how long it takes
        slaveStatusLatency = myFloat(timeFunction(slave.status))

        self.log.debug("slaveStatusLatency:%s", slaveStatusLatency)

        # If song changed, reset differences
        if slave.lastSong != slave.song:
            self.log.debug("Song changed (%s -> %s); resetting %s.currentSongDifferences",
                           slave.lastSong, slave.song, slave.host)

            # TODO: Put this in a function?
            slave.currentSongShouldSeek = True
            slave.currentSongAdjustments = 0
            slave.currentSongDifferences = AveragedList(name='%s.currentSongDifferences' % slave.host, length=10)
            slave.lastSong = slave.song

        if slave.elapsed:

            # Seems like it would make sense to add the
            # masterStatusLatency, but I seem to be observing that the
            # opposite is the case...
            difference = myFloat(master.elapsed - (slave.elapsed + slaveStatusLatency))

            # Record the difference
            slave.currentSongDifferences.insert(0, difference)

            # "Difference" is approximately aligned with the average
            # below in the debug output
            self.log.debug('Master/%s elapsed:%s/%s  Difference:%s',
                      slave.host, master.elapsed, slave.elapsed, difference)
            self.log.debug(slave.currentSongDifferences)

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
                        dest="adjustLatency", action="store_true",
                        help="Monitor latency between master and slaves and try to keep slaves' "
                             "playing position in sync with the master's")
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
    master = Master(args.master, password=args.password, adjustLatency=args.adjustLatency)

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

    # try/except to catch Ctrl+C and print debug info
    try:
        # Enter idle loop
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
