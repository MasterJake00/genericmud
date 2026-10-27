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
    ATTRIB_PAN,
    ATTRIB_VOL,
    SAMPLE_LOOP,
    STREAM_AUTOFREE,
    SYNC_END,
)
from genericmud.sound.bus import SoundBus
from tests.helpers import RecordingDiag


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
        }
        return self.next_handle

    def BASS_ErrorGetCode(self):  # noqa: N802
        return self.error

    def BASS_ChannelPlay(self, stream, _restart):  # noqa: N802
        self.streams[stream]["playing"] = True
        return 1

    def BASS_StreamFree(self, stream):  # noqa: N802
        return 1 if self.streams.pop(stream, None) is not None else 0

    def BASS_ChannelIsActive(self, stream):  # noqa: N802
        state = self.streams.get(stream)
        return ACTIVE_PLAYING if state and state["playing"] else ACTIVE_STOPPED

    def BASS_ChannelSetAttribute(self, stream, attribute, value):  # noqa: N802
        self.streams[stream]["attributes"][attribute] = value
        return 1

    def BASS_ChannelFlags(self, stream, flags, mask):  # noqa: N802
        state = self.streams[stream]
        state["flags"] = (state["flags"] & ~mask) | (flags & mask)
        return state["flags"]

    def BASS_ChannelSetSync(self, stream, kind, _parameter, callback, _user):  # noqa: N802
        self.streams[stream]["syncs"].append((kind, callback))
        return 1

    def end_pass(self, stream: int) -> None:
        """The stream reaches its end: its end syncs run, and it stops unless it loops."""
        state = self.streams[stream]
        for kind, callback in state["syncs"]:
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
