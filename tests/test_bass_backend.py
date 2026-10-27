"""The BASS audio backend: what it asks BASS to do, against a stand-in, and real BASS when the
library is here (GENERICMUD_BASS_DIR names its folder; tools/fetch_bass.py fills one)."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import pytest

from genericmud.sound import bass_backend, bass_library
from genericmud.sound.bass_backend import BassSoundBackend
from genericmud.sound.bass_effects import (
    BFX_VOLUME_ENV,
    DX8_CHORUS,
    DX8_COMPRESSOR,
    DX8_DISTORTION,
    DX8_ECHO,
    DX8_FLANGER,
    DX8_GARGLE,
    DX8_PARAMEQ,
    DX8_REVERB,
)
from genericmud.sound.bass_library import (
    ACTIVE_PLAYING,
    ACTIVE_STOPPED,
    ATTRIB_FREQ,
    ATTRIB_PAN,
    ATTRIB_VOL,
    POS_BYTE,
    POS_END,
    POS_LOOP,
    SAMPLE_LOOP,
    STREAM_AUTOFREE,
    SYNC_END,
    SYNC_FREE,
    SYNC_MIXTIME,
)
from genericmud.sound.bus import CueTiming, SoundBus
from tests.helpers import RecordingDiag

_BYTES_PER_SECOND = 1000  # the stand-in's files: a thousand bytes a second, ten seconds long
_LENGTH = 10 * _BYTES_PER_SECOND


class _FakeBass:
    """BASS's functions as the backend calls them, keeping track of every stream."""

    def __init__(
        self, missing: tuple[str, ...] = (), missing_effects: tuple[int, ...] = (),
        reuse_handles: bool = False,
    ) -> None:
        self.missing = missing  # files that "don't exist"
        self.missing_effects = missing_effects  # effects BASS lacks on this "platform"
        self.reuse_handles = reuse_handles  # hand a freed stream's number to the next one
        self.freed_handles: list[int] = []
        self.streams: dict[int, dict] = {}
        self.effects: dict[int, dict] = {}  # by effect handle
        self.next_handle = 100
        self.error = 0

    def BASS_StreamCreateFile(self, _memory, path, _offset, _length, flags):  # noqa: N802
        name = path.value if isinstance(path, ctypes.c_wchar_p) else path.value.decode()
        if name in self.missing:
            self.error = 2  # BASS_ERROR_FILEOPEN
            return 0
        if self.reuse_handles and self.freed_handles:
            handle = self.freed_handles.pop(0)
        else:
            self.next_handle += 1
            handle = self.next_handle
        self.streams[handle] = {
            "file": name, "flags": flags, "playing": False, "attributes": {}, "syncs": [],
            "positions": {}, "slides": [], "position": 0,
        }
        return handle

    def BASS_ChannelGetLength(self, _stream, _mode):  # noqa: N802
        return _LENGTH

    def BASS_ChannelGetPosition(self, stream, _mode):  # noqa: N802
        return self.streams[stream]["position"]

    def BASS_ChannelSetPosition(self, stream, position, mode):  # noqa: N802
        self.streams[stream]["positions"][mode] = position
        if mode == POS_BYTE:
            self.streams[stream]["position"] = position
        return 1

    def BASS_ChannelSeconds2Bytes(self, _stream, seconds):  # noqa: N802
        return round(seconds * _BYTES_PER_SECOND)

    def BASS_ChannelBytes2Seconds(self, _stream, count):  # noqa: N802
        return count / _BYTES_PER_SECOND

    def BASS_ChannelSlideAttribute(self, stream, attribute, value, milliseconds):  # noqa: N802
        self.streams[stream]["slides"].append((attribute, value, milliseconds))
        return 1

    def BASS_ErrorGetCode(self):  # noqa: N802
        return self.error

    def BASS_ChannelPlay(self, stream, _restart):  # noqa: N802
        self.streams[stream]["playing"] = True
        self.streams[stream]["paused"] = False
        return 1

    def BASS_ChannelPause(self, stream):  # noqa: N802
        self.streams[stream]["paused"] = True
        return 1

    def BASS_ChannelGetInfo(self, stream, reference):  # noqa: N802
        if stream not in self.streams:
            return 0
        reference._obj.freq = 22050
        reference._obj.chans = 1
        return 1

    def BASS_StreamFree(self, stream):  # noqa: N802
        return 1 if self._free_stream(stream) else 0

    def _free_stream(self, stream: int) -> bool:
        """BASS frees a stream: its free syncs run, then it's gone, with its effects."""
        state = self.streams.get(stream)
        if state is None:
            return False
        for kind, _parameter, callback, user in state["syncs"]:
            if kind & ~SYNC_MIXTIME == SYNC_FREE:
                callback(1, stream, 0, user)
        del self.streams[stream]
        for effect, effect_state in list(self.effects.items()):
            if effect_state["stream"] == stream:
                del self.effects[effect]
        self.freed_handles.append(stream)
        return True

    def BASS_ChannelSetFX(self, stream, kind, _priority):  # noqa: N802
        if stream not in self.streams:
            self.error = 5  # BASS_ERROR_HANDLE
            return 0
        if kind in self.missing_effects:
            self.error = 34  # BASS_ERROR_NOFX
            return 0
        self.next_handle += 1
        self.effects[self.next_handle] = {
            "stream": stream, "kind": kind, "settings": {}, "resets": 0,
        }
        return self.next_handle

    def BASS_FXSetParameters(self, effect, reference):  # noqa: N802
        state = self.effects.get(effect)
        if state is None:
            return 0
        structure = reference._obj
        values = {name: getattr(structure, name) for name, _type in structure._fields_}
        # DirectX's echo takes delays from 1 to 2000 ms, and refuses the lot for one outside.
        delays = [values.get(name, 1) for name in ("fLeftDelay", "fRightDelay")]
        if state["kind"] == 3 and not all(1 <= delay <= 2000 for delay in delays):
            self.error = 20  # BASS_ERROR_ILLPARAM
            return 0
        state["settings"] = values
        return 1

    def BASS_FXGetParameters(self, effect, reference):  # noqa: N802
        state = self.effects.get(effect)
        if state is None:
            return 0
        for name, value in state["settings"].items():
            setattr(reference._obj, name, value)
        return 1

    def BASS_ChannelRemoveFX(self, _stream, effect):  # noqa: N802
        return 1 if self.effects.pop(effect, None) is not None else 0

    def BASS_FXReset(self, handle):  # noqa: N802
        for effect, state in self.effects.items():
            if handle in (effect, state["stream"]):
                state["resets"] += 1
        return 1

    def BASS_ChannelIsActive(self, stream):  # noqa: N802
        state = self.streams.get(stream)
        if state and state.get("paused"):
            return 3  # BASS_ACTIVE_PAUSED
        return ACTIVE_PLAYING if state and state["playing"] else ACTIVE_STOPPED

    def BASS_ChannelSetAttribute(self, stream, attribute, value):  # noqa: N802
        self.streams[stream]["attributes"][attribute] = value
        return 1

    def BASS_ChannelFlags(self, stream, flags, mask):  # noqa: N802
        state = self.streams[stream]
        state["flags"] = (state["flags"] & ~mask) | (flags & mask)
        return state["flags"]

    def BASS_ChannelSetSync(self, stream, kind, parameter, callback, user):  # noqa: N802
        self.streams[stream]["syncs"].append((kind, parameter, callback, user))
        return 1

    def reach(self, stream: int, position: int) -> None:
        """The stream plays past ``position``: its position syncs there run."""
        for kind, parameter, callback, user in self.streams[stream]["syncs"]:
            if kind & ~SYNC_MIXTIME == 0 and parameter == position:
                callback(1, stream, 0, user)

    def end_pass(self, stream: int) -> None:
        """The stream reaches its end: its end syncs run, and it stops unless it loops."""
        state = self.streams[stream]
        for kind, _parameter, callback, user in state["syncs"]:
            if kind & SYNC_END:
                callback(1, stream, 0, user)
        if not state["flags"] & SAMPLE_LOOP:
            state["playing"] = False
            if state["flags"] & STREAM_AUTOFREE:
                self._free_stream(stream)

    def finish_slides(self, stream: int) -> None:
        """The stream's slides end: one of the volume to -1 stops it, which frees it when
        it's AUTOFREE."""
        state = self.streams[stream]
        stopping = [value for attribute, value, _ms in state["slides"] if attribute == ATTRIB_VOL]
        if stopping and stopping[-1] < 0:
            state["playing"] = False
            if state["flags"] & STREAM_AUTOFREE:
                self._free_stream(stream)

    def playing_files(self) -> list[str]:
        return [state["file"] for state in self.streams.values() if state["playing"]]


