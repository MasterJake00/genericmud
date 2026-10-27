"""MUSHclient's own PlaySound, StopSound and GetSoundStatus, by the rules in its source
(methods_sounds.cpp): ten sound buffers that play at once, a volume in decibels where 0 is full,
a pan that is the decibels taken off the other side, and the answers it gives. No pack in the
Soundpack Vault calls these, but scripts written to MUSHclient's documentation do, and a volume
of 0 there means full, not silent."""

from __future__ import annotations

from pathlib import Path

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.scripting.mushclient_rules import ERROR_CODES
from genericmud.sound.bass_backend import BassSoundBackend
from genericmud.sound.bass_library import ATTRIB_PAN, ATTRIB_VOL, SAMPLE_LOOP
from genericmud.sound.bus import SoundBus
from tests.helpers import RecordingSink
from tests.test_bass_backend import _FakeBass

HALF_AS_LOUD = 10 ** (-6 / 20)  # 6 dB down


class _BusSink(RecordingSink):
    """A sink that plays cues on the engine's sound bus, as the app's does."""

    def __init__(self, bus: SoundBus) -> None:
        super().__init__()
        self.bus = bus

    def play(self, file, channel="sound", gain=1.0, pan=0.0, loop=False, times=1):
        super().play(file, channel, gain, pan, loop, times)
        self.bus.play(file, channel, gain, pan, loop, times=times)

    def stop(self, channel):
        super().stop(channel)
        self.bus.stop(channel)


@pytest.fixture
def sounds(tmp_path):
    # Laid out as a MUSHclient folder: the world in worlds/, the sounds in sounds/.
    (tmp_path / "worlds").mkdir()
    (tmp_path / "sounds").mkdir()
    for name in ("hit.wav", "rain.wav", "door.wav"):
        (tmp_path / "sounds" / name).write_bytes(b"RIFF")
    world = tmp_path / "worlds" / "World.mcl"
    world.write_text('<?xml version="1.0"?><muclient><world name="W"/></muclient>')
    fake = _FakeBass()
    backend = BassSoundBackend(fake)
    bus = SoundBus(backend)
    engine = AutomationEngine(_BusSink(bus), sound=bus)
    pack = MushclientPack(ScriptApi(engine, source="t", base_dir=str(tmp_path)))
    pack.load_file(str(world))
    lua = pack._lua

    class _Sounds:
        def play(self, *args):
            return lua.eval("function(...) return PlaySound(...) end")(*args)

        def stop(self, *args):
            return lua.eval("function(...) return StopSound(...) end")(*args)

        def status(self, *args):
            return lua.eval("function(...) return GetSoundStatus(...) end")(*args)

        def _state(self, number):
            cue = backend._cues.get(f"mush-{number}")
            assert cue is not None, f"nothing plays in buffer {number}"
            return fake.streams[cue.stream]

        def path(self, number):
            return Path(self._state(number)["file"])

        def file(self, number):
            return self.path(number).name

        def level(self, number):
            return self._state(number)["attributes"][ATTRIB_VOL]

        def balance(self, number):
            return self._state(number)["attributes"][ATTRIB_PAN]

        def looping(self, number):
            return bool(self._state(number)["flags"] & SAMPLE_LOOP)

        def playing(self):
            return {
                int(channel.split("-")[1]) for channel, cue in backend._cues.items()
                if fake.streams.get(cue.stream, {}).get("playing")
            }

        def finish(self, number):
            fake.end_pass(backend._cues[f"mush-{number}"].stream)

    return _Sounds()


def test_volume_is_decibels_where_0_is_full(sounds):
    assert sounds.play(1, "hit.wav", False, 0, 0) == 0
    assert sounds.level(1) == 1.0
    sounds.play(2, "hit.wav", False, -6, 0)
    assert sounds.level(2) == pytest.approx(HALF_AS_LOUD)


