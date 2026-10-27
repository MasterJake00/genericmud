"""The stand-in for the BASS wrapper MUSHclient packs ship: each test is something a real pack
does with it (Miriani-Next, Toastush, Chatpack, the Yu-Gi-Oh pack), played through a sound bus
over a stand-in BASS."""

from __future__ import annotations

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.sound.bass_backend import BassSoundBackend
from genericmud.sound.bass_library import ATTRIB_FREQ, ATTRIB_VOL, SAMPLE_LOOP
from genericmud.sound.bus import SoundBus
from tests.helpers import RecordingSink
from tests.test_bass_backend import _FakeBass

AUTO_FREE = 0x40000
LOOP = 4


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
def pack(tmp_path):
    for name in ("hit.ogg", "lp.ogg", "theme.ogg"):
        (tmp_path / "sounds").mkdir(exist_ok=True)
        (tmp_path / "sounds" / name).write_bytes(b"OggS")
    fake = _FakeBass()
    bus = SoundBus(BassSoundBackend(fake))
    engine = AutomationEngine(_BusSink(bus), sound=bus)
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_source("<muclient/>")
    lua = pack._lua
    caller = lua.eval("function(object, name, ...) return object[name](object, ...) end")
    reader = lua.eval("function(object, key) return object[key] end")
    writer = lua.eval("function(object, key, value) object[key] = value end")

    class _Pack:
        def __init__(self) -> None:
            self.fake = fake
            self.bass = lua.eval("require('miriani.lib.audio.bass')()")

        def call(self, target, name, *args):
            return caller(target, name, *args)

        def get(self, target, key):
            return reader(target, key)

        def set(self, target, key, value):
            writer(target, key, value)

        def bass_names(self):
            """The BASS object's own fields, as pairs() finds them."""
            listed = lua.eval(
                "function(object) local names = {} "
                "for name in pairs(object) do names[#names + 1] = name end "
                "return table.concat(names, ',') end"
            )(self.bass)
            return set(listed.split(","))

        def stream(self, file="sounds/hit.ogg", flags=0):
            return caller(self.bass, "StreamCreateFile", False, file, 0, 0, flags)

        def playing(self):
            """The stand-in BASS's stream for the one cue playing."""
            [(handle, state)] = fake.streams.items()
            return handle, state

    return _Pack()


def test_a_finished_one_shot_reads_as_stopped_so_miriani_next_frees_it(pack):
    # Miriani-Next frees every stream whose IsActive is 0 and keeps 1, 2 and 3. The old
    # stand-in said 1 for ever, so its table of sounds only ever grew.
    stream = pack.stream(flags=AUTO_FREE)
    assert pack.call(stream, "IsActive") == 0  # made, not played
    assert pack.call(stream, "Play") == 0
    assert pack.call(stream, "IsActive") == 1
    handle, _state = pack.playing()
    pack.fake.end_pass(handle)
    assert pack.call(stream, "IsActive") == 0
    assert pack.call(stream, "Free") == 0


def test_pause_holds_a_stream_and_play_carries_it_on(pack):
    stream = pack.stream(flags=LOOP)
    pack.call(stream, "Play")
    pack.call(stream, "Pause")
    assert pack.call(stream, "IsActive") == 3
    pack.call(stream, "Play")
    assert pack.call(stream, "IsActive") == 1
    assert len(pack.fake.streams) == 1  # the same stream carried on, not a new one


def test_frequency_reads_as_the_files_own_rate_until_a_pack_changes_it(pack):
    # Packs pitch a sound from its own rate: SetAttribute(FREQ, GetAttribute(FREQ) * 1.2).
    stream = pack.stream()
    assert pack.call(stream, "GetAttribute", 1) == 22050
    pack.call(stream, "Play")
    pack.call(stream, "SetAttribute", 1, 26460)
    _handle, state = pack.playing()
    assert state["attributes"][ATTRIB_FREQ] == 26460
    assert pack.call(stream, "GetAttribute", 1) == 26460


def test_a_slide_glides_and_a_slide_to_minus_one_fades_out_and_stops(pack):
    stream = pack.stream(flags=LOOP)
    pack.call(stream, "Play")
    pack.call(stream, "SlideAttribute", 2, 0.2, 1000)
    _handle, state = pack.playing()
    assert state["slides"][-1] == (ATTRIB_VOL, 0.2, 1000)
    pack.call(stream, "SlideAttribute", 2, -1, 500)
    assert state["slides"][-1][1] == -1.0
    assert not state["flags"] & SAMPLE_LOOP


