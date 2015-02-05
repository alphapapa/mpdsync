#! /usr/bin/which python

# mpdsync.py
# Written by Nick Pegg

# An MPD client which syncs multiple MPD servers, providing they have the same 
# copy of the database. I created this because I needed a way to sync music 
# playback between my living room and my garage

import json
import os
import re
import time
from xdg import BaseDirectory

import mpd  # Requires the python-mpd library


class MPDClient():
    """ Simple wrapper class for mpd.MPDClient to hold server info """
    client = None
    host = None
    port = None
    password = None
    
    plversion = 0   # MPD playlist version. Used to get playlist diffs
    volume_diff = 0 # Difference in volume between this client and the master
    
    
    def __init__(self, host, port=6600, password=None):
        self.client = mpd.MPDClient()
        self.host = host
        self.port = port
        self.password = password
    
    def connect(self):
        ret = True
    
        # Forcibly make sure we aren't connected first
        try:
            self.client.disconnect()
        except:
            pass      
        
        try:
            self.client.connect(self.host, self.port)
            
            if self.password is not None:
                self.client.password(self.password)
        
        except:
            print("Error: Unable to connect to the server")
            print("\tHost: %s, Port: %d" % (self.host, str(self.port)))
            ret = False
            
        return ret
        
    def check_connection(self):
        try:
            self.client.ping()
            return True
        except mpd.ConnectionError:
            return self.connect()            


def main():
    settings = get_settings() 
        
    # Connect to the master and slaves
    m = settings['servers']['master']
    master = MPDClient(m['host'], m['port'], m['password'])
    master.connect()
    print("Connected to master %s:%d" % (m['host'], m['port']))
    
    slaves = list()
    for slave in settings['servers']['slaves']:
        slave_client = MPDClient(slave['host'], slave['port'], slave['password'])
        
        if slave_client.connect():
            print("Connected to slave %s:%d" % (slave['host'], slave['port']))
            slaves.append(slave_client)
               
    full_sync(master, slaves)
    
    # Wait for something to happen and sync the slaves 
    # Note: sync() calls master.idle() which will block until something happens
    # on the master server. No 
    while True:
        # Make sure that the master is alive
        if not master.check_connection():
            print("Error: Lost connection to master and couldn't get it back. Quitting.")
            quit(-2)
                
        sync(master, slaves)
        
    
def get_settings(settings_file=os.path.join(BaseDirectory.xdg_config_home, "mpdsync.json")):
    """ 
    Gets the settings from the settings file (~/.config/mpdsync.json by default)
    and makes sure that everything's kosher.
    """
    
    try:
        settings = json.load(open(settings_file))
    except:
        print("Error: Unable to load settings file. Check that it's there and syntatically correct.")
        print("For your reference, I tried to open " + str(settings_file))
        quit(-1)
        
    if settings.get('servers') is None:
        print("Error: No servers are defined in settings file")
        quit(-1)        
    elif settings['servers'].get('master') is None:
        print("Error: No master server is defined in your settings file!")
        quit(-1)
    elif settings['servers'].get('slaves') is None :
        print("Error: No slave servers are defined in your settings file!")
        quit(-1)
        
    return settings
    
      
def sync(master, slaves):
    """ Syncs the slaves playlists to the master """
    
    # Wait for something to happen
    subsystems = master.client.idle()
    
    # Make sure the clients are ready
    for slave in slaves:
        if not slave.check_connection():
            print("Lost slave %s and was not able to reconnect." % slave.host)
      
    for subsystem in subsystems:
        master_status = master.client.status()
        master.plversion = master_status['playlist']
        
        for slave in slaves:
            if subsystem == 'playlist':
                # Get the difference in playlists
                for change in master.client.plchanges(slave.plversion):
                    slave.client.addid(change['file'], change['pos'])
                
                # Truncate the slave playlist to the same length as the master
                # I had to iterate because python-mpd doesn't support 
                # MPD's range deletion for whatever reason
                m_length = int(master.client.status()['playlistlength'])
                s_length = int(slave.client.status()['playlistlength'])
                
                while m_length != s_length:
                    slave.client.delete(s_length - 1)
                    s_length -= 1
                
                    
                slave.plversion = int(master_status['playlist'])
                
            elif subsystem == 'player':
                sync_player(master, slave)
                
            elif subsystem == 'mixer':
                pass    # Future: raise/lower volume with the master
            
            
def full_sync(master, slaves):
    """ Do a full sync to copy the full state of the master to the slaves """
    
    status = master.client.status()
    print(status)
    
    # Clear the slave playlists and copy the masters
    file_prefix_re = re.compile('^file: ')
    playlist = master.client.playlist()
    print playlist
    for slave in slaves:
        slave.client.clear()

        for song in playlist:
            song = file_prefix_re.sub('', song)
            slave.client.add(song)
            print(song)
            
        # Save this playlist version. We'll use this to determine what changes
        # we need to make when we're out-of-date
        slave.plversion = status['playlist']
        
        sync_player(master, slave) 
        
        slave.volume_diff = int(status['volume']) - int(slave.client.status()['volume'])
        
        print("Synced %s to master %s" % (slave.host, master.host))
            
            
def sync_player(master, slave):
    """ Syncs a slave's player status to the master """
    status = master.client.status()
    
    if status['state'] == 'play':
        slave.client.seek(status['song'], status['time'].split(':')[0])
        slave.client.play()
    elif status['state'] == 'pause':
        slave.client.seek(status['song'], status['time'].split(':')[0])
        
        if slave.client.status()['state'] == 'play':
            slave.client.pause()
    elif status['state'] == 'stop':
        slave.client.stop()
        
   
    
if __name__ == '__main__':
    main()
    