def _backend(**kwargs):
    fake = _FakeBass(**kwargs)
    errors: list[str] = []
    backend = BassSoundBackend(fake, on_error=errors.append, diag=RecordingDiag())
    return fake, backend, errors


def test_a_cue_plays_at_its_volume_and_pan_and_frees_itself_when_done():
    fake, backend, _errors = _backend()
    backend.play("/sounds/hit.ogg", "combat", 0.5, -0.25, False)
    [(stream, state)] = fake.streams.items()
    assert state["playing"] and state["attributes"] == {ATTRIB_VOL: 0.5, ATTRIB_PAN: -0.25}
    assert state["flags"] & STREAM_AUTOFREE and not state["flags"] & SAMPLE_LOOP
    assert backend.is_playing("combat")
    fake.end_pass(stream)
    assert not backend.is_playing("combat")
    assert fake.streams == {}


def test_a_new_cue_on_a_channel_replaces_the_one_playing_there_and_no_other():
    fake, backend, _errors = _backend()
    backend.play("/sounds/rain.ogg", "ambience", 1.0, 0.0, True)
    backend.play("/sounds/step.ogg", "steps", 1.0, 0.0, False)
    backend.play("/sounds/wind.ogg", "ambience", 1.0, 0.0, True)
    assert sorted(fake.playing_files()) == ["/sounds/step.ogg", "/sounds/wind.ogg"]
    looping = [state for state in fake.streams.values() if state["file"] == "/sounds/wind.ogg"]
    # It goes round until told not to, and frees itself once it stops.
    assert looping[0]["flags"] & SAMPLE_LOOP and looping[0]["flags"] & STREAM_AUTOFREE