def test_the_fields_a_pack_reads_right_after_making_a_stream_are_real(pack):
    stream = pack.stream()
    assert pack.get(stream, "length") == 10.0
    assert pack.get(stream, "frequency") == 22050
    assert pack.get(stream, "channels") == 1
    assert pack.get(stream, "id") == 1


def test_positions_round_trip_in_the_streams_own_bytes(pack):
    # Miriani-Next notices a stuck device by GetPosition not moving; it must be the real one.
    stream = pack.stream(flags=LOOP)
    pack.call(stream, "Play")
    three_seconds = pack.call(stream, "Seconds2Bytes", 3.0)
    assert three_seconds == 3 * 22050 * 2  # 16-bit mono
    pack.call(stream, "SetPosition", three_seconds)
    assert pack.get(stream, "position") == 3.0
    assert pack.call(stream, "GetPosition") == three_seconds
    pack.set(stream, "position", 5.0)
    assert pack.call(stream, "Bytes2Seconds", pack.call(stream, "GetPosition")) == 5.0


def test_the_life_points_sound_plays_as_many_times_as_it_was_put(pack):
    # The Yu-Gi-Oh pack reads lp.ogg's data, puts it into a new stream once per hundred life
    # points lost, and times its queue by that stream's length.
    sound = pack.stream("sounds/lp.ogg")
    data = pack.call(sound, "GetData", 0xFFFFFFF)
    built = pack.call(pack.bass, "StreamCreate", pack.get(sound, "frequency"), 1, 0)
    for _ in range(3):
        pack.call(built, "PutData", data)
    assert pack.get(built, "length") == 30.0
    pack.call(built, "Play")
    handle, _state = pack.playing()
    for _ in range(2):
        pack.fake.end_pass(handle)
        assert pack.call(built, "IsActive") == 1
    pack.fake.end_pass(handle)
    assert pack.call(built, "IsActive") == 0


def test_flags_turn_a_playing_streams_loop_off(pack):
    # Chatpack lets a looping sound finish its time round this way.
    stream = pack.stream(flags=LOOP)
    pack.call(stream, "Play")
    assert pack.call(stream, "Flags", 0, LOOP) == 0
    _handle, state = pack.playing()
    assert not state["flags"] & SAMPLE_LOOP


def test_what_a_pack_cant_have_answers_with_basss_own_error_codes(pack):
    assert pack.stream("sounds/missing.ogg") == 2  # BASS_ERROR_FILEOPEN
    assert pack.stream("../outside.ogg") == 2
    assert pack.call(pack.bass, "StreamCreateUrl", "https://example.com/a.ogg", 0, 0) == 2
    assert pack.call(pack.bass, "SetEnvironment", 0, 1, 1, 1) == 22  # BASS_ERROR_NOEAX
    assert pack.call(pack.bass, "PluginLoad", "bass_aac.dll") > 0  # loaded, as far as it knows


def test_a_freed_or_stopped_auto_free_stream_is_gone_as_in_bass(pack):
    stream = pack.stream()
    pack.call(stream, "Play")
    assert pack.call(stream, "Free") == 0
    assert pack.call(stream, "Free") == 5  # BASS_ERROR_HANDLE
    assert pack.call(stream, "Play") == 5
    gone = pack.stream(flags=AUTO_FREE | LOOP)
    pack.call(gone, "Play")
    pack.call(gone, "Stop")
    assert pack.call(gone, "Play") == 5


def test_calls_the_stand_in_doesnt_model_answer_fine(pack):
    stream = pack.stream()
    assert pack.call(stream, "AddFX", 3, {}) == 0
    assert pack.call(pack.bass, "SetConfig", 0, 1) == 0


def test_a_class_built_on_the_bass_class_gets_its_methods(pack):
    # Toastush builds its Audio class on this one with Penlight's class(), which copies the
    # fields it finds with pairs(): methods only reachable through a metatable came out missing,
    # so Toastush's start-up failed and every sound after it.
    lua_names = pack.bass_names()
    assert {"Init", "Free", "StreamCreateFile", "SetConfig"} <= lua_names


def test_toastushs_wrapper_sets_attributes_by_name(pack):
    stream = pack.stream()
    pack.call(stream, "Play")
    pack.call(pack.bass, "SetAttribute", stream, "volume", 0.3)
    pack.call(pack.bass, "SetAttribute", stream, "frequency", 30000)
    _handle, state = pack.playing()
    assert state["attributes"][ATTRIB_VOL] == 0.3
    assert state["attributes"][ATTRIB_FREQ] == 30000
