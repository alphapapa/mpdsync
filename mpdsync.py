#!/usr/bin/env python

# * mpdsync.py
# Originally written by Nick Pegg <https://github.com/nickpegg/mpdsync>
# Rewritten and updated to use python-mpd2 by Adam Porter <adam@alphapapa.net>

# ** Imports
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

# ** Constants

DEFAULT_PORT = 6600
FILE_PREFIX_RE = re.compile('^file: ')

# Max number of adjustments to make before adjusting by ping again
MAX_ADJUSTMENTS = 5

# Max adjustment size in ms (larger than this will trigger an adjustment by ping)
MAX_ADJUSTMENT = 0.300

# ** Classes
class MyFloat(float):
    '''Rounds and pads to 3 decimal places when printing.  Also overrides
    built-in operator methods to return myFloats instead of regular
    floats.'''

    # There must be a better, cleaner way to do this, maybe using
    # decorators or overriding __metaclass__, but I haven't been able
    # to figure it out.  Since you can't override float's methods, you
    # can't simply override __str__, for all floats.  And because
    # whenever you +|-|/|* on a subclassed float, it returns a regular
    # float, you have to also override those built-in methods to keep
    # returning the subclass.

    def __init__(self, num, roundBy=3):
        super(MyFloat, self).__init__(num)
        self.roundBy = roundBy

    def __abs__(self):
        return MyFloat(float.__abs__(self))

    def __add__(self, val):
        return MyFloat(float.__add__(self, val))

    def __div__(self, val):
        return MyFloat(float.__div__(self, val))

    def __mul__(self, val):
        return MyFloat(float.__mul__(self, val))

    def __sub__(self, val):
        return MyFloat(float.__sub__(self, val))

    def __str__(self):
        return "{:.3f}".format(round(self, self.roundBy))

    # __repr__ is used in, e.g. mpd.seek(), so it gets a rounded
    # float.  MPD doesn't support more than 3 decimal places, anyway.
    __repr__ = __str__


class AveragedList(list):

    def __init__(self, data=None, length=None, name=None, printDebug=False):
        self.log = logging.getLogger(self.__class__.__name__)

        # TODO: Add weighted average.  Might be better than using the range.

        self.name = name
        self.length = length
        self.max = 0
        self.min = 0
        self.range = 0
        self.average = 0  # actually the moving average; not renaming right now...
        self.overall_average = 0
        self.printDebug = printDebug

        # TODO: Isn't there a more Pythonic way to do this?
        if data:
            super(AveragedList, self).__init__(data)
            self._updateStats()
        else:
            super(AveragedList, self).__init__()

    def __str__(self):
        return 'name:%s length:%s overall-average:%s moving-average:%s range:%s max:%s min:%s' % (
            self.name, len(self), self.overall_average, self.average, self.range, self.max, self.min)

    __repr__ = __str__

    def append(self, arg):
        arg = MyFloat(arg)
        super(AveragedList, self).append(arg)
        self._updateStats()

    def clear(self):
        '''Empties the list.'''

        while len(self) > 0:
            self.pop()

    def extend(self, *args):
        args = [[MyFloat(a) for l in args for a in l]]
        super(AveragedList, self).extend(*args)
        self._updateStats()

    def insert(self, pos, *args):
        args = [MyFloat(a) for a in args]
        super(AveragedList, self).insert(pos, *args)

        # Remove elements if length is limited
        while (self.length
               and len(self) > self.length):
            self.pop()
        self._updateStats()

    def _updateStats(self):
        # Actually the moving average.  Taking the first 10 elements
        # since I usually insert rather than append (not sure why,
        # though; maybe I should change that)
        to_average = self[:10]
        self.average = MyFloat(sum(to_average) / len(to_average))

        self.overall_average = MyFloat(sum(self) / len(self))
        self.max = MyFloat(max(self))
        self.min = MyFloat(min(self))
        self.overall_range = MyFloat(self.max - self.min)

        # "Moving range" of first 10 elements (necessary for properly
        # setting the max difference)
        self.range = MyFloat(max(self[:10]) - min(self[:10]))

        if self.printDebug:
            self.log.debug(self)