def test_a_cue_asked_to_play_three_times_plays_three_times():
    # A MUD's MSP L=3, or Client.Media loops 3.
    fake, backend, _errors = _backend()
    backend.play("/sounds/bell.ogg", "bell", 1.0, 0.0, False, times=3)
    [stream] = fake.streams
    fake.end_pass(stream)
    fake.end_pass(stream)
    assert backend.is_playing("bell")  # into its third time
    fake.end_pass(stream)
    assert not backend.is_playing("bell")


def test_music_has_one_stream_that_its_own_channel_name_stops():
    fake, backend, _errors = _backend()
    backend.music("/music/town.ogg", "area-music", 0.8)
    backend.music("/music/battle.ogg", "area-music", 0.8)
    assert fake.playing_files() == ["/music/battle.ogg"]
    [state] = fake.streams.values()
    assert state["flags"] & SAMPLE_LOOP  # music loops until stopped
    assert backend.is_playing("music") and backend.is_playing("area-music")
    backend.adjust("area-music", gain=0.3)
    assert state["attributes"][ATTRIB_VOL] == 0.3
    backend.stop("area-music")
    assert fake.streams == {} and not backend.is_playing("music")


def test_adjust_changes_one_axis_and_keeps_the_other():
    fake, backend, _errors = _backend()
    backend.play("/sounds/engine.ogg", "engine", 0.6, 0.5, True)
    backend.adjust("engine", pan=-1.0)
    backend.adjust("engine", gain=2.0)  # beyond full volume: held at full, as pygame does
    [state] = fake.streams.values()
    assert state["attributes"] == {ATTRIB_VOL: 1.0, ATTRIB_PAN: -1.0}
    backend.adjust("nothing-playing", gain=0.5)  # nothing to change, nothing breaks


def test_a_missing_file_is_reported_once_and_plays_nothing():
    fake, backend, errors = _backend(missing=("/sounds/gone.ogg",))
    backend.play("/sounds/gone.ogg", "a", 1.0, 0.0, False)
    backend.play("/sounds/gone.ogg", "b", 1.0, 0.0, False)
    assert fake.streams == {} and not backend.is_playing("a")
    assert len(errors) == 1 and "/sounds/gone.ogg" in errors[0]


def test_the_bus_drives_it_like_any_backend():
    fake, backend, _errors = _backend()
    bus = SoundBus(backend)
    bus.set_volume("sound", 0.5)
    bus.play("/sounds/hit.ogg", "sound", gain=0.5)
    [state] = fake.streams.values()
    assert state["attributes"][ATTRIB_VOL] == 0.25
    bus.flush()
    assert fake.streams == {}


def test_without_bass_the_session_falls_back_to_pygame(monkeypatch):
    def unavailable(folders=None):
        raise bass_library.BassUnavailable("no libbass.so found (nowhere to look)")

    monkeypatch.setattr(bass_library, "load", unavailable)
    diag = RecordingDiag()
    fallback = object()
    monkeypatch.setattr(
        "genericmud.sound.pygame_backend.make_pygame_backend",
        lambda on_error=None, diag=None: fallback,
    )
    assert bass_backend.make_sound_backend(diag=diag) is fallback
    assert diag.fields("backend.select")["reason"].startswith("no BASS")


def _only_stream(fake: _FakeBass) -> tuple[int, dict]:
    [(stream, state)] = fake.streams.items()
    return stream, state


def test_a_cue_starts_and_finishes_where_the_mud_says_each_time_round():
    # Client.Media's start and finish: the loop goes back to the start, not the file's top.
    fake, backend, _errors = _backend()
    backend.play("/sounds/song.ogg", "media:song", 1.0, 0.0, True, timing=CueTiming(2.0, 6.0))
    _stream, state = _only_stream(fake)
    assert state["positions"] == {POS_BYTE: 2000, POS_LOOP: 2000, POS_END: 6000}


