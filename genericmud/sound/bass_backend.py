"""Native audio backend: plays SoundBus cues through BASS, the library soundpacks are made for.

The same contract as the pygame backend: one cue at a time on each logical channel (a new cue
there replaces the one playing), one music stream, live volume and pan changes, and a file
that's missing or unreadable reported once. BASS mixes any number of streams at once, so
unlike pygame there's no pool of channels to share out and no cue is ever cut off for another.

Build it with :func:`make_sound_backend`, which uses BASS when the build has it and an audio
device opens, and pygame otherwise.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from genericmud.sound import bass_library
from genericmud.sound.bass_library import (
    ACTIVE_STOPPED,
    ATTRIB_PAN,
    ATTRIB_VOL,
    SAMPLE_LOOP,
    STREAM_AUTOFREE,
    SYNC_END,
    SYNC_MIXTIME,
)

if TYPE_CHECKING:
    from genericmud.session.diaglog import DiagnosticLog

MUSIC_CATEGORY = "music"

# Callbacks BASS holds for streams that are still playing, by stream handle. ctypes frees a
# callback nothing refers to, and BASS calling a freed one would crash genericMud, so they're
# kept here (one table for every session) until their stream has stopped.
_callbacks: dict[int, object] = {}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class BassSoundBackend:
    """A SoundBus backend over BASS's functions (the loaded library, or a stand-in in tests)."""

    def __init__(
        self,
        bass,
        on_error: Callable[[str], None] | None = None,
        diag: DiagnosticLog | None = None,
    ) -> None:
        self._bass = bass
        self._on_error = on_error
        self._diag = diag  # separate from on_error: every attempt is traced, not just the first
        self._streams: dict[str, int] = {}  # logical channel -> its stream
        self._levels: dict[str, tuple[float, float]] = {}  # logical channel -> (gain, pan)
        self._music = 0  # the music stream, 0 when there's none
        self._music_channel: str | None = None  # the logical channel the music was asked on
        self._warned: set[str] = set()  # files already reported, so a missing cue warns once

    def play(
        self, file: str, channel: str, gain: float, pan: float, loop: bool, times: int = 1
    ) -> None:
        self._free(self._streams.pop(channel, 0))
        repeats = 1 if loop else max(1, times)
        stream = self._open(file, loop=loop, repeats=repeats)
        if not stream:
            self._trace(file, "SKIP", gain=gain)
            return
        self._streams[channel] = stream
        self._levels[channel] = (gain, pan)
        self._set_levels(stream, gain, pan)
        self._start(file, stream, gain=gain)

    def music(self, file: str, channel: str, gain: float, times: int = -1) -> None:
        self._free(self._music)
        self._music = 0
        loop = times < 0  # -1 loops until stopped (soundpacks' background music)
        stream = self._open(file, loop=loop, repeats=1 if loop else max(1, times))
        if not stream:
            self._trace(file, "SKIP", kind="music")
            return
        self._music = stream
        self._music_channel = channel  # so stop() and adjust() on this channel reach it
        self._levels[channel] = (gain, 0.0)
        self._set_levels(stream, gain, 0.0)
        self._start(file, stream, kind="music", gain=gain)

    def stop(self, channel: str) -> None:
        # The music may have been asked for under a channel other than "music"; stop it when
        # either name is asked for, or it plays forever.
        if channel == MUSIC_CATEGORY or channel == self._music_channel:
            self._free(self._music)
            self._music = 0
            if channel == self._music_channel:
                self._music_channel = None
        self._free(self._streams.pop(channel, 0))
        self._levels.pop(channel, None)

    def is_playing(self, channel: str) -> bool:
        """Whether a cue is still playing on this logical channel. Soundpacks decide on it
        (Erion asks whether the old ambience still plays before starting the next)."""
        stream = self._stream_for(channel)
        if not stream:
            return False
        return self._bass.BASS_ChannelIsActive(stream) != ACTIVE_STOPPED

    def adjust(self, channel: str, gain: float | None = None, pan: float | None = None) -> None:
        """Change a playing cue's volume or pan in place (None leaves that one alone)."""
        stream = self._stream_for(channel)
        if not stream:
            return
        current_gain, current_pan = self._levels.get(channel, (1.0, 0.0))
        new_gain = gain if gain is not None else current_gain
        new_pan = pan if pan is not None else current_pan
        self._levels[channel] = (new_gain, new_pan)
        self._set_levels(stream, new_gain, new_pan)

    def _stream_for(self, channel: str) -> int:
        stream = self._streams.get(channel, 0)
        if not stream and (channel == MUSIC_CATEGORY or channel == self._music_channel):
            stream = self._music
        return stream

    def _open(self, file: str, *, loop: bool, repeats: int) -> int:
        """A stream of ``file`` ready to play, or 0 when it can't be opened (reported)."""
        path, flags = bass_library.path_argument(file)
        if loop or repeats > 1:
            flags |= SAMPLE_LOOP
        if not loop:
            flags |= STREAM_AUTOFREE  # a cue that ends frees itself
        stream = self._bass.BASS_StreamCreateFile(False, path, 0, 0, flags)
        if not stream:
            self._trace(file, "OPEN", error=self._bass.BASS_ErrorGetCode())
            self._warn(file)
            return 0
        if repeats > 1:
            self._repeat(stream, repeats)
        return stream

    def _repeat(self, stream: int, repeats: int) -> None:
        """Play ``stream`` ``repeats`` times: it loops, and the loop is switched off as it ends
        for the last time (a MUD's MSP L= or Client.Media loops)."""
        bass = self._bass
        endings = [0]

        def at_end(_sync: int, channel: int, _data: int, _user: object) -> None:
            endings[0] += 1
            if endings[0] >= repeats:
                bass.BASS_ChannelFlags(channel, 0, SAMPLE_LOOP)

        callback = bass_library.SyncProc(at_end)
        self._forget_finished()
        _callbacks[stream] = callback
        bass.BASS_ChannelSetSync(stream, SYNC_END | SYNC_MIXTIME, 0, callback, None)

    def _forget_finished(self) -> None:
        """Let go of the callbacks of streams that have stopped: BASS won't call them again."""
        for stream in list(_callbacks):
            if self._bass.BASS_ChannelIsActive(stream) == ACTIVE_STOPPED:
                del _callbacks[stream]

    def _set_levels(self, stream: int, gain: float, pan: float) -> None:
        self._bass.BASS_ChannelSetAttribute(stream, ATTRIB_VOL, _clamp(gain, 0.0, 1.0))
        self._bass.BASS_ChannelSetAttribute(stream, ATTRIB_PAN, _clamp(pan, -1.0, 1.0))

    def _start(self, file: str, stream: int, **fields: object) -> None:
        if self._bass.BASS_ChannelPlay(stream, False):
            self._trace(file, "OK", **fields)
        else:
            self._trace(file, "EXC", error=self._bass.BASS_ErrorGetCode(), **fields)

    def _free(self, stream: int) -> None:
        if stream:
            self._bass.BASS_StreamFree(stream)  # stops it too; a stream that ended is gone already

    def _trace(self, file: str, result: str, **fields: object) -> None:
        if self._diag is not None:
            self._diag.event("backend.play", file=file, result=result, **fields)

    def _warn(self, file: str) -> None:
        """Report a cue that couldn't play, once per file (a flood would fire every line)."""
        if self._on_error is None or file in self._warned:
            return
        self._warned.add(file)
        self._on_error(
            f"sound not played: {file} -- file missing or unsupported format; "
            "set the world's Sounds folder if your sounds are elsewhere"
        )


def make_bass_backend(
    on_error: Callable[[str], None] | None = None,
    diag: DiagnosticLog | None = None,
) -> BassSoundBackend | None:
    """BASS as a backend, or None when this build has no BASS or no audio device opens.

    No message for the player here: the caller falls back to pygame, which speaks up if it
    can't play either. ``diag`` records why, so a build that fell back can be told apart.
    """
    try:
        bass = bass_library.load()
        bass_library.start(bass)
    except bass_library.BassUnavailable as error:
        if diag is not None:
            diag.event("backend.select", result="none", reason=f"no BASS: {error}")
        return None
    if diag is not None:
        diag.event("backend.select", result="bass", reason=str(bass.folder))
    return BassSoundBackend(bass.core, on_error=on_error, diag=diag)


def make_sound_backend(
    on_error: Callable[[str], None] | None = None,
    diag: DiagnosticLog | None = None,
):
    """The native backend for this session: BASS when it can, pygame when it can't, None
    when neither can play (the caller then posts cues to the renderer)."""
    backend = make_bass_backend(on_error=on_error, diag=diag)
    if backend is not None:
        return backend
    from genericmud.sound.pygame_backend import make_pygame_backend

    return make_pygame_backend(on_error=on_error, diag=diag)
