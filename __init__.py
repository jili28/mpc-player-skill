

import re
from mycroft.skills.core import intent_handler
from mycroft.util.parse import match_one, fuzzy_match
from mycroft.messagebus import Message
from requests import HTTPError
from adapt.intent import IntentBuilder

import time
from os.path import abspath, dirname, join
from subprocess import call, Popen, DEVNULL
import signal
from socket import gethostname
from mycroft.skills.common_play_skill import CommonPlaySkill, CPSMatchLevel
from mycroft.skills.audioservice import AudioService
from mpd import MPDClient
from mpd.base import ConnectionError

class PlaybackError(Exception):
    pass



class PlaylistNotFoundError(Exception):
    pass



# Platforms for which the skill should start the spotify player
MANAGED_PLATFORMS = ['mycroft_mark_1', 'mycroft_mark_2pi']
# Return value definition indication nothing was found
# (confidence None, data None)
NOTHING_FOUND = (None, 0.0)

# Confidence levels for generic play handling
DIRECT_RESPONSE_CONFIDENCE = 0.8

MATCH_CONFIDENCE = 0.5


def best_confidence(title, query):
    """Find best match for a title against a query.
    Some titles include ( Remastered 2016 ) and similar info. This method
    will test the raw title and a version that has been parsed to remove
    such information.
    Arguments:
        title: title name from spotify search
        query: query from user
    Returns:
        (float) best condidence
    """
    best = title.lower()
    best_stripped = re.sub(r'(\(.+\)|-.+)$', '', best).strip()
    return max(fuzzy_match(best, query),
               fuzzy_match(best_stripped, query))