def test_a_start_past_the_end_or_a_finish_before_the_start_is_ignored():
    fake, backend, _errors = _backend()
    backend.play("/sounds/a.ogg", "a", 1.0, 0.0, False, timing=CueTiming(start=12.0))
    backend.play("/sounds/b.ogg", "b", 1.0, 0.0, False, timing=CueTiming(start=5.0, finish=3.0))
    positions = [state["positions"] for state in fake.streams.values()]
    assert positions == [{}, {POS_BYTE: 5000}]


def test_a_cue_fades_in_from_silence_to_its_volume():
    fake, backend, _errors = _backend()
    backend.play("/sounds/rain.ogg", "rain", 0.8, 0.0, False, timing=CueTiming(fade_in=1.5))
    _stream, state = _only_stream(fake)
    assert state["attributes"][ATTRIB_VOL] == 0.0
    assert state["slides"] == [(ATTRIB_VOL, 0.8, 1500)]


def test_a_cue_fades_out_ahead_of_its_finish():
    # Mudlet measures the fade out back from the finish when there is one.
    fake, backend, _errors = _backend()
    backend.play(
        "/sounds/rain.ogg", "rain", 0.8, 0.0, False, timing=CueTiming(finish=8.0, fade_out=2.0)
    )
    stream, state = _only_stream(fake)
    fake.reach(stream, 6000)
    assert state["slides"] == [(ATTRIB_VOL, 0.0, 2000)]


def test_each_time_round_fades_again():
    fake, backend, _errors = _backend()
    backend.play(
        "/sounds/wind.ogg", "wind", 0.6, 0.0, True, timing=CueTiming(fade_in=1.0, fade_out=1.0)
    )
    stream, state = _only_stream(fake)
    fake.reach(stream, 9000)
    fake.end_pass(stream)
    assert state["slides"] == [
        (ATTRIB_VOL, 0.6, 1000), (ATTRIB_VOL, 0.0, 1000), (ATTRIB_VOL, 0.6, 1000),
    ]
    assert state["playing"]


def test_a_cue_that_only_fades_out_comes_back_up_when_it_goes_round_again():
    fake, backend, _errors = _backend()
    backend.play("/sounds/wind.ogg", "wind", 0.6, 0.0, True, timing=CueTiming(fade_out=1.0))
    stream, state = _only_stream(fake)
    fake.reach(stream, 9000)
    state["attributes"][ATTRIB_VOL] = 0.0  # where the fade out left it
    fake.end_pass(stream)
    assert state["attributes"][ATTRIB_VOL] == 0.6


def test_fading_away_slides_to_silence_over_what_is_left_and_stops_going_round():
    # Client.Media.Stop with fadeaway: the lesser of the fade and what's left of the cue.
    fake, backend, _errors = _backend()
    backend.play("/sounds/wind.ogg", "wind", 0.6, 0.0, True, timing=CueTiming(fade_out=1.0))
    stream, state = _only_stream(fake)
    state["position"] = 8000
    backend.fade_away("wind", 5.0)
    assert state["slides"][-1] == (ATTRIB_VOL, -1.0, 2000)
    assert not state["flags"] & SAMPLE_LOOP
    backend.adjust("wind", gain=1.0)  # a change of volume mid fade doesn't undo it
    fake.reach(stream, 9000)  # nor does the cue's own fade out, starting under it
    assert state["slides"][-1] == (ATTRIB_VOL, -1.0, 2000)
    assert state["attributes"][ATTRIB_VOL] == 0.6


def test_fading_away_a_cue_with_nothing_left_stops_it():
    fake, backend, _errors = _backend()
    backend.play("/sounds/hit.ogg", "hit", 1.0, 0.0, False)
    _stream, state = _only_stream(fake)
    state["position"] = _LENGTH
    backend.fade_away("hit", 5.0)
    assert fake.streams == {}


def test_frequency_changes_how_fast_a_cue_plays():
    # VIPMud's #PC Frequency, in Hz; 0 puts back the file's own rate.
    fake, backend, _errors = _backend()
    backend.play("/sounds/engine.ogg", "engine", 1.0, 0.0, True)
    backend.adjust("engine", frequency=22050)
    _stream, state = _only_stream(fake)
    assert state["attributes"][ATTRIB_FREQ] == 22050
    backend.adjust("engine", frequency=-5)  # no such rate: left as it was
    assert state["attributes"][ATTRIB_FREQ] == 22050