class Client(mpd.MPDClient):
    '''Subclasses mpd.MPDClient, keeping state data, reconnecting as
    needed, etc.'''

    initAttrs = {None: ['currentStatus', 'lastSong',
                        'currentSongFiletype', 'playlist',
                        'playlistVersion', 'playlistLength',
                        'song', 'duration', 'elapsed', 'state',
                        'hasBeenSynced', 'playing', 'paused'],
                 False: ['consume', 'random', 'repeat',
                         'single']}

    def __init__(self, host, port=DEFAULT_PORT, password=None, latency=None,
                 logger=None):

        super(Client, self).__init__()

        # Command timeout
        self.timeout = 10

        # Split host/latency
        if '/' in host:
            host, latency = host.split('/')

        if latency is not None:
            self.latency = float(latency)
        else:
            self.latency = None

        # Split host/port
        if ':' in host:
            host, port = host.split(':')

        self.host = host
        self.port = port
        self.password = password

        self.log = logger.getChild('%s(%s)' %
                                   (self.__class__.__name__, self.host))

        self.syncLoopLocked = False
        self.playedSinceLastPlaylistUpdate = False

        self.currentSongShouldSeek = True
        self.currentSongAdjustments = None
        self.currentSongDifferences = AveragedList(
            name='currentSongDifferences')

        self.pings = AveragedList(name='%s.pings' % self.host, length=10)
        self.adjustments = AveragedList(name='%sadjustments' % self.host,
                                        length=20)
        self.initialPlayTimes = AveragedList(name='%s.initialPlayTimes'
                                             % self.host, length=20,
                                             printDebug=True)

        # MAYBE: Should I reset this in _initAttrs() ?
        self.reSeekedTimes = 0

        # Record adjustments by file type to see if there's a pattern
        self.fileTypeAdjustments = defaultdict(AveragedList)

        # TODO: Record each song's number of adjustments in a list (by
        # filename), and print on exit.  This way I can play a short
        # playlist in a loop and see if there is a pattern with
        # certain songs being consistently bad at syncing and seeking.
        self.song_adjustments = []

        self.song_differences = []

    def ping(self):
        '''Pings the daemon and records how long it took.'''

        self.pings.insert(0, timeFunction(super(Client, self).ping))

    def checkConnection(self):
        '''Pings the daemon and tries to reconnect if necessary.'''

        # I don't know why this is necessary, but for some reason the
        # slave connections tend to get dropped.
        try:
            self.ping()

        except Exception as e:
            self.log.debug('Connection to "%s" seems to be down.  '
                           'Trying to reconnect...', self.host)

            # Try to disconnect first
            try:
                self.disconnect()  # Maybe this will help it reconnect
            except Exception as e:
                self.log.exception("Couldn't DISconnect from client %s: %s",
                                   self.host, e)

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

        # Reset initial values
        for val, attrs in self.initAttrs.iteritems():
            for attr in attrs:
                setattr(self, attr, val)

        super(Client, self).connect(self.host, self.port)

        if self.password:
            super(Client, self).password(self.password)

        self.testPing()

        self.log.debug("Connected.")

    def disconnect(self):
        "Disconnect from MPD."

        super(Client, self).disconnect()

        self.log.debug("Disconnected.")

    def getPlaylist(self):
        '''Gets the playlist from the daemon.'''

        self.playlist = super(Client, self).playlist()

        self.log.debug("Got playlist")

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
            if self.latency is not None:
                # Use user-set adjustment
                offset = self.latency
            elif self.initialPlayTimes.average:
                self.log.debug("Adjusting by average initial play time")

                offset = self.initialPlayTimes.average
            else:
                self.log.debug("Adjusting by average ping")

                offset = self.pings.average

            self.log.debug('Adjusting initial play by %s seconds', offset)

            # Update status (not sure if this is still necessary, but
            # it might help avoid race conditions or something)
            self.status()

            # Execute in command list
            # TODO: Is a command list necessary or helpful here?
            try:
                self.command_list_ok_begin()
            except mpd.CommandListError as e:
                # Server was already in a command list; probably a
                # lost client connection, so try again
                self.log.exception("mpd.CommandListError: %s", e)

                self.command_list_end()
                self.command_list_ok_begin()

            # Adjust starting position if necessary
            # TODO: Is it necessary or good to make sure it's a
            # positive adjustment?  There seem to be some tracks that
            # require negative adjustments, but I don't know if that
            # would be the case when playing from a stop
            if offset > 0:
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
                self.seek(self.song, self.elapsed + offset)

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

        # Wrap whole thing in try/except because of MPD protocol
        # errors.  But I may have fixed this by "locking" each client
        # in the loop, so this may not be necessary anymore.
        try:

            # Not sure why, but sometimes this ends up as None when
            # the track or playlist is changed...?
            if self.currentStatus:
                # Status response received

                # Set playlist attrs
                self.playlistLength = int(self.currentStatus['playlistlength'])
                if self.playlist:
                    if self.song:
                        if int(self.song) >= len(self.playlist):
                            self.log.exception(
                                'Current song number > (playlist length - 1)'
                                ' (%s>%s).  What is causing this pesky bug?',
                                int(self.song), len(self.playlist))
                        else:
                            self.currentSongFiletype = (
                                self.playlist[int(self.song)].split('.')[-1])
                    else:
                        self.currentSongFiletype = None

                    self.log.debug('Current filetype: %s',
                                   self.currentSongFiletype)

                # Set True/False attrs
                for attr in self.initAttrs[False]:
                    val = (True
                           if self.currentStatus[attr] == '1'
                           else False)
                    setattr(self, attr, val)

                # Set playing state attrs
                self.state = self.currentStatus['state']
                self.playing = (True
                                if self.state == 'play'
                                else False)
                self.paused = (True
                               if self.state == 'pause'
                               else False)

                # Set song attrs
                self.song = (self.currentStatus['song']
                             if 'song' in self.currentStatus
                             else None)
                for attr in ['duration', 'elapsed']:
                    val = (MyFloat(self.currentStatus[attr])
                           if attr in self.currentStatus
                           else None)
                    setattr(self, attr, val)

            else:
                # None?  Sigh...  This shouldn't happen...if it does
                # I'll need to reconnect, I think...
                self.log.error("No status received for client %s", self.host)

        except Exception as e:
            # No status response :(
            self.log.exception("Unable to get status for client %s: %s",
                               self.host, e)

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

        self.maxDifference = self.pings.average * 5

        self.log.debug('Average ping for %s: %s seconds; '
                       'setting maxDifference: %s',
                       self.host, self.pings.average, self.maxDifference)


