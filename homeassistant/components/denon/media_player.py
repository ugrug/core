"""Support for Denon Network Receivers."""
from __future__ import annotations

import logging
import telnetlib

import voluptuous as vol

from homeassistant.components.media_player import PLATFORM_SCHEMA, MediaPlayerEntity
from homeassistant.components.media_player.const import (
    SUPPORT_NEXT_TRACK,
    SUPPORT_PAUSE,
    SUPPORT_PLAY,
    SUPPORT_PREVIOUS_TRACK,
    SUPPORT_SELECT_SOURCE,
    SUPPORT_STOP,
    SUPPORT_TURN_OFF,
    SUPPORT_TURN_ON,
    SUPPORT_VOLUME_MUTE,
    SUPPORT_VOLUME_SET,
)
from homeassistant.const import CONF_HOST, CONF_NAME, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

#ugrug
#for queue
from homeassistant.helpers.event import track_time_interval
from datetime import datetime, timedelta, timezone
import asyncio
from collections import deque
import async_timeout
#ugrug end

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Music station"

SUPPORT_DENON = (
    SUPPORT_VOLUME_SET
    | SUPPORT_VOLUME_MUTE
    | SUPPORT_TURN_ON
    | SUPPORT_TURN_OFF
    | SUPPORT_SELECT_SOURCE
)
SUPPORT_MEDIA_MODES = (
    SUPPORT_PAUSE
    | SUPPORT_STOP
    | SUPPORT_PREVIOUS_TRACK
    | SUPPORT_NEXT_TRACK
    | SUPPORT_PLAY
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)

NORMAL_INPUTS = {
    "Cd": "CD",
    "Dvd": "DVD",
    "Blue ray": "BD",
    "TV": "TV",
    "Satellite / Cable": "SAT/CBL",
    "Game": "GAME",
    "Game2": "GAME2",
    "Video Aux": "V.AUX",
    "Dock": "DOCK",
}

MEDIA_MODES = {
    "Tuner": "TUNER",
    "Media server": "SERVER",
    "Ipod dock": "IPOD",
    "Net/USB": "NET/USB",
    "Rapsody": "RHAPSODY",
    "Napster": "NAPSTER",
    "Pandora": "PANDORA",
    "LastFM": "LASTFM",
    "Flickr": "FLICKR",
    "Favorites": "FAVORITES",
    "Internet Radio": "IRADIO",
    "USB/IPOD": "USB/IPOD",
}

# Sub-modes of 'NET/USB'
# {'USB': 'USB', 'iPod Direct': 'IPD', 'Internet Radio': 'IRP',
#  'Favorites': 'FVP'}

UPDATE_INTERVAL = 1
TELNET_CLOSE_TIMEOUT = 3
TELNET_CLOSE_DELAY = 1