def test_a_paused_cue_says_so_and_carries_on_when_resumed():
    # Miriani-Next frees every stream whose IsActive says stopped and keeps playing (1),
    # stalled (2) and paused (3) ones: a pause has to read as a pause.
    fake, backend, _errors = _backend()
    backend.play("/sounds/song.ogg", "song", 1.0, 0.0, True)
    assert backend.state("song") == ACTIVE_PLAYING
    backend.pause("song")
    assert backend.state("song") == 3
    backend.resume("song")
    assert backend.state("song") == ACTIVE_PLAYING
    assert backend.state("nothing") == ACTIVE_STOPPED


def test_slides_glide_each_axis_and_positions_move_in_seconds():
    fake, backend, _errors = _backend()
    backend.play("/sounds/engine.ogg", "engine", 1.0, 0.0, True)
    backend.slide("engine", gain=0.2, frequency=11025, seconds=1.5)
    stream, state = _only_stream(fake)
    assert state["slides"] == [(ATTRIB_VOL, 0.2, 1500), (ATTRIB_FREQ, 11025, 1500)]
    backend.seek("engine", 4.5)
    assert backend.position("engine") == 4.5
    backend.set_looping("engine", False)
    assert not state["flags"] & SAMPLE_LOOP
    backend.set_looping("engine", True)
    assert state["flags"] & SAMPLE_LOOP


def test_a_file_is_measured_without_being_played():
    fake, backend, _errors = _backend(missing=("/sounds/gone.ogg",))
    info = backend.source_info("/sounds/lp.ogg")
    assert (info.seconds, info.rate, info.channels) == (10.0, 22050, 1)
    assert fake.streams == {}  # opened to read, and closed again
    assert backend.source_info("/sounds/gone.ogg") is None


def test_the_bus_scales_a_slide_and_takes_a_slide_below_zero_as_a_fade_away():
    fake, backend, _errors = _backend()
    bus = SoundBus(backend)
    bus.set_volume("rain", 0.5)
    bus.play("/sounds/rain.ogg", "rain", loop=True)
    _stream, state = _only_stream(fake)
    bus.slide("rain", gain=0.8, seconds=2.0)
    assert state["slides"][-1] == (ATTRIB_VOL, 0.4, 2000)
    bus.slide("rain", gain=-1.0, seconds=1.0)
    assert state["slides"][-1] == (ATTRIB_VOL, -1.0, 1000)


def test_a_loop_frees_itself_once_it_stops_going_round():
    # A loop told to stop going round plays to its end and stops. BASS must free it then, or
    # every ambience a pack fades out stays allocated for the rest of the session.
    fake, backend, _errors = _backend()
    backend.play("/sounds/rain.ogg", "rain", 1.0, 0.0, True)
    stream, _state = _only_stream(fake)
    backend.set_looping("rain", False)
    fake.end_pass(stream)
    assert fake.streams == {}
    assert not backend.is_playing("rain")


def test_a_loop_faded_away_is_freed_when_the_fade_ends():
    fake, backend, _errors = _backend()
    backend.play("/sounds/rain.ogg", "rain", 1.0, 0.0, True)
    stream, _state = _only_stream(fake)
    backend.fade_away("rain", 1.0)
    fake.finish_slides(stream)
    assert fake.streams == {}


def test_a_cue_that_has_ended_is_forgotten():
    # Packs give each sound a channel of its own (erion-audio-1, -2 and on), so a record
    # kept of each one that ended would grow all session.
    fake, backend, _errors = _backend()
    for number in range(1, 4):
        backend.play("/sounds/hit.ogg", f"hit-{number}", 1.0, 0.0, False)
        fake.end_pass(backend._cues[f"hit-{number}"].stream)
    backend.play("/sounds/rain.ogg", "rain", 1.0, 0.0, True)
    assert list(backend._cues) == ["rain"]


def test_a_freed_cues_handle_is_never_used_again():
    # This stand-in hands a freed stream's number to the next stream (real BASS, measured,
    # doesn't). A channel whose cue has ended must never reach the new stream through it.
    fake, backend, _errors = _backend(reuse_handles=True)
    backend.play("/sounds/hit.ogg", "hit", 1.0, 0.0, False)
    old = backend._cues["hit"].stream
    fake.end_pass(old)
    backend.play("/sounds/rain.ogg", "rain", 1.0, 0.0, True)
    assert backend._cues["rain"].stream == old  # the same number, another stream
    assert not backend.is_playing("hit")
    backend.adjust("hit", gain=0.1)
    backend.stop("hit")
    state = fake.streams[old]
    assert state["playing"] and state["attributes"][ATTRIB_VOL] == 1.0