class Master(Client):
    def __init__(self, *args, **kwargs):

        # Don't pass adjustLatency to Client
        # FIXME: Is there a nicer way to do this?
        attr = 'adjustLatency'
        if attr in kwargs:
            self.adjustLatency = kwargs[attr]
            del kwargs[attr]
        else:
            self.adjustLatency = None

        super(Master, self).__init__(*args, **kwargs)

        self.slaves = []
        self.slaveDifferences = AveragedList(name='slaveDifferences', length=10)
        self.elapsedLoopRunning = False

        self.seeker = None

    def _current_difference(self, slave):
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
        self.ping()
        slave.ping()
        masterPing = self.pings[0]
        slavePing = slave.pings[0]
        ping = abs(masterPing - slavePing)

        self.log.debug("Last ping time for slave %s: %s", slave.host, ping)

        # Get master status and time how long it takes
        masterStatusLatency = MyFloat(timeFunction(self.status))

        self.log.debug("masterStatusLatency:%s", masterStatusLatency)

        # Get slave status and time how long it takes
        slaveStatusLatency = MyFloat(timeFunction(slave.status))

        self.log.debug("slaveStatusLatency:%s", slaveStatusLatency)

        # If song changed, reset differences
        if slave.lastSong != slave.song:
            self.log.debug("Song changed (%s -> %s); "
                           "resetting %s.currentSongDifferences",
                           slave.lastSong, slave.song, slave.host)

            # TODO: Put this in a function?
            slave.currentSongShouldSeek = True
            slave.currentSongAdjustments = AveragedList(name='%s.currentSongAdjustments' % slave.host,
                                                        length=10, printDebug=True)
            slave.currentSongDifferences = AveragedList(name='%s.currentSongDifferences' % slave.host)
            slave.lastSong = slave.song

            # Record song differences and adjustments for later debugging
            slave.song_adjustments.append({'file': slave.playlist[int(slave.song)],
                                           'adjustments': slave.currentSongAdjustments})
            slave.song_differences.append({'file': slave.playlist[int(slave.song)],
                                           'differences': slave.currentSongDifferences})

        if slave.elapsed:

            # Seems like it would make sense to add the
            # masterStatusLatency, but I seem to be observing that the
            # opposite is the case...
            difference = self.elapsed - (slave.elapsed + slaveStatusLatency)

            # Record the difference
            slave.currentSongDifferences.insert(0, difference)

            # "Difference" is approximately aligned with the average
            # below in the debug output
            self.log.debug('Master/%s elapsed:%s/%s  Difference:%s',
                      slave.host, self.elapsed, slave.elapsed, difference)
            self.log.debug(slave.currentSongDifferences)
            self.log.debug(slave.currentSongAdjustments)

            return abs(slave.currentSongDifferences.average)

    def status(self):
        '''Gets the master's status and updates its playlistVersion
        attribute.'''

        super(Master, self).status()

        # Use the master's reported playlist version (for the slaves
        # we update it manually to match the master)
        self.playlistVersion = self.currentStatus['playlist']

    def addSlave(self, host, password=None):
        '''Connects to a slave, gets its status, and adds it to the list of
        slaves.'''

        slave = Client(host, password=password, logger=self.log)

        # Connect to slave
        try:
            slave.connect()
        except Exception as e:
            self.log.exception('Unable to connect to slave: %s:%s: %s',
                               slave.host, slave.port, e)
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
                raise Exception("Unable to reconnect to slave: %s", slave.host)

            if not slave.hasBeenSynced:
                # Do a full sync the first time

                # Compare playlists; don't clear if they're the same
                slave.getPlaylist()
                if slave.playlist != self.playlist:
                    # Playlists differ
                    self.log.debug("Playlist differs on slave %s; syncing...",
                                   slave.host)

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
                        self.log.critical("Couldn't add tracks to playlist on slave: %s",
                                          slave.host)
                        continue
                    else:
                        self.log.debug("Added to playlist on slave %s, result: %s",
                                       slave.host, result)

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
                    self.log.debug('Adding to slave:"%s" file:"%s" at pos:%s',
                                   slave.host, change['file'], change['pos'])

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

        # Restart sync thread if necessary
        if self.adjustLatency:
            if self.playing:
                self.checkSeeker()
            else:
                if not self.paused:
                    # Stopped
                    self.stopSeeker()

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

                # Update slave status
                slave.status()

                # Sync player status
                if self.playing:

                    # Verify playlist is set.  Sometimes this can get emptied somehow, when connections drop...
                    if not slave.playlist:
                        self.syncPlaylists()

                    # Don't re-sync if the slave is already playing
                    # the same song at the right place
                    if (slave.playing
                        and slave.song == self.song
                        and self._current_difference(slave) < 1):

                        self.log.debug('Slave %s and master already playing same song, less than 1 second apart',
                                       slave.host)

                    else:
                        # Slave not playing, or playing a different song
                        self.log.debug('Playing slave %s, initial=True' % slave.host)

                        # Seek to current master position before playing
                        try:
                            slave.seek(self.song, self.elapsed)
                        except Exception as e:
                            self.log.exception("Couldn't seek slave %s:", slave.host, e)

                            return False

                        # Play it
                        if not slave.play(initial=True):
                            self.log.critical("Couldn't play slave: %s", slave.host)

                            return False

                        # Wait a moment and then check the difference
                        time.sleep(0.2)
                        playLatency = self._current_difference(slave)

                        if not playLatency:
                            # This probably means the slave isn't playing at all for some reason
                            self.log.error('No playLatency for slave "%s"', slave.host)

                            slave.stop()  # Maybe...?

                            return False

                        self.log.debug('Client %s took %s seconds to start playing',
                                       slave.host, playLatency)

                        # Update initial play times
                        slave.initialPlayTimes.insert(0, playLatency)

                elif self.paused:
                    slave.pause()
                else:
                    slave.stop()

            except Exception as e:
                self.log.exception("Unable to syncPlayer for slave %s.  Tries:%s  Error:%s",
                                   slave.host, tries, e)
                tries += 1

                return False
            else:
                # Sync succeeded
                return True

    def startSeeker(self):
        '''Runs a loop trying to keep the slaves in sync with the master.'''

        # Make new connection to master server that won't idle()
        self.seeker = Seeker(self)
        self.seeker.connect()
        self.seeker.start_loop()

    def checkSeeker(self):
        "Restart seeker thread if necessary."

        if self.seeker:
            self.seeker.check_loop()
        else:
            self.startSeeker()

    def stopSeeker(self):
        "Stop sync loop thread, disconnect seeker connection, and cleanup Seeker instance."

        if self.seeker:
            # Stop thread
            self.seeker.sync = False
            while self.seeker.thread.is_alive():
                self.log.debug("Waiting for seeker thread to stop...")
                time.sleep(0.1)

            self.log.debug("Seeker thread stopped.")

            # Disconnect and cleanup object
            self.seeker.disconnect()
            self.seeker = None