def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Denon platform."""
    denon = DenonDevice(hass, config[CONF_NAME], config[CONF_HOST])

    if denon.update():
        add_entities([denon])


class DenonDevice(MediaPlayerEntity):
    """Representation of a Denon device."""

    def __init__(self, hass, name, host):
        """Initialize the Denon device."""
        self.hass = hass
        self._name = name
        self._host = host
        self._pwstate = "PWSTANDBY"
        self._volume = 0
        # Initial value 60dB, changed if we get a MVMAX
        self._volume_max = 60
        self._source_list = NORMAL_INPUTS.copy()
        self._source_list.update(MEDIA_MODES)
        self._muted = False
        self._mediasource = ""
        self._mediainfo = ""

        self._should_setup_sources = True

        _LOGGER.info('Init ' + str(self.name) + ' [' + str(host) + ']')

        self.telnet_progress_set = set()
        self.telnet_progress = None
        self.telnet_command_queue = deque()

        self.telnet = None
        self.telnet_is_open = False
        self.telnet_last_active = None
        self.telnet_last_error = None

        self.first_update = True

        self.executing_command = False
        self.executing_update = False

        track_time_interval(
          self.hass, self.update_periodic, timedelta(seconds=UPDATE_INTERVAL)
        )

    def _setup_sources(self):
        # NSFRN - Network name
        nsfrn = self.telnet_request(self.telnet, "NSFRN ?")[len("NSFRN ") :]
        if nsfrn:
            self._name = nsfrn

        # SSFUN - Configured sources with (optional) names
        self._source_list = {}
        for line in self.telnet_request(self.telnet, "SSFUN ?", all_lines=True):
            ssfun = line[len("SSFUN") :].split(" ", 1)

            source = ssfun[0]
            if len(ssfun) == 2 and ssfun[1]:
                configured_name = ssfun[1]
            else:
                # No name configured, reusing the source name
                configured_name = source

            self._source_list[configured_name] = source

        # SSSOD - Deleted sources
        for line in self.telnet_request(self.telnet, "SSSOD ?", all_lines=True):
            source, status = line[len("SSSOD") :].split(" ", 1)
            if status == "DEL":
                for pretty_name, name in self._source_list.items():
                    if source == name:
                        del self._source_list[pretty_name]
                        break

    @classmethod
    def telnet_request(cls, telnet, command, all_lines=False):
        """Execute `command` and return the response."""

        _LOGGER.debug("Sending: %s", command)
        try:
          telnet.write(command.encode("ASCII") + b"\r")
          lines = []
          while True:
            line = telnet.read_until(b"\r", timeout=0.2)
            if not line:
              break
            lines.append(line.decode("ASCII").strip())
            _LOGGER.debug("Received: %s", line)

        except ConnectionResetError as e:
          _LOGGER.error("ConnectionResetError, cannot request %s [%s]", command.encode("ASCII"), str(e))
          return ""

        except BrokenPipeError as e:
          _LOGGER.error("BrokenPipeError, cannot request %s [%s]", command.encode("ASCII"), str(e))
          return ""

        if all_lines:
            return lines
        return lines[0] if lines else ""

    def telnet_command(self, command):
        """Establish a telnet connection and sends `command`."""
        kwargs = {"command":command}
        self.telnet_command_queue.append(kwargs)
        self.telnet_progress_set.add("command")
        if self.telnet_progress is None:
          self.hass.add_job(self.async_task_process())

    #ugrug
    def telnet_open(self):
        success = True
        if not self.telnet_is_open:
          success = False
          try:
            _LOGGER.debug("Telnet open")
            self.telnet = telnetlib.Telnet(self._host)
            self.telnet_is_open = True
            success = True
            self.telnet_last_active = datetime.now()
            self.telnet_last_error = None
          except OSError as e:
            self.telnet_last_active = None
            self.telnet_last_error = datetime.now()
            _LOGGER.error("OSError: %s", str(e))
          except:
            self.telnet_last_active = None
            self.telnet_last_error = datetime.now()
            _LOGGER.error("Unknown error")
        return success

    def telnet_close(self):
        success = True
        if self.telnet_is_open:
          success = False
          try:
            _LOGGER.debug("Telnet close")
            self.telnet.close()
            self.telnet = None
            self.telnet_is_open = False
            success = True
          except OSError as e:
            _LOGGER.error("OSError: %s", str(e))
          except:
            _LOGGER.error("Unknown error")
          self.telnet_last_active = None
          self.telnet_last_error = datetime.now()
        return success

    def telnet_command_execute(self, command):
        """Establish a telnet connection and sends `command`."""
        success = False
        try:
          _LOGGER.debug("Sending: %s", command)
          progress = self.telnet_open()
          if progress:
            self.telnet.write(command.encode("ASCII") + b"\r")
            self.telnet.read_very_eager()  # skip response
            self.telnet_last_active = datetime.now()
            self.telnet_last_error = None
          success = progress
        except OSError as e:
          self.telnet_last_active = None
          self.telnet_last_error = datetime.now()
          _LOGGER.error("OSError: %s [%s]", command, str(e))
        except:
          self.telnet_last_active = None
          self.telnet_last_error = datetime.now()
          _LOGGER.error("Unknown error: %s", command)
        self.executing_command = False
        return success

    def update_periodic(self, now):
        if self.telnet_progress is not None:
          return
        if len(self.telnet_progress_set):
          self.hass.add_job(self.async_task_process())
        else:
          if (self.telnet_last_active is None) or \
            ((not (self.telnet_last_active is None)) and ((datetime.now() - self.telnet_last_active).total_seconds() >= TELNET_CLOSE_TIMEOUT)):
              self.telnet_close()

    def update(self):
        if self.first_update:
          self.first_update = False
          success = self.update_execute()
          return success
        else:
          self.telnet_progress_set.add('update')
          if self.telnet_progress is None:
            self.hass.add_job(self.async_task_process())

    def update_execute(self):
        """Get the latest details from the device."""
        success = False
        try:
            progress = self.telnet_open()
            if progress:

                if self._should_setup_sources:
                    self._setup_sources()
                    self._should_setup_sources = False

                self._pwstate = self.telnet_request(self.telnet, "PW?")
                for line in self.telnet_request(self.telnet, "MV?", all_lines=True):
                    if line.startswith("MVMAX "):
                        # only grab two digit max, don't care about any half digit
                        self._volume_max = int(line[len("MVMAX ") : len("MVMAX XX")])
                        continue
                    if line.startswith("MV"):
                        self._volume = int(line[len("MV") :])
                self._muted = self.telnet_request(self.telnet, "MU?") == "MUON"
                self._mediasource = self.telnet_request(self.telnet, "SI?")[len("SI") :]

                if self._mediasource in MEDIA_MODES.values():
                    self._mediainfo = ""
                    answer_codes = [
                        "NSE0",
                        "NSE1X",
                        "NSE2X",
                        "NSE3X",
                        "NSE4",
                        "NSE5",
                        "NSE6",
                        "NSE7",
                        "NSE8",
                    ]
                    for line in self.telnet_request(self.telnet, "NSE", all_lines=True):
                        self._mediainfo += f"{line[len(answer_codes.pop(0)) :]}\n"
                else:
                    self._mediainfo = self.source

                self.telnet_last_active = datetime.now()

            success = progress
        except OSError as e:
            self.telnet_last_active = None
            _LOGGER.error("OSError, Error during update %s", str(e))
        except:
            self.telnet_last_active = None
            _LOGGER.error("Unknown error")
        self.executing_update = False
        return success

    async def async_task_process(self):
        if self.telnet_progress is not None:
          return
        if ((not (self.telnet_last_error is None)) and ((datetime.now() - self.telnet_last_error).total_seconds() < TELNET_CLOSE_DELAY)):
          return
        lock = asyncio.Lock()
        async with lock:
          self.telnet_progress = True
          finished = False
          error = False
          kwargs = None
          try:
            with async_timeout.timeout(5):
              while (not finished) and (not error):
                if "update" in self.telnet_progress_set:
                  self.executing_update = True
                  success = await self.hass.async_add_executor_job(self.update_execute)
                  while self.executing_update:
                    time.sleep(0.1)
                  if success:
                    self.telnet_progress_set.remove("update")
                  else:
                    error = True
                if "command" in self.telnet_progress_set:
                  if len(self.telnet_command_queue) > 0:
                    kwargs = self.telnet_command_queue.popleft()
                    command = kwargs["command"]
                    self.executing_command = True
                    success = await self.hass.async_add_executor_job(self.telnet_command_execute, command)
                    while self.executing_command:
                      time.sleep(0.1)
                    if success:
                      kwargs = None
                    else:
                      error = True
                  if len(self.telnet_command_queue) == 0:
                    self.telnet_progress_set.remove("command")
                if len(self.telnet_progress_set) == 0:
                  finished = True
          except asyncio.TimeoutError:
            _LOGGER.error("Timeout")
          except Exception as e:
            _LOGGER.error("Error " + str(e) + " [" + str(type(e)) + "]")
          except:
            _LOGGER.error("Unknown error")
          finally:
            if kwargs is not None:
              self.telnet_command_queue.appendleft(kwargs)
            self.telnet_progress = None
        return

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def state(self):
        """Return the state of the device."""
        if self._pwstate == "PWSTANDBY":
            return STATE_OFF
        if self._pwstate == "PWON":
            return STATE_ON

        return None

    @property
    def volume_level(self):
        """Volume level of the media player (0..1)."""
        return self._volume / self._volume_max

    @property
    def is_volume_muted(self):
        """Return boolean if volume is currently muted."""
        return self._muted

    @property
    def source_list(self):
        """Return the list of available input sources."""
        return sorted(self._source_list)

    @property
    def media_title(self):
        """Return the current media info."""
        return self._mediainfo

    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        if self._mediasource in MEDIA_MODES.values():
            return SUPPORT_DENON | SUPPORT_MEDIA_MODES
        return SUPPORT_DENON

    @property
    def source(self):
        """Return the current input source."""
        for pretty_name, name in self._source_list.items():
            if self._mediasource == name:
                return pretty_name

    def turn_off(self):
        """Turn off media player."""
        self.telnet_command("PWSTANDBY")

    def volume_up(self):
        """Volume up media player."""
        self.telnet_command("MVUP")

    def volume_down(self):
        """Volume down media player."""
        self.telnet_command("MVDOWN")

    def set_volume_level(self, volume):
        """Set volume level, range 0..1."""
        self.telnet_command(f"MV{round(volume * self._volume_max):02}")

    def mute_volume(self, mute):
        """Mute (true) or unmute (false) media player."""
        mute_status = "ON" if mute else "OFF"
        self.telnet_command(f"MU{mute_status})")

    def media_play(self):
        """Play media player."""
        self.telnet_command("NS9A")

    def media_pause(self):
        """Pause media player."""
        self.telnet_command("NS9B")

    def media_stop(self):
        """Pause media player."""
        self.telnet_command("NS9C")

    def media_next_track(self):
        """Send the next track command."""
        self.telnet_command("NS9D")

    def media_previous_track(self):
        """Send the previous track command."""
        self.telnet_command("NS9E")

    def turn_on(self):
        """Turn the media player on."""
        self.telnet_command("PWON")

    def select_source(self, source):
        """Select input source."""
        self.telnet_command(f"SI{self._source_list.get(source)}")
