"""Stand-in for the BASS wrapper class MUSHclient soundpacks ship, played through genericMud's
sound bus.

Miriani-Next, Toastush, Chatpack and the Yu-Gi-Oh pack each carry the same Lua wrapper around
bass.dll: a BASS class whose StreamCreateFile hands back Channel objects. It's written for
LuaJIT's ffi, which genericMud's Lua doesn't have, so genericMud answers ``require`` of it with
this stand-in: the same calls and the same answers (BASS's error codes, and Channel fields like
length, position and frequency), with every stream a cue on the sound bus, so the player's
volume settings and the stop-all key reach it too.

Packs keep house by these answers. Miriani-Next frees every stream whose IsActive says it has
stopped and watches GetPosition to spot a stuck device, and the Yu-Gi-Oh pack times its queue of
sounds by their length; a stand-in that said "playing" for ever, at position 0, broke both.

What would take a pack outside its own folder, or run its code, is refused the way BASS refuses
things: a URL stream answers "can't open", a plugin (Chatpack's bass_aac) gets a pretend handle
since BASS here decodes AAC itself where the system can, and an EAX environment answers "no EAX".

Effects go on for real. Chatpack's AddFX(effect, settings) puts BASS's DirectX 8 effects on a
stream by the wrapper's names for their settings (left_delay, wet_dry_mix), and ChatMUD's GMCP
Client.Media.FX reaches it as addFx, lower case, which the wrapper doesn't have, so on Windows it
fails; here it does what ChatMUD meant. A setting the effect doesn't have is passed over without
the wrapper's printed list of the ones it does (ChatMUD's GMCP always sends one, the sound's key).
An effect stays on its stream when the stream is played again, as in BASS.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from genericmud.sound import bass_effects

# BASS's numbers, from bass.h.
_OK = 0
_ERROR_FILEOPEN = 2
_ERROR_HANDLE = 5
_ERROR_ILLTYPE = 19
_ERROR_ILLPARAM = 20
_ERROR_NOEAX = 22
_ERROR_NOFX = 34
_ATTRIB_FREQ = 1
_ATTRIB_VOL = 2
_ATTRIB_PAN = 3
_SAMPLE_8BITS = 1
_SAMPLE_LOOP = 4
_SAMPLE_FLOAT = 0x100
_STREAM_AUTOFREE = 0x40000
_POS_BYTE = 0
_ACTIVE_STOPPED = 0
_ACTIVE_PAUSED = 3
_CTYPE_STREAM = 0x10000
_VERSION = 0x02041203  # BASS 2.4.18.3, the one genericMud carries
_PLUGIN_HANDLE = 1  # what PluginLoad answers: a plugin "loaded", since none is ever run

# Attribute names, as Toastush's older wrapper takes them (its BASS:SetAttribute(stream, "volume",
# 0.5) looks the name up in its constants).
_ATTRIBUTES = {"frequency": _ATTRIB_FREQ, "volume": _ATTRIB_VOL, "pan": _ATTRIB_PAN}
# The wrapper's names for each effect's settings (Chatpack's fxparameters, which ChatMUD's GMCP
# uses too), and BASS's.
_MODULATION_SETTINGS = {
    "wet_dry_mix": "fWetDryMix", "depth": "fDepth", "feedback": "fFeedback",
    "frequency": "fFrequency", "waveform": "lWaveform", "delay": "fDelay", "phase": "lPhase",
}
_EFFECT_SETTINGS = {
    bass_effects.DX8_CHORUS: _MODULATION_SETTINGS,
    bass_effects.DX8_COMPRESSOR: {
        "gain": "fGain", "attack": "fAttack", "release": "fRelease",
        "threshold": "fThreshold", "ratio": "fRatio", "predelay": "fPredelay",
    },
    bass_effects.DX8_DISTORTION: {
        "gain": "fGain", "edge": "fEdge", "post_eq_center_frequency": "fPostEQCenterFrequency",
        "post_eq_bandwidth": "fPostEQBandwidth", "pre_lowpass_cutoff": "fPreLowpassCutoff",
    },
    bass_effects.DX8_ECHO: {
        "wet_dry_mix": "fWetDryMix", "feedback": "fFeedback", "left_delay": "fLeftDelay",
        "right_delay": "fRightDelay", "pan_delay": "lPanDelay",
    },
    bass_effects.DX8_FLANGER: _MODULATION_SETTINGS,
    bass_effects.DX8_GARGLE: {"rate_hz": "dwRateHz", "wave_shape": "dwWaveShape"},
    bass_effects.DX8_I3DL2REVERB: {
        "room": "lRoom", "room_hf": "lRoomHF", "room_rolloff_factor": "flRoomRolloffFactor",
        "decay_time": "flDecayTime", "decay_hf_ratio": "flDecayHFRatio",
        "reflections": "lReflections", "reflections_delay": "flReflectionsDelay",
        "reverb": "lReverb", "reverb_delay": "flReverbDelay", "diffusion": "flDiffusion",
        "density": "flDensity", "hf_reference": "flHFReference",
    },
    bass_effects.DX8_PARAMEQ: {"center": "fCenter", "bandwidth": "fBandwidth", "gain": "fGain"},
    bass_effects.DX8_REVERB: {
        "in_gain": "fInGain", "reverb_mix": "fReverbMix", "reverb_time": "fReverbTime",
        "high_frequency_rt_ratio": "fHighFreqRTRatio",
    },
}
# Past this many, streams that can't be playing are dropped from the list BASS:Free() stops.
_KEPT_STREAMS = 256
# What a stream whose file couldn't be measured is taken to hold: CD-quality stereo.
_DEFAULT_RATE = 44100
_DEFAULT_CHANNELS = 2

# Builds a Lua object whose methods are Python callables and whose other fields are read and
# written through Python, the way the wrapper's Channel class serves length and position. The
# methods are the object's own fields, not looked up through its metatable: Toastush builds its
# own Audio class on the BASS class with Penlight's class(), which copies the fields it finds.
_MAKE_OBJECT = """
function(methods, read, write)
  local object = {}
  for name, method in pairs(methods) do
    object[name] = method
  end
  return setmetatable(object, {
    __index = function(_object, key)
      local method = methods[key]
      if method ~= nil then
        return method
      end
      return read(key)
    end,
    __newindex = function(object, key, value)
      if not write(key, value) then
        rawset(object, key, value)
      end
    end,
    __call = function(object)
      return object
    end,
  })
