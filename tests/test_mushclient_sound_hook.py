"""MUSHclient hands each sound it would play to the first plugin with an OnPluginPlaySound, and
that plugin plays it its own way. The LuaAudio packs (Erion, Empire, Mystavaria and others) take
every Sound() and trigger sound like this, their volume keys' Sound("volume=x") included. These
are MUSHclient's rules for it, from its PlaySoundFile."""

from __future__ import annotations

from pathlib import Path

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.scripting.mushclient_rules import ERROR_CODES
from genericmud.sound.bass_backend import BassSoundBackend
from genericmud.sound.bass_library import ATTRIB_VOL
from genericmud.sound.bus import SoundBus
from tests.helpers import RecordingSink
from tests.test_bass_backend import _FakeBass


def _listener(plugin_id: str, extra: str = "") -> str:
    """A plugin whose OnPluginPlaySound notes each sound it's handed in a variable."""
    return (
        f'<muclient><plugin id="{plugin_id}" name="{plugin_id}"/><script><![CDATA[\n'
        "function OnPluginPlaySound(name)\n"
        f'  local heard = GetVariable("heard_{plugin_id}") or ""\n'
        f'  SetVariable("heard_{plugin_id}", heard .. name .. ";")\n'
        f"  {extra}\n"
        "end\n"
        "]]></script></muclient>"
    )


def _pack(tmp_path, *plugins: str, world: str = "", sink=None, sound=None):
    names = []
    for index, body in enumerate(plugins):
        name = f"plugin{index}.xml"
        (tmp_path / name).write_text(body, encoding="latin-1")
        names.append(name)
    includes = "".join(f'<include name="{name}" plugin="y"/>' for name in names)
    world_file = tmp_path / "World.mcl"
    world_file.write_text(
        f'<?xml version="1.0"?><muclient><world name="W"/>{world}{includes}</muclient>',
        encoding="latin-1",
    )
    sink = sink or RecordingSink()
    engine = AutomationEngine(sink, sound=sound)
    pack = MushclientPack(
        ScriptApi(engine, source="t", base_dir=str(tmp_path)), full_stdlib=True
    )
    pack.load_file(str(world_file))
    pack.dispatch_install()
    return sink, engine, pack


def test_a_sound_goes_to_the_first_plugin_that_plays_sounds(tmp_path):
    plain = '<muclient><plugin id="plain" name="plain"/></muclient>'
    sink, engine, pack = _pack(tmp_path, plain, _listener("first"), _listener("second"))
    assert pack._execute_lua('return Sound("sounds/door.ogg")') == 0  # eOK
    assert engine.get_var("heard_first") == "sounds/door.ogg;"
    assert engine.get_var("heard_second", None) is None
    assert sink.played == []  # the plugin played it; genericMud didn't as well


def test_a_sound_played_inside_the_hook_plays_as_usual(tmp_path):
    # MUSHclient won't hand a plugin its own sound back, or loop for ever.
    (tmp_path / "beep.ogg").write_bytes(b"OggS")
    sink, engine, pack = _pack(tmp_path, _listener("taker", 'Sound("beep.ogg")'))
    pack._execute_lua('Sound("sounds/door.ogg")')
    assert engine.get_var("heard_taker") == "sounds/door.ogg;"
    assert [Path(entry["file"]).name for entry in sink.played] == ["beep.ogg"]


def test_a_triggers_sound_goes_to_the_plugin_too(tmp_path):
    world = (
        '<triggers><trigger match="You are hit" enabled="y" sound="sounds/hit.ogg"'
        ' sequence="100"/></triggers>'
    )
    sink, engine, _pack_ = _pack(tmp_path, _listener("taker"), world=world)
    engine.process_line(Line("You are hit"))
    assert engine.get_var("heard_taker") == "sounds/hit.ogg;"
    assert sink.played == []


def test_a_sound_with_no_name_reaches_no_plugin(tmp_path):
    sink, engine, pack = _pack(tmp_path, _listener("taker"))
    assert pack._execute_lua('return Sound("")') == ERROR_CODES["eNoNameSpecified"]
    assert engine.get_var("heard_taker", None) is None


def test_a_disabled_plugin_is_passed_over(tmp_path):
    sink, engine, pack = _pack(tmp_path, _listener("off"), _listener("on"))
    pack._execute_lua('EnablePlugin("off", false)')
    pack._execute_lua('Sound("sounds/door.ogg")')
    assert engine.get_var("heard_off", None) is None
    assert engine.get_var("heard_on") == "sounds/door.ogg;"


# LuaAudio's own hook, cut down to the lines that matter here: settings arrive as Sound("key=x").
_LUA_AUDIO = (
    '<muclient><plugin id="aedf0cb0be5bf045860d54b7" name="LuaAudio"/><script><![CDATA[\n'
    "function OnPluginPlaySound(file)\n"
    '  if file == "" then audio.stop(0) return end\n'
    '  local key, value = file:match("^(%a+)=(.*)$")\n'
    '  if key == "volume" then audio.volume(tonumber(value))\n'
    '  elseif key == "pan" then audio.pan(tonumber(value))\n'
    "  else audio.play(file, 0) end\n"
    "end\n"
    "function OnPluginInstall()\n"
    '  assert(package.loadlib("audio.dll", "luaopen_audio"))()\n'
    '  Sound("volume=60")\n'
    "end\n"
    "]]></script></muclient>"
)


class _BusSink(RecordingSink):
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
def lua_audio(tmp_path):
    (tmp_path / "sounds").mkdir()
    for name in ("door.ogg", "rain.ogg"):
        (tmp_path / "sounds" / name).write_bytes(b"OggS")
    fake = _FakeBass()
    bus = SoundBus(BassSoundBackend(fake))
    sink, engine, pack = _pack(tmp_path, _LUA_AUDIO, sink=_BusSink(bus), sound=bus)

    def volume_of(name):
        [state] = [state for state in fake.streams.values() if state["file"].endswith(name)]
        return state["attributes"][ATTRIB_VOL]

    return pack, volume_of


def test_a_luaaudio_pack_plays_at_the_volume_it_set_when_installed(lua_audio):
    # Every LuaAudio pack puts its saved volume back with Sound("volume=x") as it installs.
    pack, volume_of = lua_audio
    pack._execute_lua('Sound("sounds/door.ogg")')
    assert volume_of("door.ogg") == pytest.approx(0.6)


def test_a_luaaudio_packs_sounds_overlap_and_its_volume_keys_reach_them_all(lua_audio):
    # audio.play starts a sound of its own each time, where genericMud's own Sound() plays one
    # at a time; the volume keys' Sound("volume=x") re-levels every one still playing.
    pack, volume_of = lua_audio
    pack._execute_lua('Sound("sounds/rain.ogg")')
    pack._execute_lua('Sound("sounds/door.ogg")')
    pack._execute_lua('Sound("volume=25")')
    assert volume_of("rain.ogg") == pytest.approx(0.25)
    assert volume_of("door.ogg") == pytest.approx(0.25)
