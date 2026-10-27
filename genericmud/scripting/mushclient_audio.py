"""Stand-in for audio.dll, the small BASS front end that Erion, Empire, Mystavaria, Cosmic Rage,
SquareSoft, V.I., Krinnzenn's pack and Mush-Z play every sound through.

Each of those packs ships the same 17 KB audio.dll and loads it from its LuaAudio plugin with
package.loadlib. genericMud never runs a pack's DLLs, so the ``audio`` table they get is this
stand-in, written from the DLL's own code so that a pack sounds the way it does on Windows:

- Values come first and ids after them: volume(x, id), pan(x, id), pitch(x, id),
  slideVol(x, id, ms), slidePan(x, id, ms), slidePitch(x, id, ms) and fadeout(id, ms).
- Volume and pan are percentages (100 is full; -100 is hard left), times are milliseconds (a
  second when left out), and every number is read as a whole one, rounded, as the DLL reads them.
- pitch(x, id) plays a sound at its own rate plus x hundred Hz (Cosmic Rage turns the MUD's Hz
  into these steps); slidePitch(x, id, ms) glides x hundred Hz from where the sound is.
- fadeout(id, ms) fades a sound to silence and stops it looping. It plays out silently to the end
  of its pass, so isPlaying answers 1 until then, as on Windows.
- volume(x) with no id is the pack's master volume: what every later sound without a volume of
  its own plays at, and what every sound still playing is set to, apart from ones at silence.
- BASS refuses a pan past -100 or 100, and so does this: Cosmic Rage plays with pan -10000 to mean
  "none given", and those sounds stay centred.
- A call naming id 0, or a sound that has ended, does nothing; stop(0) and free() stop every sound
  the pack started here, and nothing else in the session.
- play answers 0 for a file that isn't there. playDelay and playDelayLooped answer nothing and
  play once: the DLL never sets the loop flag for either.

Where the DLL slips in a way no pack relies on, this does what was meant: getVolume(id) answers
that sound's volume (the DLL reads whichever sound it touched last), pitch starts from the sound's
own rate (the DLL's from the last one played), pan(x) with no id pans later sounds (the DLL rounds
it to nothing), and the delayed plays start at the master volume (the DLL at 1% until volume() is
first called). One difference stays: genericMud never plays a sound louder than full, where BASS
would amplify a volume over 100.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

CHANNEL_PREFIX = "erion-audio-"  # one bus channel per sound, so stop(id) reaches just that one
_STOPPED = 0  # BASS_ChannelIsActive's answers
_PLAYING = 1
_FULL = 100  # volume and pan are percentages
_PITCH_STEP = 100  # Hz for each step of pitch() and slidePitch()
_SLIDE_MILLISECONDS = 1000  # a slide's or a fade's length when the pack leaves it out
_LOOPED_DELAY_MILLISECONDS = 250  # playDelayLooped's wait when the pack leaves it out
_DEFAULT_RATE = 44100  # what a file nothing could measure is taken to play at
_KEPT_SOUNDS = 512  # past this many, even looping sounds that have stopped are forgotten


@dataclass
class _Sound:
    """One sound a pack started, and what the stand-in knows of it."""

    number: int  # the id the pack sees
    channel: str  # the sound bus channel it plays on
    file: str
    level: float  # the volume it was last set or slid to, 1.0 for full
    looping: bool
    rate: float | None = None  # the file's own sample rate, measured when first needed
    frequency: float | None = None  # the rate it was last pitched to


@dataclass
class AudioDll:
    """audio.dll for one pack, over its ScriptApi."""

    lua: object  # the pack's Lua runtime
    api: object  # its ScriptApi
    to_number: Callable[[object], float | None]
    sounds: dict[int, _Sound] = field(default_factory=dict)
    master: float = 1.0  # volume() with no id, 1.0 for 100
    default_pan: float = 0.0  # pan() with no id
    next_number: int = 1

    def build(self):
        """The ``audio`` table the pack's LuaAudio plugin calls into. Anything else on it is a
        function that does nothing, so a call to one the DLL doesn't have (LuaAudio's own hook
        calls audio.freq) can't stop the pack's script."""
        audio = self.lua.eval(
            "setmetatable({}, {__index = function() return function() end end})"
        )
        audio.play = self._play
        audio.playDelay = self._play_delay
        audio.playDelayLooped = self._play_delay_looped
        audio.stop = self._stop
        audio.free = self._free
        audio.isPlaying = self._is_playing
        audio.volume = self._volume
        audio.getVolume = self._get_volume
        audio.pan = self._pan
        audio.pitch = self._pitch
        audio.slideVol = self._slide_volume
        audio.slidePan = self._slide_pan
        audio.slidePitch = self._slide_pitch
        audio.fadeout = self._fadeout
        # Not the DLL's: Toastush once reached its own BASS object through a global named
        # audio, and set it up with these before anything else had made it.
        const = self.lua.table()
        device = self.lua.table()
        device.stereo = 0x8000
        const.device = device
        audio.const = const
        audio.Init = lambda *_args: 0
        return audio

    # --- playing ---

    def _play(self, file=None, loop=None, pan=None, volume=None, *_rest) -> int:
        name = "" if file is None else str(file)
        if not name or not self.api.sound_exists(name):
            return 0  # BASS couldn't open it, and the DLL answers 0
        level = self._new_level(volume, self.master)
        balance = self._new_pan(pan, self.default_pan)
        looping = self._whole(loop) not in (None, 0)
        sound = self._add(name, level, looping)
        self.api.play(name, channel=sound.channel, gain=level, pan=balance, loop=looping)
        return sound.number

    def _play_delay(self, file=None, milliseconds=None, pan=None, volume=None, *_rest) -> None:
        self._play_later(file, self._whole(milliseconds) or 0, pan, volume)

    def _play_delay_looped(
        self, file=None, milliseconds=None, pan=None, volume=None, *_rest
    ) -> None:
        wait = self._whole(milliseconds)
        self._play_later(file, _LOOPED_DELAY_MILLISECONDS if wait is None else wait, pan, volume)

    def _play_later(self, file, milliseconds: int, pan, volume) -> None:
        """Play ``file`` once after ``milliseconds``, at the pan and volume asked for now."""
        name = "" if file is None else str(file)
        if not name:
            return
        level = self._new_level(volume, self.master)
        balance = self._new_pan(pan, 0.0)

        def start() -> None:
            if not self.api.sound_exists(name):
                return  # BASS couldn't open it, and nothing plays
            sound = self._add(name, level, looping=False)
            self.api.play(name, channel=sound.channel, gain=level, pan=balance, loop=False)

        if milliseconds > 0:
            self.api.add_timer(milliseconds / 1000, start)
        else:
            start()

    def _stop(self, number=None, *_rest) -> None:
        which = self._whole(number)
        if which is None:
            return  # the DLL refuses a missing id; it doesn't mean "stop them all"
        if which == 0:
            for sound in list(self.sounds.values()):
                self._end(sound)
            return
        sound = self.sounds.get(which)
        if sound is not None:
            self._end(sound)

    def _free(self, *_rest) -> None:
        for sound in list(self.sounds.values()):
            self._end(sound)
        self.sounds.clear()

    def _is_playing(self, number=None, *_rest) -> int:
        sound = self._sound(number)
        return 1 if sound is not None and self._state(sound) == _PLAYING else 0

    # --- volume and pan ---

    def _volume(self, amount=None, number=None, *_rest) -> None:
        percent = self._whole(amount)
        if percent is None or percent < 0:
            return  # BASS refuses a volume below silence
        level = percent / _FULL
        which = self._whole(number) or 0
        if which:
            sound = self.sounds.get(which)
            if sound is not None:
                sound.level = level
                self.api.adjust(sound.channel, gain=level)
            return
        self.master = level
        for sound in list(self.sounds.values()):
            if sound.level in (0.0, level) or self._state(sound) != _PLAYING:
                continue
            sound.level = level
            self.api.adjust(sound.channel, gain=level)

    def _get_volume(self, number=None, *_rest) -> float:
        which = self._whole(number) or 0
        if not which:
            return self.master * _FULL
        sound = self.sounds.get(which)
        return sound.level * _FULL if sound is not None else 0.0

    def _pan(self, amount=None, number=None, *_rest) -> None:
        balance = self._balance(amount)
        if balance is None:
            return
        which = self._whole(number) or 0
        if not which:
            self.default_pan = balance
            return
        sound = self.sounds.get(which)
        if sound is not None:
            self.api.adjust(sound.channel, pan=balance)

    # --- pitch ---

    def _pitch(self, amount=None, number=None, *_rest) -> None:
        steps = self._whole(amount)
        sound = self._sound(number)
        if steps is None or sound is None:
            return
        frequency = self._rate(sound) + steps * _PITCH_STEP
        if frequency < 0:
            return  # BASS refuses a rate below nothing
        sound.frequency = frequency or self._rate(sound)  # 0 is the file's own rate to BASS
        self.api.adjust(sound.channel, frequency=frequency)

    # --- slides ---

    def _slide_volume(self, amount=None, number=None, milliseconds=None, *_rest) -> None:
        percent = self._whole(amount)
        sound = self._sound(number)
        if percent is None or sound is None:
            return
        seconds = self._seconds(milliseconds)
        if percent < 0:
            # BASS fades a sound sliding below silence away, and stops it at the end.
            sound.level = 0.0
            sound.looping = False
            self.api.fade_away(sound.channel, seconds)
            return
        sound.level = percent / _FULL
        self.api.slide(sound.channel, gain=sound.level, seconds=seconds)

    def _slide_pan(self, amount=None, number=None, milliseconds=None, *_rest) -> None:
        balance = self._balance(amount)
        sound = self._sound(number)
        if balance is None or sound is None:
            return
        self.api.slide(sound.channel, pan=balance, seconds=self._seconds(milliseconds))

    def _slide_pitch(self, amount=None, number=None, milliseconds=None, *_rest) -> None:
        steps = self._whole(amount)
        sound = self._sound(number)
        if steps is None or sound is None:
            return
        start = sound.frequency if sound.frequency is not None else self._rate(sound)
        frequency = start + steps * _PITCH_STEP
        if frequency < 0:
            return
        sound.frequency = frequency or self._rate(sound)
        self.api.slide(sound.channel, frequency=frequency, seconds=self._seconds(milliseconds))

    def _fadeout(self, number=None, milliseconds=None, *_rest) -> None:
        sound = self._sound(number)
        if sound is None:
            return
        sound.level = 0.0
        sound.looping = False
        self.api.slide(sound.channel, gain=0.0, seconds=self._seconds(milliseconds))
        self.api.set_looping(sound.channel, False)

    # --- keeping track ---

    def _add(self, file: str, level: float, looping: bool) -> _Sound:
        """Record a sound starting now, dropping first the ones that have ended, delayed
        sounds' included."""
        self._forget_ended()
        number = self.next_number
        self.next_number += 1
        sound = _Sound(
            number=number, channel=f"{CHANNEL_PREFIX}{number}", file=file, level=level,
            looping=looping,
        )
        self.sounds[number] = sound
        return sound

    def _end(self, sound: _Sound) -> None:
        """Stop a sound. It's asked of the bus whatever state the sound is in: genericMud's web
        page plays sounds where nothing here can tell whether they're still going."""
        sound.looping = False
        self.api.stop(sound.channel)

    def _forget_ended(self) -> None:
        """Drop the sounds that have ended, as the DLL does each time it plays one. A looping
        sound is kept until it's stopped, since the web page can't say when one is playing."""
        crowded = len(self.sounds) > _KEPT_SOUNDS
        for number, sound in list(self.sounds.items()):
            if (crowded or not sound.looping) and self._state(sound) == _STOPPED:
                del self.sounds[number]

    def _sound(self, number) -> _Sound | None:
        which = self._whole(number)
        return self.sounds.get(which) if which else None

    def _state(self, sound: _Sound) -> int:
        return self.api.sound_state(sound.channel)

    def _rate(self, sound: _Sound) -> float:
        """The file's own sample rate, which pitch() counts from."""
        if sound.rate is None:
            info = self.api.source_info(sound.file)
            sound.rate = float(info.rate) if info is not None and info.rate else _DEFAULT_RATE
        return sound.rate

    # --- reading numbers as the DLL does ---

    def _whole(self, value) -> int | None:
        """A number as the DLL reads one: whole, rounded half to even as Lua's conversion does
        on Windows, or None when it isn't a number."""
        number = self.to_number(value)
        if number is None or not math.isfinite(number):
            return None
        return round(number)

    def _new_level(self, volume, default: float) -> float:
        """The volume a new sound starts at: the percentage asked for, or ``default``."""
        percent = self._whole(volume)
        if percent is None:
            return default
        if percent < 0:
            return 1.0  # BASS refuses it, and a new stream keeps its own full volume
        return percent / _FULL

    def _new_pan(self, pan, default: float) -> float:
        if self._whole(pan) is None:
            return default
        balance = self._balance(pan)
        return 0.0 if balance is None else balance  # refused: a new stream stays centred

    def _balance(self, pan) -> float | None:
        """A pan percentage as BASS takes it, or None when BASS would refuse it."""
        percent = self._whole(pan)
        if percent is None or abs(percent) > _FULL:
            return None
        return percent / _FULL

    def _seconds(self, milliseconds) -> float:
        amount = self._whole(milliseconds)
        if amount is None:
            amount = _SLIDE_MILLISECONDS
        return max(amount, 0) / 1000
