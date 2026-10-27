"""The stand-in for audio.dll, the LuaAudio library Erion, Empire, Mystavaria, Cosmic Rage,
SquareSoft, V.I., Krinnzenn's pack and Mush-Z play every sound through. Each test is a call those
packs make, checked against what the real DLL does with it, played through a sound bus over a
stand-in BASS."""

from __future__ import annotations

from pathlib import Path

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.sound.bass_backend import BassSoundBackend
from genericmud.sound.bass_library import ATTRIB_FREQ, ATTRIB_PAN, ATTRIB_VOL, SAMPLE_LOOP
from genericmud.sound.bus import SoundBus
from tests.helpers import RecordingSink
from tests.test_bass_backend import _FakeBass

SOUNDS = ("rain.ogg", "hit.ogg", "music.ogg", "step.ogg")


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
def audio(tmp_path):
    (tmp_path / "sounds").mkdir()
    for name in SOUNDS:
        (tmp_path / "sounds" / name).write_bytes(b"OggS")
    fake = _FakeBass()
    bus = SoundBus(BassSoundBackend(fake))
    sink = _BusSink(bus)
    engine = AutomationEngine(sink, sound=bus)
    api = ScriptApi(engine, source="m", base_dir=str(tmp_path))
    pack = MushclientPack(api)
    pack.load_source("<muclient/>")
    caller = pack._lua.eval("function(name, ...) return audio[name](...) end")

    class _Audio:
        def __init__(self) -> None:
            self.fake = fake
            self.sink = sink
            self.api = api
            self.dll = pack._audio_dll

        def __getattr__(self, name):
            return lambda *args: caller(name, *args)

        def stream(self, name):
            """The stand-in BASS's state for the latest stream of the file ``name``."""
            found = [
                state for state in fake.streams.values() if Path(state["file"]).name == name
            ]
            assert found, f"{name} was never played"
            return found[-1]

        def handle(self, name):
            return next(
                handle for handle, state in reversed(fake.streams.items())
                if Path(state["file"]).name == name
            )

    return _Audio()


def test_values_come_before_ids_and_times_are_milliseconds(audio):
    # Packs call slideVol(x, id, time): the old stand-in read it as slideVol(id, x) and did nothing.
    rain = audio.play("sounds/rain.ogg", 1)
    audio.slideVol(30, rain, 500)
    assert audio.stream("rain.ogg")["slides"][-1] == (ATTRIB_VOL, 0.3, 500)
    audio.slidePan(-40, rain, 250)
    assert audio.stream("rain.ogg")["slides"][-1] == (ATTRIB_PAN, -0.4, 250)
    audio.slideVol(80, rain)
    assert audio.stream("rain.ogg")["slides"][-1] == (ATTRIB_VOL, 0.8, 1000)  # a second, unsaid


def test_fadeout_fades_to_silence_and_lets_the_sound_play_out(audio):
    # Erion fades its old music and ambience out this way. The DLL slides the volume to 0 and
    # turns the loop off, so the sound runs silently to the end of its pass and stops there.
    music = audio.play("sounds/music.ogg", 1, 0, 70)
    audio.fadeout(music, 500)
    state = audio.stream("music.ogg")
    assert state["slides"][-1] == (ATTRIB_VOL, 0.0, 500)
    assert not state["flags"] & SAMPLE_LOOP
    assert audio.isPlaying(music) == 1
    audio.fake.end_pass(audio.handle("music.ogg"))
    assert audio.isPlaying(music) == 0


def test_fadeout_takes_a_second_when_the_pack_gives_no_time(audio):
    music = audio.play("sounds/music.ogg", 1)
    audio.fadeout(music)
    assert audio.stream("music.ogg")["slides"][-1] == (ATTRIB_VOL, 0.0, 1000)


def test_a_call_naming_id_0_leaves_every_sound_alone(audio):
    # Erion keeps play()'s answer, which is 0 for a missing file, and fades it out later. The old
    # stand-in took fadeout(0) as "stop everything" and the music went with it.
    audio.play("sounds/rain.ogg", 1)
    audio.play("sounds/music.ogg", 1)
    audio.fadeout(0)
    audio.slideVol(10, 0, 100)
    audio.pitch(5, 0)
    assert audio.sink.stopped == []
    for name in ("rain.ogg", "music.ogg"):
        state = audio.stream(name)
        assert state["playing"] and state["slides"] == []
        assert ATTRIB_FREQ not in state["attributes"]


def test_stop_0_and_free_stop_only_the_packs_own_sounds(audio):
    # The DLL keeps a list of the sounds it started, and stop(0) stops those. The old stand-in
    # flushed every sound in the session, a plugin's own BASS streams included.
    audio.api.play("sounds/step.ogg", channel="footsteps", loop=True)
    rain = audio.play("sounds/rain.ogg", 1)
    audio.stop(0)
    assert audio.isPlaying(rain) == 0
    assert audio.stream("step.ogg")["playing"]
    music = audio.play("sounds/music.ogg", 1)
    audio.free()
    assert audio.isPlaying(music) == 0
    assert audio.stream("step.ogg")["playing"]