def test_bass_holds_one_callback_for_good_and_a_cues_actions_go_once_it_is_freed():
    # BASS calls back from its own thread, and calling a callback ctypes had freed would crash
    # genericMud. So BASS only ever holds one, which is never freed, and it finds each
    # cue's action by a number BASS hands back. A cue's actions go once BASS frees it.
    fake, backend, _errors = _backend()
    backend.play("/sounds/rain.ogg", "rain", 1.0, 0.0, True, timing=CueTiming(fade_in=1.0))
    rain = backend._cues["rain"]
    syncs = fake.streams[rain.stream]["syncs"]
    assert len(syncs) >= 2  # it's freed, and it fades in each time round
    assert all(callback is bass_backend._dispatch for _kind, _parameter, callback, _user in syncs)
    numbers = [user for _kind, _parameter, _callback, user in syncs]
    backend.set_looping("rain", False)
    backend.play("/sounds/hit.ogg", "hit", 1.0, 0.0, False)  # a new cue tidies up
    assert all(number in bass_backend._actions for number in numbers)  # still playing out
    fake.end_pass(rain.stream)
    backend.play("/sounds/hit.ogg", "hit", 1.0, 0.0, False)
    assert not any(number in bass_backend._actions for number in numbers)


def test_the_panic_key_stops_a_cue_while_it_fades_away():
    # A fade away can take seconds; the stop-all key and closing the session must end it.
    fake, backend, _errors = _backend()
    bus = SoundBus(backend)
    bus.play("/sounds/rain.ogg", "rain", loop=True)
    bus.fade_away("rain", 5.0)
    assert fake.streams  # fading
    bus.flush()
    assert fake.streams == {}


def test_an_effect_goes_on_the_playing_cue_with_the_settings_given():
    fake, backend, _errors = _backend()
    backend.play("/sounds/rain.ogg", "rain", 1.0, 0.0, True)
    stream, _state = _only_stream(fake)
    echo = backend.add_effect("rain", DX8_ECHO, {"fLeftDelay": 250.0, "fRightDelay": 750.0})
    assert (fake.effects[echo]["stream"], fake.effects[echo]["kind"]) == (stream, DX8_ECHO)
    settings = backend.effect_settings("rain", echo)
    assert (settings["fLeftDelay"], settings["fRightDelay"]) == (250.0, 750.0)
    assert backend.add_effect("idle", DX8_ECHO) == 0  # nothing plays there
    assert backend.add_effect("rain", 99) == 0  # no such effect


def test_a_refused_setting_leaves_every_setting_as_it_was():
    _fake, backend, _errors = _backend()
    backend.play("/sounds/rain.ogg", "rain", 1.0, 0.0, True)
    echo = backend.add_effect("rain", DX8_ECHO, {"fLeftDelay": 250.0, "fRightDelay": 250.0})
    assert not backend.set_effect("rain", echo, {"fLeftDelay": 5000.0, "fRightDelay": 100.0})
    settings = backend.effect_settings("rain", echo)
    assert (settings["fLeftDelay"], settings["fRightDelay"]) == (250.0, 250.0)


def test_gargle_is_made_from_a_volume_envelope_where_bass_has_none():
    # BASS lacks DirectX's gargle off Windows; ChatMUD asks Chatpack for it.
    fake, backend, _errors = _backend(missing_effects=(DX8_GARGLE,))
    backend.play("/sounds/rain.ogg", "rain", 1.0, 0.0, True)
    gargle = backend.add_effect("rain", DX8_GARGLE, {"dwRateHz": 10, "dwWaveShape": 1})
    assert fake.effects[gargle]["kind"] == BFX_VOLUME_ENV
    # A square wave: four points in each tenth of a second of the ten-second sound, then one.
    assert fake.effects[gargle]["settings"]["lNodeCount"] == 4 * 100 + 1
    assert backend.effect_settings("rain", gargle) == {"dwRateHz": 10, "dwWaveShape": 1}
    assert not backend.set_effect("rain", gargle, {"dwRateHz": 0})  # DirectX takes 1 to 1000
    assert backend.effect_settings("rain", gargle)["dwRateHz"] == 10


def test_an_effect_bass_lacks_here_answers_0():
    _fake, backend, _errors = _backend(missing_effects=(DX8_COMPRESSOR,))
    backend.play("/sounds/rain.ogg", "rain", 1.0, 0.0, True)
    assert backend.add_effect("rain", DX8_COMPRESSOR) == 0


def test_effects_come_off_and_start_afresh():
    fake, backend, _errors = _backend()
    backend.play("/sounds/rain.ogg", "rain", 1.0, 0.0, True)
    echo = backend.add_effect("rain", DX8_ECHO)
    reverb = backend.add_effect("rain", DX8_REVERB)
    backend.reset_effects("rain", echo)
    assert (fake.effects[echo]["resets"], fake.effects[reverb]["resets"]) == (1, 0)
    backend.reset_effects("rain")
    assert (fake.effects[echo]["resets"], fake.effects[reverb]["resets"]) == (2, 1)
    backend.remove_effect("rain", echo)
    assert list(fake.effects) == [reverb]
    assert backend.effect_settings("rain", echo) is None