def test_a_volume_past_0_or_minus_100_plays_at_full(sounds):
    # MUSHclient forces it to 0; the old reading took 50 as half volume.
    sounds.play(1, "hit.wav", False, 50, 0)
    sounds.play(2, "hit.wav", False, -150, 0)
    assert sounds.level(1) == sounds.level(2) == 1.0


def test_pan_takes_decibels_off_the_other_side(sounds):
    sounds.play(1, "hit.wav", False, 0, 6)  # the left side 6 dB down: half as loud
    assert sounds.balance(1) == pytest.approx(1 - HALF_AS_LOUD)
    sounds.play(2, "hit.wav", False, 0, -100)  # the right side 100 dB down: hard left
    assert sounds.balance(2) == pytest.approx(-1.0, abs=1e-4)
    sounds.play(3, "hit.wav", False, 0, 150)  # out of range: centred, as MUSHclient forces it
    assert sounds.balance(3) == 0.0


def test_buffer_0_takes_a_free_buffer_among_the_first_five(sounds):
    # Buffers 6 to 10 are only ever used when a script names them.
    for number in range(1, 6):
        sounds.play(number, "rain.wav", True)
    sounds.play(0, "hit.wav")
    assert sounds.file(1) == "hit.wav"
    assert sounds.playing() == {1, 2, 3, 4, 5}


def test_buffer_0_reuses_a_buffer_whose_sound_has_ended(sounds):
    sounds.play(1, "rain.wav", True)
    sounds.play(2, "hit.wav")
    sounds.finish(2)
    sounds.play(0, "door.wav")
    assert sounds.file(2) == "door.wav"
    assert sounds.file(1) == "rain.wav"


def test_an_empty_name_resets_the_playing_sounds_volume_pan_and_looping(sounds):
    sounds.play(1, "rain.wav", True, 0, 0)
    assert sounds.play(1, "", False, -6, 6) == 0
    assert sounds.level(1) == pytest.approx(HALF_AS_LOUD)
    assert sounds.balance(1) == pytest.approx(1 - HALF_AS_LOUD)
    assert not sounds.looping(1)  # it plays on to its end and stops there


def test_a_missing_file_leaves_the_buffer_playing(sounds):
    sounds.play(1, "rain.wav", True)
    assert sounds.play(1, "missing.wav") == ERROR_CODES["eFileNotFound"]
    assert sounds.file(1) == "rain.wav"
    assert sounds.playing() == {1}


def test_a_bad_buffer_or_name_answers_ebadparameter(sounds):
    bad = ERROR_CODES["eBadParameter"]
    assert sounds.play(11, "hit.wav") == bad
    assert sounds.play(-1, "hit.wav") == bad
    assert sounds.play(0, "") == bad
    assert sounds.play(3, "", False, 0, 0) == bad  # nothing in buffer 3 to change
    assert sounds.stop(11) == bad
    assert sounds.stop(1) == 0


def test_a_bare_name_is_found_in_the_sounds_folder_beside_mushclient(sounds, tmp_path):
    (tmp_path / "hit.wav").write_bytes(b"RIFF")  # another file of that name at the top
    sounds.play(1, "hit.wav")
    assert sounds.path(1) == tmp_path / "sounds" / "hit.wav"


def test_getsoundstatus_answers_as_mushclient_does(sounds):
    assert sounds.status(0) == sounds.status(11) == -1  # no such buffer
    assert sounds.status(1) == -2  # free
    sounds.play(1, "rain.wav", True)
    sounds.play(2, "hit.wav")
    assert sounds.status(1) == sounds.status(2) == 1  # playing, looping or not
    sounds.finish(2)
    assert sounds.status(2) == 0  # ended, and not let go of yet
    sounds.play(3, "door.wav")  # each PlaySound first lets go of the buffers that have ended
    assert sounds.status(2) == -2
    sounds.stop(1)
    assert sounds.status(1) == -2