def test_pitch_plays_a_sound_at_its_own_rate_plus_hundreds_of_hz(audio):
    # Cosmic Rage turns the MUD's pitch into steps of 100 Hz from the file's own rate.
    hit = audio.play("sounds/hit.ogg", 0)
    audio.pitch(40, hit)
    assert audio.stream("hit.ogg")["attributes"][ATTRIB_FREQ] == 22050 + 4000
    audio.pitch(-10, hit)
    assert audio.stream("hit.ogg")["attributes"][ATTRIB_FREQ] == 22050 - 1000


def test_slide_pitch_glides_from_where_the_sound_is(audio):
    hit = audio.play("sounds/hit.ogg", 0)
    audio.pitch(10, hit)
    audio.slidePitch(5, hit, 200)
    assert audio.stream("hit.ogg")["slides"][-1] == (ATTRIB_FREQ, 23550, 200)


def test_the_master_volume_relevels_what_plays_but_not_what_is_silent(audio):
    # LuaAudio's volume keys send Sound("volume=x"), which its hook turns into audio.volume(x):
    # the DLL sets every sound still playing to it, apart from ones faded to silence, and plays
    # later sounds with no volume of their own at it.
    quiet = audio.play("sounds/rain.ogg", 1, 0, 50)
    loud = audio.play("sounds/music.ogg", 1)
    hit = audio.play("sounds/hit.ogg", 1)
    audio.fadeout(hit)
    audio.volume(70)
    assert audio.stream("rain.ogg")["attributes"][ATTRIB_VOL] == pytest.approx(0.7)
    assert audio.stream("music.ogg")["attributes"][ATTRIB_VOL] == pytest.approx(0.7)
    assert audio.stream("hit.ogg")["attributes"][ATTRIB_VOL] == 1.0  # still fading to silence
    assert audio.getVolume() == pytest.approx(70)
    assert audio.isPlaying(quiet) == audio.isPlaying(loud) == 1
    audio.play("sounds/step.ogg", 0)
    assert audio.stream("step.ogg")["attributes"][ATTRIB_VOL] == pytest.approx(0.7)


def test_volume_and_pan_with_an_id_change_just_that_sound(audio):
    rain = audio.play("sounds/rain.ogg", 1)
    music = audio.play("sounds/music.ogg", 1, 0, 90)
    audio.volume(20.4, rain)  # the DLL takes whole numbers
    audio.pan(-50, rain)
    assert audio.stream("rain.ogg")["attributes"][ATTRIB_VOL] == pytest.approx(0.2)
    assert audio.stream("rain.ogg")["attributes"][ATTRIB_PAN] == pytest.approx(-0.5)
    assert audio.getVolume(rain) == pytest.approx(20)
    assert audio.getVolume(music) == pytest.approx(90)
    assert audio.getVolume() == pytest.approx(100)  # the master volume is untouched


def test_a_pan_past_either_side_is_refused_as_bass_refuses_it(audio):
    # Cosmic Rage plays with pan -10000 when the MUD gave none. BASS won't take it, so the
    # sound stays centred; the old stand-in clamped it to hard left.
    hit = audio.play("sounds/hit.ogg", 0, -10000, 80)
    assert audio.stream("hit.ogg")["attributes"][ATTRIB_PAN] == 0.0
    audio.pan(150, hit)
    assert audio.stream("hit.ogg")["attributes"][ATTRIB_PAN] == 0.0


def test_play_answers_0_for_a_file_that_is_not_there(audio):
    assert audio.play("sounds/missing.ogg", 0) == 0
    assert audio.play("sounds/hit.ogg", 0) > 0


def test_any_loop_value_but_0_loops(audio):
    audio.play("sounds/rain.ogg", 2)
    assert audio.stream("rain.ogg")["flags"] & SAMPLE_LOOP


def test_delayed_plays_answer_nothing_and_play_once(audio):
    # The DLL plays a delayed sound from a thread and never sets its loop flag, the "looped"
    # one included.
    assert audio.playDelayLooped("sounds/rain.ogg") is None
    assert audio.playDelay("sounds/hit.ogg", 300, 0, 60) is None
    waits = sorted(delay for delay, _callback in audio.sink.scheduled)
    assert waits == [pytest.approx(0.25), pytest.approx(0.3)]
    audio.sink.run_pending()
    assert not audio.stream("rain.ogg")["flags"] & SAMPLE_LOOP
    assert audio.stream("hit.ogg")["attributes"][ATTRIB_VOL] == pytest.approx(0.6)


def test_delayed_sounds_that_have_ended_dont_pile_up(audio):
    # A pack that only ever plays sounds after a delay keeps no record of the ones that ended.
    for _ in range(5):
        audio.playDelay("sounds/hit.ogg", 10)
        audio.sink.run_pending()
        audio.fake.end_pass(audio.handle("hit.ogg"))
    assert len(audio.dll.sounds) <= 1