# Real BASS, when this machine has it.

_FOLDER = os.environ.get(bass_library.ENVIRONMENT_FOLDER)
needs_bass = pytest.mark.skipif(
    not _FOLDER or not Path(_FOLDER).is_dir(),
    reason=f"set {bass_library.ENVIRONMENT_FOLDER} to a folder with the BASS library",
)


@pytest.fixture
def real_bass():
    bass = bass_library.load([Path(_FOLDER)])
    bass_library.start(bass, bass_library.DEVICE_NO_SOUND)
    return bass


def _tone(path: Path, seconds: float = 0.2) -> Path:
    """A short WAV file to play."""
    import struct
    import wave

    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(22050)
        file.writeframes(struct.pack("<h", 1000) * int(22050 * seconds))
    return path


@needs_bass
def test_real_bass_plays_stops_and_reports_a_missing_file(real_bass, tmp_path):
    errors: list[str] = []
    backend = BassSoundBackend(real_bass.core, on_error=errors.append)
    tone = _tone(tmp_path / "tone ünïcode.wav")
    backend.play(str(tone), "test", 0.5, 0.0, True)
    assert backend.is_playing("test")
    backend.adjust("test", gain=0.2, pan=0.5)
    backend.stop("test")
    assert not backend.is_playing("test")
    backend.play(str(tmp_path / "missing.wav"), "test", 1.0, 0.0, False)
    assert len(errors) == 1


@needs_bass
def test_real_bass_repeats_a_cue_the_number_of_times_asked(real_bass, tmp_path):
    # Decoded rather than heard, so the count is exact.
    tone = _tone(tmp_path / "tone.wav")
    core = real_bass.core
    backend = BassSoundBackend(core)
    path, flags = bass_library.path_argument(str(tone))
    stream = core.BASS_StreamCreateFile(
        False, path, 0, 0, flags | bass_library.STREAM_DECODE | SAMPLE_LOOP
    )
    length = core.BASS_ChannelGetLength(stream, 0)
    cue = bass_backend._Cue(stream=stream, gain=1.0, pan=0.0)  # holds the callback alive
    backend._repeat(cue, 3)
    buffer = ctypes.create_string_buffer(1 << 16)
    total = 0
    while total < length * 10:
        got = core.BASS_ChannelGetData(stream, buffer, len(buffer))
        if got in (0, 0xFFFFFFFF):
            break
        total += got
    core.BASS_StreamFree(stream)
    assert total == length * 3


_SOUNDS = Path(__file__).parent / "data" / "sounds"
# One of each kind of sound the Soundpack Vault's packs ship (tiny tones made with ffmpeg).
# AAC is Chatpack's .m4a: Windows and macOS decode it themselves; Linux needs an add-on
# genericMud doesn't carry.
_KINDS = sorted(path.name for path in _SOUNDS.iterdir())


@needs_bass
@pytest.mark.parametrize("name", _KINDS)
def test_real_bass_decodes_every_kind_of_sound_packs_ship(real_bass, name):
    if name.endswith(".m4a") and sys.platform.startswith("linux"):
        pytest.skip("AAC needs BASS's GPL add-on on Linux")
    core = real_bass.core
    path, flags = bass_library.path_argument(str(_SOUNDS / name))
    stream = core.BASS_StreamCreateFile(False, path, 0, 0, flags | bass_library.STREAM_DECODE)
    assert stream, f"{name}: BASS error {core.BASS_ErrorGetCode()}"
    assert core.BASS_ChannelGetLength(stream, 0) > 0
    core.BASS_StreamFree(stream)


def _volume(core, stream: int) -> float:
    value = ctypes.c_float()
    core.BASS_ChannelGetAttribute(stream, ATTRIB_VOL, ctypes.byref(value))
    return value.value


@needs_bass
def test_real_bass_trims_and_repeats_a_cue_each_time_round(real_bass, tmp_path):
    # Decoded rather than heard, so the count is exact: three times round the middle second.
    tone = _tone(tmp_path / "tone.wav", seconds=2.0)
    core = real_bass.core
    backend = BassSoundBackend(core)
    path, flags = bass_library.path_argument(str(tone))
    stream = core.BASS_StreamCreateFile(
        False, path, 0, 0, flags | bass_library.STREAM_DECODE | SAMPLE_LOOP
    )
    second = core.BASS_ChannelSeconds2Bytes(stream, 1.0)
    cue = bass_backend._Cue(stream=stream, gain=1.0, pan=0.0)
    cue.end = core.BASS_ChannelGetLength(stream, 0)
    backend._repeat(cue, 3)
    backend._time(cue, CueTiming(start=0.5, finish=1.5), looping=True)
    buffer = ctypes.create_string_buffer(1 << 16)
    total = 0
    while total < second * 20:
        got = core.BASS_ChannelGetData(stream, buffer, len(buffer))
        if got in (0, 0xFFFFFFFF):
            break
        total += got
    core.BASS_StreamFree(stream)
    assert abs(total - 3 * second) <= 64  # to the nearest sample or so


