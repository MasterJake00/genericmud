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
    SYNC_MIXTIME,
)
from genericmud.sound.bus import CueTiming, SoundBus
from tests.helpers import RecordingDiag

_BYTES_PER_SECOND = 1000  # the stand-in's files: a thousand bytes a second, ten seconds long
_LENGTH = 10 * _BYTES_PER_SECOND


class _FakeBass:
    """BASS's functions as the backend calls them, keeping track of every stream."""

    def __init__(self, missing: tuple[str, ...] = ()) -> None:
        self.missing = missing  # files that "don't exist"
        self.streams: dict[int, dict] = {}
        self.next_handle = 100
        self.error = 0

    def BASS_StreamCreateFile(self, _memory, path, _offset, _length, flags):  # noqa: N802
        name = path.value if isinstance(path, ctypes.c_wchar_p) else path.value.decode()
        if name in self.missing:
            self.error = 2  # BASS_ERROR_FILEOPEN
            return 0
        self.next_handle += 1
        self.streams[self.next_handle] = {
            "file": name, "flags": flags, "playing": False, "attributes": {}, "syncs": [],
            "positions": {}, "slides": [], "position": 0,
        }
        return self.next_handle

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
        return 1 if self.streams.pop(stream, None) is not None else 0

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

    def BASS_ChannelSetSync(self, stream, kind, parameter, callback, _user):  # noqa: N802
        self.streams[stream]["syncs"].append((kind, parameter, callback))
        return 1

    def reach(self, stream: int, position: int) -> None:
        """The stream plays past ``position``: its position syncs there run."""
        for kind, parameter, callback in self.streams[stream]["syncs"]:
            if kind & ~SYNC_MIXTIME == 0 and parameter == position:
                callback(1, stream, 0, None)

    def end_pass(self, stream: int) -> None:
        """The stream reaches its end: its end syncs run, and it stops unless it loops."""
        state = self.streams[stream]
        for kind, _parameter, callback in state["syncs"]:
            if kind & SYNC_END:
                callback(1, stream, 0, None)
        if not state["flags"] & SAMPLE_LOOP:
            state["playing"] = False
            if state["flags"] & STREAM_AUTOFREE:
                del self.streams[stream]

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
    assert looping[0]["flags"] & SAMPLE_LOOP and not looping[0]["flags"] & STREAM_AUTOFREE


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
    backend._repeat(stream, 3)
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
    backend._repeat(stream, 3)
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