end
"""


def _nothing(*_args) -> int:
    """What any call the wrapper has and this stand-in doesn't model answers: BASS's "fine"."""
    return _OK


@dataclass
class _SoundData:
    """What a stream's GetData hands a pack, to put into another stream: the Yu-Gi-Oh pack reads
    its life-points sound this way and puts it into a new stream as many times as it should play.
    It's a reference to the file rather than its samples, which is all that use needs."""

    file: str


@dataclass(eq=False)  # each one is itself, whatever its settings
class _StreamEffect:
    """An effect a pack put on a stream, kept so it goes on again each time the stream plays."""

    kind: int
    priority: int
    settings: dict[str, float] = field(default_factory=dict)  # by BASS's names, as last set
    handle: int = 0  # the effect on the cue playing now, 0 before the stream has played


@dataclass(eq=False)  # each one is itself, whatever its settings
class _Stream:
    """One stream a pack created, and the cue it plays as."""

    number: int  # the handle the pack sees
    channel: str  # the sound bus channel it plays on
    file: str  # as the pack named it ("" for a stream data is put into)
    flags: int
    seconds: float
    rate: int
    channels: int
    gain: float = 1.0
    pan: float = 0.0
    frequency: float = 0.0  # the rate set by the pack; 0 is the file's own
    repeats: int = 1  # a stream data is put into plays the data this many times
    started: bool = False
    freed: bool = False
    generation: int = 0  # which BASS:Free() it was made after
    effects: list[_StreamEffect] = field(default_factory=list)

    @property
    def bytes_per_second(self) -> int:
        return self.rate * self.channels * self.sample_bytes

    @property
    def sample_bytes(self) -> int:
        if self.flags & _SAMPLE_FLOAT:
            return 4
        if self.flags & _SAMPLE_8BITS:
            return 1
        return 2