class MpcPlayer(CommonPlaySkill):
    """
        MPD control through MPD client, using only the common play framework Query
        audio backend is ignore
    """
    def __init__(self):
        super(CommonPlaySkill, self).__init__()
        self.is_playing = True
        self.process = None
        self.idle_count = 0
        #enclosure_config = self.config_core.get('enclosure')
        #self.platform = enclosure_config.get('platform', 'unknown')
        self.client = MPDClient()
        self.regexes = {}
        self.ducking = False
        self.last_played_type = None
        self.spoken_name="MPD Player"

    def initialize(self):
        #add handler for non existing MPD server
        super().initialize()
        self.add_event('recognizer_loop:record_begin',
                       self.handle_listener_started)
        self.add_event('recognizer_loop:record_end',
                       self.handle_listener_ended)
        self.add_event('mycroft.audio.service.next', self.next_track)
        self.add_event('mycroft.audio.service.prev', self.prev_track)
        self.add_event('mycroft.audio.service.pause', self.pause)
        self.add_event('mycroft.audio.service.resume', self.resume)
        self.create_intents()
        self.log.info("MPD player skill initialized")
        #should handle not connecting, but for now ok
        self.MPDconnect()
        self.schedule_repeating_event(self.keep_alive, None, 10, name="MPD Keep Alive")

    def keep_alive(self):
        self.client.status()
    ######################################################################
    # Handle auto ducking when listener is started.

    def handle_listener_started(self, message):
        """Handle auto ducking when listener is started.
        The ducking is enabled/disabled using the skill settings on home.
        TODO: Evaluate the Idle check logic
        """
        if (self.client.status()['state'] == 'play' and
                self.settings.get('use_ducking', True)):
            self.pause()
            self.ducking = True

            # Start idle check
            # self.idle_count = 0
            # self.cancel_scheduled_event('IdleCheck')
            # self.schedule_repeating_event(self.check_for_idle, None,
            #                  1, name='IdleCheck')
    def handle_listener_ended(self, message):
        if (self.client.status()['state'] == 'pause' and
                self.settings.get('use_ducking', True)): #by default always use ducking
            self.resume(message)
            self.ducking = True
    # #rewrute to handle listener ended
    # def check_for_idle(self):
    #     """Repeating event checking for end of auto ducking."""
    #     if not self.ducking:
    #         self.cancel_scheduled_event('IdleCheck')
    #         return
    #
    #     active = self.enclosure.display_manager.get_active()
    #     if not active == '' or active == 'MPC-Player-Skill':
    #         # No activity, start to fall asleep
    #         self.idle_count += 1
    #
    #         if self.idle_count >= 5:
    #             # Resume playback after 5 seconds of being idle
    #             self.cancel_scheduled_event('IdleCheck')
    #             self.ducking = False
    #             self.resume()
    #     else:
    #         self.idle_count = 0

    ######################################################################
        ######################################################################
        # Mycroft display handling

    def start_monitor(self):
        """Monitoring and current song display."""
        # Clear any existing event
        self.stop_monitor()

        # Schedule a new one every 5 seconds to monitor/update display
        self.schedule_repeating_event(self._update_display,
                                      None, 5,
                                      name='MonitorSpotify')
        self.add_event('recognizer_loop:record_begin',
                       self.handle_listener_started)

    def stop_monitor(self):
        # Clear any existing event
        self.cancel_scheduled_event('MonitorSpotify')

    def _update_display(self, message):
        # Checks once a second for feedback
        status = self.client.currentsong() if self.client else {}
        self.is_playing = True if status else False

        if not status:
            self.stop_monitor()
            self.mouth_text = None
            self.enclosure.mouth_reset()
            self.disable_playing_intents()
            return

        # Get the current track info
        try:
            artist = status['artist']
        except Exception:
            artist = ''
        try:
            track = status['title']
        except Exception:
            track = ''
        try:
            image = self.client.albumart(status['file']) # might be uri
        except Exception:
            image = ''

        self.CPS_send_status(artist=artist, track=track, image=image)

        # # Mark-1
        # if artist and track:
        #     text = '{}: {}'.format(artist, track)
        # else:
        #     text = ''
        #
        # # Update the "Now Playing" display if needed
        # if text != self.mouth_text:
        #     self.mouth_text = text
        #     self.enclosure.mouth_text(text)

    def translate_regex(self, regex):
        if regex not in self.regexes:
            path = self.find_resource(regex+'.regex')
            with open(path) as f:
                string = f.read().strip()
                self.regexes[regex] = string
            self.log.info("Added regex " + string + " for " + regex)
        return self.regexes[regex]

    def CPS_match_query_phrase(self, phrase):
        """
        responds whether MPD can play the input phrase
        :param phrase input phrase of user
        :return:
        """
        #should check whether MPD available
        # if "iron man" in phrase.lower():
        #     self.log.info("MPD found")
        #     return phrase, CPSMatchLevel.EXACT, {'data': 'Iron Man', 'name': 'Iron Man', 'type': 'playlist'}
        mpd_specified = 'mpd' in phrase
        bonus = 0.1 if mpd_specified else 0.0
        #replaces
        phrase = re.sub(self.translate_regex('on_mpd'), "", phrase, re.IGNORECASE)
        confidence, data = self.continue_playback(phrase, bonus)
        self.log.info("MPD check: " + phrase)
        if not data:
            self.log.info("MPD check for specific query")
            confidence, data = self.specific_query(phrase, bonus)
            if not data:
                self.log.info("MPD check for generic Query")
                confidence, data = self.generic_query(phrase, bonus)

        if data:
            self.log.info('MPD confidence: {}'.format(confidence))
            self.log.info('              data: {}'.format(data))

            if data.get('type') in ['album', 'artist',
                                    'track', 'playlist']:
                if mpd_specified:
                    # " play great song on spotify'
                    level = CPSMatchLevel.EXACT
                else:
                    if confidence > 0.9:
                        # TODO: After 19.02 scoring change
                        # level = CPSMatchLevel.MULTI_KEY
                        level = CPSMatchLevel.TITLE
                    elif confidence < 0.5:
                        level = CPSMatchLevel.GENERIC
                    else:
                        level = CPSMatchLevel.TITLE
                    phrase += ' on mpd'
            elif data.get('type') == 'continue':
                if mpd_specified > 0:
                    # "resume playback on spotify"
                    level = CPSMatchLevel.EXACT
                else:
                    # "resume playback"
                    level = CPSMatchLevel.GENERIC
                    phrase += ' on mpd'
            else:
                self.log.warning('Unexpected mpd type: '
                                 '{}'.format(data.get('type')))
                level = CPSMatchLevel.GENERIC

            return phrase, level, data
        else:
            self.log.debug('Couldn\'t find anything to play on mpd')

    def continue_playback(self, phrase, bonus):
        if phrase.strip() == 'mpd':
            return (1.0,
                    {
                            'data': None,
                            'name': None,
                            'type': 'continue'
                    })
        else:
            return NOTHING_FOUND

    def specific_query(self, phrase, bonus):
        """
            Check if the phrase can be matched against a specific spotify request.
            This includes asking for playlists, albums,
            artists or songs.
            Arguments:
                phrase (str): Text to match against
                bonus (float): Any existing match bonus
            Returns: Tuple with confidence and data or NOTHING_FOUND
        """
            # Check if saved
        #Check if playlist is contained
        # Check if playlist
        match = re.match(self.translate_regex('playlist'), phrase,
                         re.IGNORECASE)
        if match:
            conf, data = self.query_playlist(match.groupdict()['playlist'])
            if conf > 0.7:
                return conf, data
            else:
                return NOTHING_FOUND

        # Check album
        match = re.match(self.translate_regex('album'), phrase,
                         re.IGNORECASE)
        if match:
            bonus += 0.1
            album = match.groupdict()['album']
            return self.query_album(album, bonus)

        # Check artist
        match = re.match(self.translate_regex('artist'), phrase,
                         re.IGNORECASE)
        if match:
            artist = match.groupdict()['artist']
            return self.query_artist(artist, bonus)
        match = re.match(self.translate_regex('song'), phrase,
                         re.IGNORECASE)
        if match:
            song = match.groupdict()['track']
            return self.query_song(song, bonus)

        return NOTHING_FOUND


    def generic_query(self, phrase, bonus):
                """Check for a generic query, not asking for any special feature.
                This will try to parse the entire phrase in the following order
                - As a user playlist
                - As an album
                - As a track
                Arguments:
                    phrase (str): Text to match against
                    bonus (float): Any existing match bonus
                Returns: Tuple with confidence and data or NOTHING_FOUND
                """
                self.log.info('Handling "{}" as a generic query...'.format(phrase))
                results = []

                #check for playlist
                self.log.info('Checking playlists')
                conf, playlistdata = self.query_playlist(phrase)
                #decision
                if conf and conf > DIRECT_RESPONSE_CONFIDENCE:
                    return conf, playlistdata
                elif conf and conf > MATCH_CONFIDENCE:
                    results.append((conf, playlistdata))

                #Check for Artist
                self.log.info('Checking artists')

                conf, data = self.query_artist(phrase)
                if conf and conf > DIRECT_RESPONSE_CONFIDENCE:
                    return conf, data
                elif conf and conf > MATCH_CONFIDENCE:
                    results.append((conf, data))
                #Check for Track
                self.log.info('Checking tracks')
                titles = self.client.list('title')
                if len(titles) > 0:
                    titles = [t['title'] for t in titles]
                    key, conf = match_one(phrase.lower(), titles)
                    #key = titles.index(key)
                    track_data = self.client.search('title', key)
                    data = {'data':track_data[0], 'name': None, 'type': 'track'}
                if conf and conf > DIRECT_RESPONSE_CONFIDENCE:
                    return conf, data
                elif conf and conf > MATCH_CONFIDENCE:
                    results.append((conf, data))

                #Check for album
                self.log.info("Checking albums")
                conf, data = self.query_album(phrase, bonus)
                if conf and conf > DIRECT_RESPONSE_CONFIDENCE:
                    return conf, data
                elif conf and conf > MATCH_CONFIDENCE:
                    results.append((conf, data))

                if len(results) == 0:
                    return NOTHING_FOUND
                else:
                    #return highest confidence value
                    results.reverse()
                    return sorted(results, key=lambda x: x[0])[-1]
        #bonus if MPD was specified
    # @intent_file_handler('player.mpc.intent')
    # def handle_player_mpc(self, message):
    #     self.log.log(20, message.data)
    #     #handle connecting to MPD client
    #     self.speak_dialog('player.mpc')
    #     songs = []
    #     try:
    #         songs =
    #     except Exception as e:
    #         self.log.log(20, e)
    #         self.speak_dialog('play_fail', {"media": intent})
    #     else:
    #         self.audio_service = AudioService(self.bus)
    #         self.audio_service.play(songs, message.data['utterance']

    def query_song(self, song: str, bonus=0.0):
        """
            Try to find song
        :param self:
        :param song:
        :return:
        """
        data = None
        by_word = ' {} '.format(self.translate('by'))
        if len(song.split(by_word)) > 1:
            song, artist = song.split(by_word)
            song_search = song
            #song_search = '*{}* artist:{}'.format(song, artist)
        else:
            song_search = song
        data = self.client.search(type="title", query=song_search)
        if data and len(data) > 0:
            #find best match
            #still to be refined
            titles = [ d['title'] for d in data]
            key, confidence = match_one(song, titles)
            key = titles.index(key)
            #song data is dict containing file uri
            self.log.info("MPD Song: " + song + " matched to" + key + "with conf " + str(confidence))
            return confidence + bonus, {'data': data[key], 'name': None, 'type': 'track'}
        else:
            return NOTHING_FOUND


    def query_playlist(self, phrase: str):
        """

        :param phrase:
        :return:
        """
        playlists = self.client.listplaylists()
        if len(playlists) > 0:
            #names of all playlists
            play = [v['playlist'] for v in playlists]

            key, confidence = match_one(phrase.lower(), play)
            self.log.info("MPD Playlist: " + phrase + " matched to " + key + " with conf" + str(confidence))
            #key = play.index(key)
            playlistdata = self.client.listplaylistinfo(key)

            data = {'data': playlistdata[0], 'name': key, 'type': 'playlist'}
            return confidence, data

        return NOTHING_FOUND


    def query_album(self, album, bonus):
        """Try to find an album.

        Arguments:
            album (str): Album to search for
            bonus (float): Any bonus to apply to the confidence
        Returns: Tuple with confidence and data or NOTHING_FOUND
        """
        data = None
        by_word = ' {} '.format(self.translate('by'))
        if len(album.split(by_word)) > 1:
            album, artist = album.split(by_word)
            album_search = album
            #album_search = '*{}* artist:{}'.format(album, artist)
            #bonus += 0.1
        else:
            album_search = album
        albums = self.client.list('album')
        if len(albums) > 0:
            albumlist = [a['album'] for a in albums]
            key, confidence = match_one(album.lower(), albumlist)
            # key = artist.index(key)
            # artistdata = self.client.search('artist'.key)
            #album returns album name as data
            self.log.info("MPD Album: " + album + " matched to " + key + " with conf " + str(confidence))
            data = {'data': key, 'name': key, 'type': 'album'}
            return confidence, data
        else:
            return NOTHING_FOUND
            # Also check with parentheses removed for example
            # "'Hello Nasty ( Deluxe Version/Remastered 2009" as "Hello Nasty")

    def query_artist(self, artist, bonus=0.0):
        """
        returns best matching artist among available ones
        :param artist: str
        :param bonus: float
        :return:
        """
        bonus += 0.1
        artists = self.client.list('artist')
        if len(artists) > 0:
            #list of artists
            artists = [a['artist'] for a in artists]
            key, confidence = match_one(artist.lower(), artists)
            confidence = min(confidence+bonus, 1.0)
            self.log.info("MPD Artist: " + artist + " matched to " + key + " with conf " + str(confidence))
            #artistdata = self.client.search('artist'.key)
            data = {'data': key, 'name': key, 'type': 'artist'}
            return confidence, data
        else:
            return NOTHING_FOUND


    def filter(self, data, key, value):
        pass

    def CPS_start(self, phrase, data):
        """
        Handler for common play framework
        :param phrase: original utterance
        :param data:
        :return:
        """
        try:
            self.MPDconnect()
            #disable seems to give out a stop signal
            self.enable_playing_intents()
            if data['type'] == 'continue':
                self.acknowledge()
                self.continue_current_playlist()
            elif data['type'] == 'playlist':
                self.start_playlist_playback(data['name'],
                                     data['data'])
            else:  # artist, album track
                self.log.info('playing {}'.format(data['type']))
                self.log.info('by {}'.format(data['name']))
                self.play(data=data['data'], data_type=data['type'], name=data['name'])
            #might have error
            #self.log.info("MPD started playback")
            #self.log.info("MPD post playing intents")
            if data.get('type') and data['type'] != 'continue':
                self.last_played_type = data['type']
                self.is_playing = True
        except Exception as e:
            self.log.error("Error raised while starting playback")
            raise

    def create_intents(self):
        """Setup the spotify intents."""
        # intent = IntentBuilder('').require('Spotify').require('Search') \
        #                           .require('For')
        # self.register_intent(intent, self.search_spotify)
        self.register_intent_file('ShuffleOn.intent', self.shuffle)
        #self.register_intent_file('ShuffleOff.intent', self.shuffle)
        self.register_intent_file('WhatSong.intent', self.song_info)
        self.register_intent_file('WhatAlbum.intent', self.album_info)
        self.register_intent_file('WhatArtist.intent', self.artist_info)
        self.register_intent_file('StopMusic.intent', self.handle_stop)
        time.sleep(0.5)
        self.disable_playing_intents()

    def enable_playing_intents(self):
        self.enable_intent('WhatSong.intent')
        self.enable_intent('WhatAlbum.intent')
        self.enable_intent('WhatArtist.intent')
        self.enable_intent('StopMusic.intent')

    def disable_playing_intents(self):
        self.disable_intent('WhatSong.intent')
        self.disable_intent('WhatAlbum.intent')
        self.disable_intent('WhatArtist.intent')
        self.disable_intent('StopMusic.intent')

    def handle_stop(self):
        if self.client:
            self.client.clear()
        else:
            self.failed()

    def shuffle(self):
        """ Turn on shuffling """
        if self.client:
            self.client.shuffle()
        else:
            self.failed()

    def song_info(self, message):
        """ Speak song info. """
        status = self.client.currentsong() if self.client else None
        song, artist = status['title'], status['artist']
        self.speak_dialog('CurrentSong', {'song': song, 'artist': artist})

    def album_info(self, message):
        """ Speak album info. """
        status = self.client.currentsong() if self.client else None
        album = status['album']
        if self.last_played_type == 'album':
            self.speak_dialog('CurrentAlbum', {'album': album})
        else:
            self.speak_dialog('OnAlbum', {'album': album})

    def artist_info(self, message):
        """ Speak artist info. """
        status = self.client.currentsong() if self.client else None
        if status:
            artist = status['artist']
            self.speak_dialog('CurrentArtist', {'artist': artist})

    def __pause(self):
        # if authorized and playback was started by the skill
        if self.client:
            self.log.info('Pausing MPD')
            if self.client.status()['state'] != 'pause':
                self.client.pause()

    def pause(self, message=None):
        """ Handler for playback control pause. """
        self.ducking = False
        self.__pause()

    def resume(self, message=None):
        """ Handler for playback control resume. """
        if self.client:
            self.log.info('Resume MPD')
            self.client.play()

    def next_track(self, message):
        """ Handler for playback control next. """
        # if authorized and playback was started by the skill
        if self.client:
            self.log.info('Next MPD track')
            self.client.next()
            self.start_monitor()
            return True
        return False

    def prev_track(self, message):
        """ Handler for playback control prev. """
        # if authorized and playback was started by the skill
        if self.client:
            self.log.info('Previous MPD track')
            self.client.prev()
            self.start_monitor()

    def MPDstatus(self):
        self.client.status()
    def MPDconnect(self, host='localhost', port=6600):
        try:
            self.client.connect(host=host, port = port)
        except ConnectionError:
            self.log.error("Already connected")
            pass

    def start_playlist_playback(self, name="", data=None):
        utterance = name.replace('|', ':')
        if data:
            self.speak_dialog('ListeningToPlaylist', data={'playlist': utterance})
            self.client.clear()
            self.client.load(name)
            self.client.play()
        else:
            self.log.info('No playlist found')
            raise PlaylistNotFoundError

    def play(self, data, data_type, name):
        try:
            if data_type == 'track':
                song, artist, uri = data['title'], data['artist'], data['file']
                self.client.clear()
                self.client.add(uri)
                self.client.play()
            elif data_type == 'album':
                self.client.clear()
                self.client.searchadd('album', name)
                self.client.play()
            elif data_type == 'artist':
                self.client.clear()
                self.client.searchadd('artist', name)
                self.client.play()
            else:
                self.log.error("wrong data_type")
                raise ValueError("Invalid Type")
        except Exception as e:
            self.log.error("Unable to obtain name, artist or"
                           " URI information while asked to play: " + str(e))

    def continue_current_playlist(self):
        pass

    def failed(self):
        pass


def create_skill():
    return MpcPlayer()