@needs_bass
def test_real_bass_fades_a_cue_in_and_away(real_bass, tmp_path):
    import time

    tone = _tone(tmp_path / "tone.wav", seconds=2.0)
    backend = BassSoundBackend(real_bass.core)
    backend.play(str(tone), "fading", 0.8, 0.0, True, timing=CueTiming(fade_in=0.4))
    stream = backend._cues["fading"].stream
    assert _volume(real_bass.core, stream) < 0.3
    time.sleep(0.6)
    assert abs(_volume(real_bass.core, stream) - 0.8) < 0.05
    backend.fade_away("fading", 0.2)
    assert backend.is_playing("fading")
    time.sleep(0.6)
    assert not backend.is_playing("fading")


@needs_bass
def test_real_bass_pauses_resumes_seeks_and_measures(real_bass, tmp_path):
    import time

    tone = _tone(tmp_path / "tone.wav", seconds=2.0)
    backend = BassSoundBackend(real_bass.core)
    info = backend.source_info(str(tone))
    assert (round(info.seconds, 2), info.rate, info.channels) == (2.0, 22050, 1)
    backend.play(str(tone), "held", 1.0, 0.0, True)
    time.sleep(0.2)
    backend.pause("held")
    assert backend.state("held") == 3
    time.sleep(0.1)  # the reported position settles as the device's buffer drains
    held_at = backend.position("held")
    time.sleep(0.2)
    assert backend.position("held") == held_at
    backend.resume("held")
    time.sleep(0.2)
    assert backend.position("held") > held_at
    backend.seek("held", 1.5)
    assert 1.5 <= backend.position("held") < 1.7
    backend.stop("held")


@needs_bass
def test_real_bass_puts_its_effects_on_a_cue(real_bass, tmp_path):
    tone = _tone(tmp_path / "tone.wav", seconds=2.0)
    backend = BassSoundBackend(real_bass.core)
    backend.play(str(tone), "fx", 1.0, 0.0, True)
    echo = backend.add_effect("fx", DX8_ECHO, {"fLeftDelay": 250.0, "fRightDelay": 750.0})
    assert echo
    settings = backend.effect_settings("fx", echo)
    assert (settings["fLeftDelay"], settings["fRightDelay"]) == (250.0, 750.0)
    assert not backend.set_effect("fx", echo, {"fLeftDelay": 5000.0})  # past DirectX's 2 s
    assert backend.effect_settings("fx", echo)["fLeftDelay"] == 250.0
    # Gargle is BASS's own on Windows, and made from BASS_FX's volume envelope elsewhere.
    gargle = backend.add_effect("fx", DX8_GARGLE, {"dwRateHz": 40, "dwWaveShape": 1})
    assert gargle
    assert backend.effect_settings("fx", gargle) == {"dwRateHz": 40, "dwWaveShape": 1}
    for kind in (DX8_CHORUS, DX8_DISTORTION, DX8_FLANGER, DX8_PARAMEQ, DX8_REVERB):
        assert backend.add_effect("fx", kind), kind
    backend.reset_effects("fx")
    backend.remove_effect("fx", echo)
    assert backend.effect_settings("fx", echo) is None
    backend.stop("fx")


@needs_bass
def test_real_bass_frees_a_loop_once_it_stops_and_forgets_it(real_bass, tmp_path):
    import time

    tone = _tone(tmp_path / "tone.wav", seconds=0.2)
    backend = BassSoundBackend(real_bass.core)
    details = bass_library.ChannelInfo()
    backend.play(str(tone), "loop", 1.0, 0.0, True)
    looped = backend._cues["loop"].stream
    backend.set_looping("loop", False)
    backend.play(str(tone), "faded", 1.0, 0.0, True)
    faded = backend._cues["faded"].stream
    backend.fade_away("faded", 0.1)
    time.sleep(0.7)
    for stream in (looped, faded):
        assert not real_bass.core.BASS_ChannelGetInfo(stream, ctypes.byref(details))  # freed
    assert not backend.is_playing("loop")
    backend.play(str(tone), "other", 1.0, 0.0, False)
    assert sorted(backend._cues) == ["other"]
    backend.stop("other")