class Seeker(Master):
    '''Used to make a second connection to the master server that doesn't
    idle, which can be used to re-seek slaves' playing positions.'''

    # TODO: It would seem like a good idea to avoid doing some things
    # in this instance, like syncing the playlist, since that's not
    # relevant to the seeker and takes some time.

    def __init__(self, master):
        super(Seeker, self).__init__(master.host, logger=master.log)

        # By doing this, we don't have to run status() in the loop,
        # because it can get the master's playing status from the
        # other instance
        self.master = master
        self.slaves = master.slaves
        self.sync = False

    def start_loop(self):
        self.sync = True

        # Remove any stale locks
        for slave in self.slaves:
            slave.syncLoopLocked = False

        self.thread = Thread(target=self._syncLoop)
        self.thread.daemon = True
        self.thread.start()

    def check_loop(self):
        "Restart _syncLoop if necessary."

        if self.sync and not self.thread.is_alive():
            self.log.warning("Sync thread died; restarting...")

            self.checkConnection()
            self.start_loop()

    def _syncLoop(self):
        '''Runs a loop trying to keep the slaves in sync with the master.'''

        # Run the loop
        while self.sync:
            sleepTime = None

            # NOTE: This works, but the thread won't stop while it's
            # sleeping, so it can take several seconds in the worst
            # case for the thread to notice that it should stop.  This
            # doesn't seem like a problem most of the time, unless the
            # user stops MPD and then starts it playing again before
            # this thread has terminated, in which case the slaves
            # won't start playing again until the thread terminates
            # and the script catches up with the subsystem updates.
            # I'm not sure how to fix this short of switching to a
            # subprocess, because killing threads is not supported,
            # and any workarounds are necessarily ugly hacks which
            # might have bad side effects.

            if self.master.playing:

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

                    if self._reseek_necessary(slave):
                        self._reseek_player(slave)
                        sleepTime = 2  # Sleep 2 seconds after reseeking

                    # Unlock the slave
                    slave.syncLoopLocked = False

                # Sleep for 400ms for each measurement, but at least 2 s
                if not sleepTime:
                    sleepTime = max(2, 0.4 * len(slave.currentSongDifferences))

                # Print comparison between two slaves
                if len(self.slaves) > 1:
                    self.slaveDifferences.insert(0, abs(self.slaves[0].currentSongDifferences.average
                                                        - self.slaves[1].currentSongDifferences.average))
                    self.log.debug("Average difference between slaves 1 and 2: %s",
                                   self.slaveDifferences.average)

            else:
                # Not playing; sleep 2 seconds
                sleepTime = 2

                # TODO: Instead, stop this thread, and restart it when playing resumes

            # BUG: If there are multiple slaves, this sleeps for
            # whatever sleepTime was set to for the last slave.  Not a
            # big deal, but might need fixing.
            self.log.debug('Sleeping for %s seconds', sleepTime)

            time.sleep(sleepTime)

    def _reseek_player(self, slave):
        '''Seeks the slave to the master's playing position.'''

        # TODO: Idea: Maybe part of the problem is that MPD can't seek
        # songs precisely, but only to certain places, like between
        # frames, or something like that.  So maybe if the master and
        # slaves were both seeked to the same value initially, it
        # would cause them to both seek to about the same place, once
        # or twice, instead of the slaves trying repeatedly to get a
        # good sync.

        adjustBy = self._calc_adjustment(slave)

        # Calculate position
        position = self.elapsed - adjustBy
        if position < 0:
            self.log.debug("Position for %s was < 0 (%s); skipping adjustment",
                           slave.host, position)

            return False

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
            self.log.exception("Unable to seek slave %s: %s", slave.host, e)

            # Try to completely resync the slave
            slave.playlistVersion = None
            self.syncPlaylists()

            # Clear song adjustments to prevent wild jittering after
            # seek timeouts
            slave.currentSongAdjustments.clear()
            slave.checkConnection()

            return False

        else:
            # Seek succeeded

            slave.adjustments.insert(0, adjustBy)
            slave.currentSongAdjustments.append(adjustBy)
            slave.fileTypeAdjustments[slave.currentSongFiletype].append(adjustBy)

            # Reset song differences
            slave.currentSongDifferences.clear()

            return True

    def _calc_adjustment(self, slave):
        "Return adjustment to make to sync slave with master."

        # Choose adjustment
        if slave.latency is not None:
            # Use the user-set adjustment
            self.log.debug("Adjusting %s by slave.latency: %s", slave.host, slave.latency)

            adjustBy = MyFloat(slave.latency)

        else:
            # Calculate adjustment automatically

            if len(slave.currentSongAdjustments) < 1:
                # First adjustment for song
                self.log.debug("First adjustment for song...")

                if len(slave.adjustments) > 5:
                    # More than 5 total adjustments made to this
                    # slave.  Adjust by average adjustment, slightly
                    # reduced to avoid swinging back and forth
                    adjustBy = slave.adjustments.average * 0.75

                    self.log.debug("Adjusting %s by average slave adjustment: %s", slave.host, adjustBy)
                else:
                    # 5 or fewer total adjustments made to this slave.
                    # Adjust by average ping
                    adjustBy = slave.pings.average

                    self.log.debug("Adjusting %s by average ping: %s", slave.host, adjustBy)
            else:
                # Not the first adjustment for song

                if len(slave.currentSongAdjustments) > MAX_ADJUSTMENTS:
                    # Too many adjustments; alternate between ping and average difference
                    self.log.debug("Too many adjustments (%s > %s)", len(slave.currentSongAdjustments), MAX_ADJUSTMENTS)

                    if not even(len(slave.currentSongAdjustments)):
                        # Adjust by ping
                        self.log.debug("Adjusting by ping...")

                        adjustBy = slave.pings.average
                    else:
                        # Try adjusting by difference again
                        self.log.debug("Trying to adjust by difference...")

                        adjustBy = slave.currentSongDifferences.average

                else:
                    # Adjust by average difference
                    adjustBy = slave.currentSongDifferences.average

                    self.log.debug("Adjusting %s by average difference: %s",
                                   slave.host, adjustBy)

            # TODO: If the difference is too great, it seems that the
            # slave MPD has hit a bug where it thinks that song
            # duration is shorter than it is and won't seek past that
            # point.  Stopping the slave playback and re-playing the
            # track seems to fix it.

            absAdjustBy = abs(adjustBy)
            if absAdjustBy > MAX_ADJUSTMENT:
                # Adjustment too large; use ping
                self.log.debug("Adjustment too large (%s > %s); adjusting by ping", absAdjustBy, MAX_ADJUSTMENT)

                adjustBy = slave.pings.average

            if adjustBy == slave.pings.average:
                # Adjusting by ping, clear differences
                self.log.debug("Clearing differences")

                slave.currentSongDifferences.clear()
            else:
                # Invert final difference-based adjustment
                adjustBy = adjustBy * -1

        return adjustBy

    def _reseek_necessary(self, slave):
        "Return True if reseek is necessary."

        # Update master info
        self.status()

        # Reconnect if connection dropped
        slave.checkConnection()

        if not slave.currentSongShouldSeek:
            self.log.debug('Not seeking this song anymore')

            return False

        current_difference = self._current_difference(slave)
        max_difference = self._max_difference(slave)

        if len(slave.currentSongDifferences) < 3:
            self.log.debug("Less than 3 measurements; not seeking")

            return False

        if (current_difference > max_difference
            and abs(slave.currentSongDifferences[0]) > max_difference):
            # Current and average difference too large; reseek
            self.log.debug("Average difference (%s) > max difference (%s); reseeking..." % (current_difference, max_difference))

            return True
        else:
            self.log.debug("Average difference within acceptable range; not reseeking")

            return False

    def _max_difference(self, slave):
        "Return max difference between slave and master."

        if len(slave.currentSongDifferences) >= 5:
            # At least 5 measurements for current song

            if len(slave.currentSongDifferences) >= 10:
                # 10 or more measurements; use 1/4 of the range
                maxDifference = slave.currentSongDifferences.range / 4

                # Add half the average to prevent it from being too
                # small and causing excessive reseeks.  For some songs
                # the range can be very small, like 38ms, but for
                # others it can be consistently 100-200 ms.
                maxDifference += (abs(slave.currentSongDifferences.average) / 2)
            else:
                # 5-9 measurements; use 1/2 of the range
                maxDifference = slave.currentSongDifferences.range / 2

            # Use at least half of the biggest difference to prevent
            # the occasional small range from causing unnecessary
            # syncs.  This may not be necessary with adding half the
            # average a few lines up, but it may be a good extra
            # precaution.

            # Use the max and min of the last 10 measurements, but not less than 30ms
            minimumMaxDifference = max(0.030, (0.5 * max(abs(max(slave.currentSongDifferences[:10])),
                                                         abs(min(slave.currentSongDifferences[:10])))))

            if maxDifference < minimumMaxDifference:
                self.log.debug('maxDifference too small (%s); setting maxDifference to half of the biggest difference', maxDifference)

                maxDifference = minimumMaxDifference

        elif slave.pings.average:
            # Use average ping

            # Use the larger of master or slave ping so the script can
            # run on either one, multiplied by 30 (e.g. a 2 ms LAN
            # ping becomes 60 ms max difference), but not less than 30ms
            maxDifference = max(0.030, (30 * max([slave.pings.average, self.pings.average])))

            # But don't go over 200 ms; if it's that high, better to
            # just resync again.  But this does not work well for
            # remote files.  The difference sometimes never gets lower
            # than the maxDifference, so it resyncs forever, and
            # always by the average ping or average slave adjustment,
            # which basically loops doing the same syncs forever.
            # Sigh.
            maxDifference = min(0.200, maxDifference)

        else:
            # This shouldn't happen, but if it does, use 200ms until we
            # have something better.  200ms seems like a lot, and for
            # local files it is, but remote files don't seek as
            # quickly or consistently, so 100ms can cause excessive
            # reseeking in a short period of time
            maxDifference = 0.200

        if len(slave.currentSongAdjustments) > 3:
            # Many adjustments means trouble seeking this song.  Begin
            # increasing the acceptable range by 25ms per adjustment over 3.
            increase_by = 0.025 * (len(slave.currentSongAdjustments) - 3)

            maxDifference += increase_by

            self.log.debug("More than 3 adjustments; increasing max difference by %s", increase_by)

        maxDifference = round(maxDifference, 3)

        self.log.debug("maxDifference for slave %s: %s", slave.host, maxDifference)

        return maxDifference

