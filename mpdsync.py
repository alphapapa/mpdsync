#!/usr/bin/env python

# mpdsync.py
# Originally written by Nick Pegg
# Rewritten and updated to use python-mpd2 by Adam Porter <adam@alphapapa.net>

import argparse
import logging as log
import os
import re
import sys
import time

from mpd import MPDClient  # using python-mpd2

# TODO: Verify python-mpd2 is being used

DEFAULT_PORT = 6600
FILE_PREFIX_RE = re.compile('^file: ')

class client(MPDClient):

    def __init__(self, info=None):
        super(client, self).__init__()
        self.host = 'localhost'
        self.port = DEFAULT_PORT
        self.password = None

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

        self.volume = None
        
    def connect(self):
        super(client, self).connect(self.host, self.port)

        if self.password:
            super(client, self).password(self.password)

    def getPlaylist(self):
        self.playlist = super(client, self).playlist()

    def status(self):
        self.currentStatus = super(client, self).status()

        self.playlistLength = int(self.currentStatus['playlistlength'])
        self.playlistVersion = self.currentStatus['playlist']

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

def main():
    global master, slaves

    # Parse args
    parser = argparse.ArgumentParser(
            description='Syncs two mpd servers.')
    parser.add_argument('-m', '--master',
                        dest='master',
                        help='Address and port of master server in HOST:PORT format')
    parser.add_argument('-s', '--slaves',
                        dest="slaves", nargs='*',
                        help='Address and port of slave servers in HOST:PORT format')
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
	
    log.debug("Args: %s" % args)

    # Check args
    if not args.master:
        log.error("Please provide a master server with -m.")
        return False

    if not len(args.slaves) > 0:
        log.error("Please provide at least one slave server with -c.")
        return False

    # Connect to the master server
    master = client()
    if ':' in args.master:
        master.host, master.port = args.master.split(':')
    else:
        master.host = args.master
        
    try:
        master.connect()
    except Exception as e:
        log.error('Unable to connect to master server: %s' % e)
        return False
    
    log.debug('Connected to master server.')

    # Connect to slaves
    slaves = []
    for slave in args.slaves:

        slaveClient = client()
        if ':' in slave:
            slaveClient.host, slaveClient.port = slave.split(':')
        else:
            slaveClient.host = slave
        
        try:
            slaveClient.connect()
        except Exception as e:
            log.error('Unable to connect to slave "%s": %s' % (slave, e))
        else:
            log.debug('Connected to slave "%s".' % slave)
            slaves.append(slaveClient)

    # Sync master and slaves
    syncAll()

    # Enter sync loop
    syncLoop()        
        
def syncLoop():
    global master

    while True:
        # Wait for something to happen
        subsystems = master.idle()

        # Sync stuff
        for subsystem in subsystems:
            if subsystem == 'playlist':
                syncPlaylists()
            elif subsystem == 'player':
                syncPlayers()  
            elif subsystem == 'options':
                syncOptions()

def syncAll():
    syncPlaylists()
    syncOptions()
    syncPlayers()
    
def syncPlaylists():
    global master, slaves

    # Get master info
    master.status()
    master.getPlaylist()

    # Sync slaves
    for slave in slaves:

        if slave.playlistVersion is None:
            # Do a full sync the first time

            # Start command list
            slave.command_list_ok_begin()
            
            # Clear playlist
            slave.clear()

            # Add tracks
            for song in master.playlist:
                slave.add(FILE_PREFIX_RE.sub('', song))

            # Execute command list
            results = slave.command_list_end()

        else:
            # Make changes
            
            # Get current status
            slave.status()

            # Start command list
            slave.command_list_ok_begin()
            
            for change in master.plchanges(slave.playlistVersion):
                log.debug("Making change: %s" % change)
                
                # Add new tracks
                log.debug('Adding to slave:"%s" file:"%s" at pos:%s' % (slave.host, change['file'], change['pos']))
                slave.addid(change['file'], change['pos'])

            # Truncate the slave playlist to the same length as the master
            if master.playlistLength > slave.playlistLength:
                log.debug("Deleting from %s to %s" % (master.playlistLength, slave.playlistLength))
                slave.delete((master.playlistLength, slave.playlistLength))

            # Execute command list
            results = slave.command_list_end()

            # Check result
            slave.status()
            if slave.playlistLength != master.playlistLength:
                log.error("Playlist lengths don't match: %s / %s" % (slave.playlistLength, master.playlistLength))

            # Update slave playlist version number
            slave.playlistVersion = master.playlistVersion

def syncOptions():
    global master, slaves

    pass

def syncPlayers():
    global master, slaves

    # Update master status
    master.status()

    for slave in slaves:
        slave.status()

        if slave.state != master.state:
            if master.playing:
                slave.play()
            elif master.paused:
                slave.pause()
            else:
                slave.stop()

        # Seek to current playing position, adjusted for latency
        if master.playing:
            slave.seek(master.song, master.elapsed + 0.100)
    
if __name__ == '__main__':
    sys.exit(main())
    
