#!/usr/bin/env python

# mpdsync.py
# Originally written by Nick Pegg <https://github.com/nickpegg/mpdsync>
# Rewritten and updated to use python-mpd2 by Adam Porter <adam@alphapapa.net>

import argparse
import logging as log
import os
import re
import sys

import mpd  # Using python-mpd2

# Verify python-mpd2 is being used
if mpd.VERSION < (0, 5, 4):
    print 'ERROR: This script requires python-mpd2 >= 0.5.4.'
    sys.exit(1)

DEFAULT_PORT = 6600
FILE_PREFIX_RE = re.compile('^file: ')

class Client(mpd.MPDClient):

    def __init__(self, host, port=DEFAULT_PORT, password=None):
        super(Client, self).__init__()

        # Split host/port
        if ':' in host:
            host, port = host.split(':')

        self.host = host
        self.port = port
        self.password = password

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

    def getPlaylist(self):
        self.playlist = super(Client, self).playlist()

    def status(self):
        self.currentStatus = super(Client, self).status()

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

        # TODO: Add other attributes, e.g. {'playlistlength': '55',
        # 'playlist': '3868', 'repeat': '0', 'consume': '0',
        # 'mixrampdb': '0.000000', 'random': '0', 'state': 'stop',
        # 'volume': '-1', 'single': '0'}

class Master(Client):

    def __init__(self, host, password=None):
        super(Master, self).__init__(host, password=password)

        self.slaves = []

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

    def syncAll(self):
        self.syncPlaylists()
        self.syncOptions()
        self.syncPlayers()

    def syncOptions(self):
        pass

    def syncPlayers(self):

        # SOMEDAY: do this in parallel?
        for slave in self.slaves:
            self.syncPlayer(slave)

    def syncPlayer(self, slave):
        self.status()

        # Reconnect if connection dropped
        slave.checkConnection()

        # Sync player status
        if self.playing:
            slave.play()

            # Seek to current playing position, adjusted for latency
            slave.seek(self.song, round(self.elapsed + 0.100, 3))
        elif self.paused:
            slave.pause()
        else:
            slave.stop()

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
                slave.command_list_end()

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
                    syncPlayer(slave)

            # Update slave playlist version number to match the master's
            slave.playlistVersion = self.playlistVersion

def main():

    # Parse args
    parser = argparse.ArgumentParser(
            description='Syncs multiple mpd servers.')
    parser.add_argument('-m', '--master',
                        dest='master',
                        help='Name or address of master server, optionally with port in HOST:PORT format')
    parser.add_argument('-s', '--slaves',
                        dest="slaves", nargs='*',
                        help='Name or address of slave servers, optionally with port in HOST:PORT format')
    parser.add_argument('-p', '--password', default=None,
                        dest="password",
                        help='Password to connect to servers with')
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
    master = Master(args.master, password=args.password)

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