# ** Functions

def even(num):
    return (num % 2) == 0

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
    parser.add_argument("-v", "--verbose", action="count", dest="verbose", help="Be verbose, up to -vvv")
    args = parser.parse_args()

    # Setup logging
    log = logging.getLogger('mpdsync')
    if args.verbose >= 3:
        # Debug everything, including MPD module.  This sets the root
        # logger, which python-mpd2 uses.  Too bad it doesn't use a
        # logging.NullHandler to make this cleaner.  See
        # https://docs.python.org/2/howto/logging.html#library-config
        LOG_LEVEL = logging.DEBUG
        logging.basicConfig(level=LOG_LEVEL, format="%(levelname)s: %(name)s: %(message)s")

    else:
        # Don't debug MPD.  Don't set the root logger.  Do manually
        # what basicConfig() does, because basicConfig() sets the root
        # logger.  This seems more confusing than it should be.  I
        # think the key is that logging.logger.getChild() is not in
        # the logging howto tutorials.  When I found getChild() (which
        # is in the API docs, which are also not obviously linked in
        # the howto), it started falling into place.  But without
        # getchild(), it was a confusing mess.
        if args.verbose == 1:
            LOG_LEVEL = logging.INFO
        elif args.verbose == 2:
            LOG_LEVEL = logging.DEBUG
        else:
            LOG_LEVEL = logging.WARNING

        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(name)s: %(message)s"))
        log.addHandler(handler)
        log.setLevel(LOG_LEVEL)

    log.debug('Using python-mpd version: %s', str(mpd.VERSION))
    log.debug("Args: %s", args)

    # Check args
    if not args.master:
        log.error("Please provide a master server with -m.")
        return False

    if not args.slaves:
        log.error("Please provide at least one slave server with -c.")
        return False

    # Connect to the master server
    master = Master(host=args.master, password=args.password,
                    adjustLatency=args.adjustLatency, logger=log)

    try:
        master.connect()
    except Exception as e:
        log.exception('Unable to connect to master server: %s', e)
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
        log.debug("Song adjustments for slave 0: %s",
                  "\n".join([str(a)
                             for a in sorted(master.slaves[0].song_adjustments,
                                             key=lambda i: len(i['adjustments']))]))
        log.debug("Song differences for slave 0: %s",
                  "\n".join([str(a)
                             for a in sorted(master.slaves[0].song_differences,
                                             key=lambda i: i['differences'].overall_average)]))

if __name__ == '__main__':
    sys.exit(main())