@dataclass
class BassWrapper:
    """The BASS class and its streams for one pack, over its ScriptApi."""

    lua: object  # the pack's Lua runtime
    api: object  # its ScriptApi
    to_number: Callable[[object], float | None]
    streams: list[_Stream] = field(default_factory=list)  # the ones that may be playing
    next_number: int = 1
    generation: int = 0  # goes up at each BASS:Free(), which frees every stream made before

    def build(self):
        """The BASS object the pack's ``require`` gets: callable, as the wrapper's class is."""
        self._make = self.lua.eval(_MAKE_OBJECT)
        methods = self.lua.table_from({
            "Init": lambda *_a: _OK,
            "Start": lambda *_a: _OK,
            "Reinit": lambda *_a: _OK,
            "Free": self._free_all,
            "IsInitialized": lambda *_a: True,
            # Miriani-Next checks the device before every sound and recovers it on an error;
            # genericMud's own device handling does that job.
            "CheckDeviceHealth": lambda *_a: _OK,
            "RecoverFromDeviceFailure": lambda *_a: (True, "Recovered"),
            "GetConfig": lambda *_a: 0,
            "SetConfig": lambda *_a: _OK,
            "GetVersion": lambda *_a: _VERSION,
            "StreamCreateFile": self._create_file,
            "StreamCreateUrl": lambda *_a: _ERROR_FILEOPEN,  # packs never reach the network
            "StreamCreateURL": lambda *_a: _ERROR_FILEOPEN,
            "StreamCreate": self._create_push,
            "PluginLoad": lambda *_a: _PLUGIN_HANDLE,
            "SetEnvironment": lambda *_a: _ERROR_NOEAX,
            # Not the wrapper's, but older genericMud answered it: set a stream's attribute.
            "SetAttribute": self._set_attribute_of,
        })
        return self._make(methods, lambda _key: _nothing, lambda _key, _value: False)

    # --- the BASS class ---

    def _create_file(self, _bass, _memory=False, file="", _offset=0, _length=0, flags=0):
        # Flags left out are 0, as Miriani-Next's, Chatpack's and the Yu-Gi-Oh pack's wrappers
        # have it; Toastush's older one makes them auto-free, which only changes what happens
        # to a stream played again after it ends, and Toastush makes a new one each time.
        name = str(file or "")
        if not name or not self.api.sound_exists(name):
            return _ERROR_FILEOPEN  # missing, or outside the pack's folders
        info = self.api.source_info(name)
        if info is None:
            # There, but nothing here can measure it (the web renderer plays sounds, or the
            # backend can't read the format): it plays as it can, its length unknown.
            stream = self._add(name, flags, 0.0, _DEFAULT_RATE, _DEFAULT_CHANNELS)
        else:
            stream = self._add(name, flags, info.seconds, info.rate, info.channels)
        return self._object(stream)

    def _create_push(self, _bass, frequency=0, channels=0, flags=0):
        rate = int(self.to_number(frequency) or _DEFAULT_RATE)
        count = int(self.to_number(channels) or _DEFAULT_CHANNELS)
        stream = self._add("", flags, 0.0, rate, count)
        return self._object(stream)

    def _add(self, file: str, flags: object, seconds: float, rate: int, channels: int) -> _Stream:
        self._forget_ended()
        number = self.next_number
        self.next_number += 1
        stream = _Stream(
            number=number,
            channel=f"mush-bass-{number}",
            file=file,
            flags=int(self.to_number(flags) or 0),
            seconds=seconds,
            rate=rate or _DEFAULT_RATE,
            channels=channels or _DEFAULT_CHANNELS,
            generation=self.generation,
        )
        self.streams.append(stream)
        return stream

    def _forget_ended(self) -> None:
        """Drop the streams that can't be playing from the list BASS:Free() stops, once it
        grows: Miriani-Next makes a stream for every sound. A pack still holding one of them
        uses it as before, and BASS:Free() still ends it (see generation)."""
        if len(self.streams) <= _KEPT_STREAMS:
            return
        self.streams = [
            stream for stream in self.streams
            if not self._gone(stream)
            and (not stream.started or self._state(stream) != _ACTIVE_STOPPED)
        ]

    def _gone(self, stream: _Stream) -> bool:
        """Whether BASS would have freed the stream: by itself, or at a BASS:Free() since."""
        return stream.freed or stream.generation != self.generation

    def _free_all(self, *_args) -> int:
        for stream in self.streams:
            if not self._gone(stream):
                self.api.stop(stream.channel)
                stream.freed = True
        self.streams.clear()
        self.generation += 1  # and every stream made before is gone with them
        return _OK

    def _set_attribute_of(self, _bass, stream=None, attribute=0, value=0):
        if stream is None or isinstance(stream, (int, float)):
            return _OK  # an error code where a stream should be: nothing to set
        setter = stream["SetAttribute"]
        return setter(stream, attribute, value)

    def _attribute(self, attribute) -> int:
        """An attribute as BASS numbers it, given its number or its name."""
        if isinstance(attribute, str) and attribute.lower() in _ATTRIBUTES:
            return _ATTRIBUTES[attribute.lower()]
        return int(self.to_number(attribute) or 0)

    # --- a stream ---

    def _object(self, stream: _Stream):
        def call(action):
            def method(_object, *args):
                if self._gone(stream):
                    return _ERROR_HANDLE
                return action(stream, *args)

            return method

        methods = self.lua.table_from({
            "Play": call(self._play),
            "Pause": call(self._pause),
            "Stop": call(self._stop),
            "Free": lambda _object, *_args: self._free(stream),
            "IsActive": lambda _object, *_args: self._state(stream),
            "SetAttribute": call(self._set_attribute),
            "GetAttribute": call(self._get_attribute),
            "SlideAttribute": call(self._slide_attribute),
            "IsSliding": lambda *_a: 0,
            "Flags": call(self._flags),
            "GetLength": call(self._length_bytes),
            "GetPosition": call(self._position_bytes),
            "SetPosition": call(self._seek_bytes),
            "Bytes2Seconds": lambda _object, count=0: self._seconds(stream, count),
            "Seconds2Bytes": lambda _object, seconds=0: self._bytes(stream, seconds),
            "GetInfo": lambda *_a: _OK,
            "GetData": lambda *_a: _SoundData(stream.file),
            "PutData": call(self._put_data),
            "Update": lambda *_a: _OK,
            "SetFX": call(self._set_fx),
            "AddFX": call(self._add_fx),
            "addFx": call(self._add_fx),  # ChatMUD's spelling; see the module's notes
            "FXReset": call(self._reset_effects),
            "OnReachedEnd": lambda *_a: None,
        })

        def read(key):
            fields = {
                "id": lambda: stream.number,
                "length": lambda: stream.seconds * stream.repeats,
                "position": lambda: self.api.sound_position(stream.channel),
                "frequency": lambda: stream.rate,
                "channels": lambda: stream.channels,
                "flags": lambda: stream.flags,
                "channel_type": lambda: _CTYPE_STREAM,
                "original_resolution": lambda: stream.sample_bytes * 8,
            }
            getter = fields.get(key)
            return getter() if getter is not None else _nothing

        def write(key, value):
            if key != "position":
                return False
            seconds = self.to_number(value)
            if seconds is not None and not self._gone(stream):
                self.api.seek(stream.channel, seconds)
            return True

        return self._make(methods, read, write)

    def _play(self, stream: _Stream, restart=False) -> int:
        state = self.api.sound_state(stream.channel)
        if state == _ACTIVE_PAUSED:
            self.api.resume(stream.channel)
            return _OK
        if state != _ACTIVE_STOPPED:
            if restart:
                self.api.seek(stream.channel, 0.0)
            return _OK
        if stream.started and stream.flags & _STREAM_AUTOFREE:
            # BASS frees a stream like this when it ends; playing it again is an error there.
            stream.freed = True
            return _ERROR_HANDLE
        if not stream.file:
            return _OK  # nothing has been put into it yet
        stream.started = True
        if stream not in self.streams:
            self.streams.append(stream)  # it left the list once it ended; BASS:Free() stops it
        self.api.play(
            stream.file, channel=stream.channel, gain=stream.gain, pan=stream.pan,
            loop=bool(stream.flags & _SAMPLE_LOOP), times=stream.repeats,
        )
        if stream.frequency:
            self.api.adjust(stream.channel, frequency=stream.frequency)
        for effect in stream.effects:  # a new cue: its effects go on again
            effect.handle = self.api.add_effect(
                stream.channel, effect.kind, effect.settings or None, effect.priority
            )
        return _OK

    def _pause(self, stream: _Stream, *_args) -> int:
        self.api.pause(stream.channel)
        return _OK

    def _stop(self, stream: _Stream, *_args) -> int:
        self.api.stop(stream.channel)
        if stream.flags & _STREAM_AUTOFREE:
            stream.freed = True  # BASS frees a stream like this when it stops, as when it ends
        return _OK

    def _free(self, stream: _Stream) -> int:
        if self._gone(stream):
            return _ERROR_HANDLE
        self.api.stop(stream.channel)
        stream.freed = True
        return _OK

    def _state(self, stream: _Stream) -> int:
        """IsActive: 0 stopped, 1 playing, 3 paused, as BASS answers it."""
        if self._gone(stream) or not stream.started:
            return _ACTIVE_STOPPED
        return self.api.sound_state(stream.channel)

    def _set_attribute(self, stream: _Stream, attribute=0, value=0) -> int:
        number = self._attribute(attribute)
        amount = self.to_number(value)
        if amount is None:
            return _OK
        if number == _ATTRIB_VOL:
            stream.gain = amount
            if stream.started:
                self.api.adjust(stream.channel, gain=amount)
        elif number == _ATTRIB_PAN:
            stream.pan = amount
            if stream.started:
                self.api.adjust(stream.channel, pan=amount)
        elif number == _ATTRIB_FREQ:
            stream.frequency = amount
            if stream.started:
                self.api.adjust(stream.channel, frequency=amount)
        return _OK

    def _get_attribute(self, stream: _Stream, attribute=0):
        number = self._attribute(attribute)
        if number == _ATTRIB_VOL:
            return stream.gain
        if number == _ATTRIB_PAN:
            return stream.pan
        if number == _ATTRIB_FREQ:
            # Packs pitch a sound from its own rate (rate * 1.2), so before any change this is
            # the file's real rate.
            return stream.frequency or stream.rate
        return 0

    def _slide_attribute(self, stream: _Stream, attribute=0, value=0, milliseconds=0) -> int:
        number = self._attribute(attribute)
        amount = self.to_number(value)
        seconds = (self.to_number(milliseconds) or 0.0) / 1000
        if amount is None:
            return _OK
        if not stream.started:
            return self._set_attribute(stream, number, max(amount, 0.0))
        if number == _ATTRIB_VOL:
            if amount < 0:
                # BASS's slide to -1: fade out, then stop.
                self.api.fade_away(stream.channel, seconds)
                return _OK
            stream.gain = amount
            self.api.slide(stream.channel, gain=amount, seconds=seconds)
        elif number == _ATTRIB_PAN:
            stream.pan = amount
            self.api.slide(stream.channel, pan=amount, seconds=seconds)
        elif number == _ATTRIB_FREQ:
            stream.frequency = amount
            self.api.slide(stream.channel, frequency=amount, seconds=seconds)
        return _OK

    def _flags(self, stream: _Stream, flags=0, mask=0) -> int:
        wanted = int(self.to_number(flags) or 0)
        which = int(self.to_number(mask) or 0)
        changed = (stream.flags & ~which) | (wanted & which)
        if which & _SAMPLE_LOOP and stream.started:
            self.api.set_looping(stream.channel, bool(changed & _SAMPLE_LOOP))
        stream.flags = changed
        return changed

    # --- effects ---

    def _new_effect(self, stream: _Stream, effect, priority) -> _StreamEffect | int:
        """Put an effect on ``stream``: on its cue now when it has one, else when it plays.
        Answers the effect, or BASS's error code when it can't be had."""
        kind = self.to_number(effect)
        if kind is None or int(kind) not in bass_effects.SETTINGS:
            return _ERROR_ILLTYPE
        entry = _StreamEffect(kind=int(kind), priority=int(self.to_number(priority) or 0))
        if stream.started:
            entry.handle = self.api.add_effect(stream.channel, entry.kind, None, entry.priority)
            if not entry.handle:
                if self._state(stream) == _ACTIVE_STOPPED:
                    return _ERROR_HANDLE  # it has ended, and BASS has freed it
                return _ERROR_NOFX
            entry.settings = self.api.effect_settings(stream.channel, entry.handle) or {}
        stream.effects.append(entry)
        return entry

    def _set_fx(self, stream: _Stream, effect=None, priority=0):
        entry = self._new_effect(stream, effect, priority)
        if isinstance(entry, int):
            return entry
        return self._effect_object(stream, entry)

    def _add_fx(self, stream: _Stream, effect=None, options=None, *_args) -> None:
        """Chatpack's AddFX: put on an effect with the settings named, by the wrapper's names in
        any case."""
        entry = self._new_effect(stream, effect, 0)
        if isinstance(entry, int):
            return
        self._change_effect(stream, entry, self._bass_settings(entry.kind, options))

    def _bass_settings(self, kind: int, table) -> dict[str, float]:
        """An effect's settings from a Lua table by the wrapper's names, in any case, as BASS
        names them. Names the effect doesn't have, and values that aren't numbers, are left
        out."""
        names = _EFFECT_SETTINGS[kind]
        chosen = {}
        if table is None or not hasattr(table, "items"):
            return chosen
        for key, value in table.items():
            name = names.get(str(key).lower())
            amount = value if isinstance(value, bool) else self.to_number(value)
            if name is not None and amount is not None:
                chosen[name] = amount
        return chosen

    def _change_effect(self, stream: _Stream, entry: _StreamEffect, settings: dict) -> int:
        """New settings for an effect, by BASS's names. Answers BASS's code for fine, or for an
        illegal parameter when it refuses one, which leaves them all as they were."""
        if not settings:
            return _OK
        if not entry.handle:
            entry.settings.update(settings)  # BASS checks them when the stream plays
            return _OK
        if not self.api.set_effect(stream.channel, entry.handle, settings):
            return _ERROR_ILLPARAM
        entry.settings = self.api.effect_settings(stream.channel, entry.handle) or {
            **entry.settings, **settings
        }
        return _OK

    def _reset_effects(self, stream: _Stream, *_args) -> int:
        self.api.reset_effects(stream.channel)
        return _OK

    def _effect_object(self, stream: _Stream, entry: _StreamEffect):
        """The object SetFX answers: its Parameters by the wrapper's names, which Update puts
        on, and Remove and Reset."""
        names = _EFFECT_SETTINGS[entry.kind]
        parameters = self.lua.table()

        def show() -> None:
            for wrapper_name, bass_name in names.items():
                parameters[wrapper_name] = entry.settings.get(bass_name, 0)

        def set_parameters(_object, table=None, *_args) -> int:
            return self._change_effect(stream, entry, self._bass_settings(entry.kind, table))

        def get_parameters(_object, *_args) -> int:
            if entry.handle:
                entry.settings = (
                    self.api.effect_settings(stream.channel, entry.handle) or entry.settings
                )
            show()
            return _OK

        def update(_object, *_args) -> int:
            chosen = self._bass_settings(entry.kind, parameters)
            result = self._change_effect(stream, entry, chosen)
            get_parameters(_object)
            return result

        def remove(_object, *_args) -> int:
            if entry in stream.effects:
                stream.effects.remove(entry)
            if entry.handle:
                self.api.remove_effect(stream.channel, entry.handle)
                entry.handle = 0
            return _OK

        def reset(_object, *_args) -> int:
            if entry.handle:
                self.api.reset_effects(stream.channel, entry.handle)
            return _OK

        show()
        methods = self.lua.table_from({
            "SetParameters": set_parameters,
            "GetParameters": get_parameters,
            "Update": update,
            "Remove": remove,
            "Reset": reset,
        })

        def read(key):
            fields = {
                "id": lambda: entry.handle,
                "channel": lambda: stream.number,
                "Parameters": lambda: parameters,
            }
            getter = fields.get(key)
            return getter() if getter is not None else _nothing

        return self._make(methods, read, lambda _key, _value: False)

    def _length_bytes(self, stream: _Stream, *_args) -> int:
        return self._bytes(stream, stream.seconds * stream.repeats)

    def _position_bytes(self, stream: _Stream, *_args) -> int:
        if not stream.started:
            return 0
        return self._bytes(stream, self.api.sound_position(stream.channel))

    def _seek_bytes(self, stream: _Stream, position=0, mode=_POS_BYTE) -> int:
        if int(self.to_number(mode) or 0) == _POS_BYTE:
            self.api.seek(stream.channel, self._seconds(stream, position))
        return _OK

    def _put_data(self, stream: _Stream, data=None, *_args) -> int:
        if not isinstance(data, _SoundData) or not data.file:
            return 0
        if not stream.file:
            stream.file = data.file
            stream.repeats = 1
            info = self.api.source_info(data.file)
            if info is not None:
                stream.seconds = info.seconds
        elif data.file == stream.file:
            stream.repeats += 1
        return _OK

    def _bytes(self, stream: _Stream, seconds) -> int:
        """Seconds as the stream's bytes, whole samples on every channel, as BASS counts them."""
        amount = self.to_number(seconds) or 0.0
        block = stream.channels * stream.sample_bytes
        samples = int(amount * stream.rate)
        return max(0, samples * block)

    def _seconds(self, stream: _Stream, count) -> float:
        amount = self.to_number(count) or 0.0
        return max(0.0, amount / stream.bytes_per_second)
