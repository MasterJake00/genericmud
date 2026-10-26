"""Each MUSHclient plugin has its own script space, as it has its own Lua state in MUSHclient.

genericMud runs a pack's plugins in one Lua state. Their globals used to land in one shared
table, so the last plugin to define a name won it: the V.I. Mush bundle for Materia Magica has
69 function names that several of its plugins define (init_stuff, load_config, save_config),
and its plugins ran each other's setup and saving code.
"""

from __future__ import annotations

from pathlib import Path

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from tests.helpers import RecordingSink

A = "a" * 24
B = "b" * 24


def _plugin(plugin_id: str, name: str, script: str = "", rules: str = "") -> str:
    return (
        f"<muclient><plugin name='{name}' id='{plugin_id}'/>"
        f"<script><![CDATA[{script}]]></script>{rules}</muclient>"
    )


def _pack(*plugins: str, base_dir: Path | None = None, trusted: bool = False):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source="m", base_dir=str(base_dir) if base_dir else None)
    pack = MushclientPack(api, full_stdlib=trusted)
    for plugin in plugins:
        pack.load_source(plugin)
    return sink, engine, pack


def _notes(sink: RecordingSink) -> list[str]:
    return [text for text, _channel in sink.echoed]


def test_each_plugin_runs_its_own_function_of_a_shared_name():
    sink, _engine, pack = _pack(*(
        _plugin(plugin_id, name, (
            f"function init_stuff() Note('{name} set up') end\n"
            "function OnPluginInstall() init_stuff() end"
        ))
        for plugin_id, name in ((A, "first"), (B, "second"))
    ))
    pack.dispatch_install()
    assert _notes(sink) == ["first set up", "second set up"]


def test_a_plugins_globals_are_its_own():
    sink, _engine, pack = _pack(
        _plugin(A, "first", "config = {owner = 'first'}\n"
                "function OnPluginInstall() Note('first sees ' .. config.owner) end"),
        _plugin(B, "second",
                "function OnPluginInstall() Note('second sees ' .. tostring(config)) end"),
    )
    pack.dispatch_install()
    assert _notes(sink) == ["first sees first", "second sees nil"]


def test_send_to_script_text_runs_in_its_own_plugin():
    rules = (
        "<aliases><alias match='{name} count' enabled='y' send_to='12'>"
        "<send>counter = (counter or 0) + 1  Note('{name} ' .. counter)</send>"
        "</alias></aliases>"
    )
    sink, engine, _pack_ = _pack(*(
        _plugin(plugin_id, name, "", rules.replace("{name}", name))
        for plugin_id, name in ((A, "first"), (B, "second"))
    ))
    for command in ("first count", "second count", "first count"):
        engine.process_input(command)
    assert _notes(sink) == ["first 1", "second 1", "first 2"]


def test_rule_and_timer_scripts_call_their_own_plugins_function():
    sink, engine, pack = _pack(*(
        _plugin(
            plugin_id, name,
            f"function tick() Note('{name} tick') end\n"
            f"function hit() Note('{name} hit') end\n"
            "AddTimer('ticker', 0, 0, 1, '', timer_flag.Enabled, 'tick')",
            f"<triggers><trigger match='{name} line' enabled='y' script='hit'/></triggers>",
        )
        for plugin_id, name in ((A, "first"), (B, "second"))
    ))
    pack.dispatch_line("first line")
    engine.process_line(Line("first line"))
    sink.run_pending()  # each plugin's timer fires once
    assert _notes(sink)[0] == "first hit"
    assert sorted(_notes(sink)[1:]) == ["first tick", "second tick"]


def test_call_plugin_reaches_the_target_plugins_own_function():
    _sink, _engine, pack = _pack(*(
        _plugin(plugin_id, name, f"function whoami() return '{name}' end")
        for plugin_id, name in ((A, "first"), (B, "second"))
    ))
    assert pack._lua.eval(f"select(2, CallPlugin('{A}', 'whoami'))") == "first"
    assert pack._lua.eval(f"select(2, CallPlugin('{B}', 'whoami'))") == "second"


def test_a_plugin_can_call_its_own_hooks():
    # Packs re-run their own OnPluginInstall from a reset alias; genericMud took the hooks
    # out of the shared table to keep plugins apart, so the call did nothing.
    sink, engine, pack = _pack(_plugin(
        A, "first", "function OnPluginInstall() Note('installed') end",
        "<aliases><alias match='reset' enabled='y' send_to='12'>"
        "<send>OnPluginInstall()</send></alias></aliases>",
    ))
    pack.dispatch_install()
    engine.process_input("reset")
    assert _notes(sink) == ["installed", "installed"]


def test_loadstring_writes_into_the_plugin_that_called_it():
    # The usual way a MUSHclient plugin restores saved tables: it starts the table empty,
    # then runs the saved text, which assigns the global again.
    sink, _engine, pack = _pack(
        _plugin(A, "first", (
            "config = {}\n"
            "assert(loadstring('config = {volume = 70}'))()\n"
            "local pieces, index = {'config.muted ', '= true'}, 0\n"
            "assert(load(function() index = index + 1 return pieces[index] end))()\n"
            "function OnPluginInstall() Note(config.volume .. ' ' .. tostring(config.muted)) end"
        )),
        trusted=True,
    )
    pack.dispatch_install()
    assert _notes(sink) == ["70 true"]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="latin-1")


def test_a_module_that_defines_globals_runs_once_for_each_plugin(tmp_path):
    # Materia Magica's gmcphelper.lua defines plain global functions, and 21 of the bundle's
    # plugins require it. Each needs its own copy, reading its own globals.
    _write(tmp_path / "lua" / "helper.lua", (
        "local calls = 0\n"
        "function bump() calls = calls + 1 return calls end\n"
        "function owner() return who end\n"
    ))
    sink, _engine, pack = _pack(
        _plugin(A, "first", "require 'helper'\nwho = 'first'\n"
                "function OnPluginInstall() bump() Note(owner() .. ' ' .. bump()) end"),
        _plugin(B, "second", "require 'helper'\nwho = 'second'\n"
                "function OnPluginInstall() Note(owner() .. ' ' .. bump()) end"),
        base_dir=tmp_path, trusted=True,
    )
    pack.dispatch_install()
    assert _notes(sink) == ["first 2", "second 1"]


def test_a_module_built_with_module_is_each_plugins_own(tmp_path):
    # Lua 5.1's module() puts a library in the Lua state's globals, and each MUSHclient
    # plugin has its own. Mush-Z saves its settings with serialize.save('name'), which finds
    # the plugin's table through _G, and 35 of the V.I. bundle's plugins keep their pending
    # waits in their own copy of wait.lua.
    _write(tmp_path / "lua" / "saver.lua", (
        "module('saver', package.seeall)\n"
        "local pending = {}\n"
        "function add(item) pending[#pending + 1] = item return #pending end\n"
        "function lookup(name)\n"
        "  return assert(_G[name], 'Variable ' .. name .. ' does not exist')\n"
        "end\n"
    ))
    sink, _engine, pack = _pack(
        _plugin(A, "first", "require 'saver'\ngag = 'first gags'\nsaver.add('x')\n"
                "function OnPluginInstall()\n"
                "  Note(saver.lookup('gag') .. ' ' .. saver.add('y'))\n"
                "end"),
        _plugin(B, "second", "require 'saver'\ngag = 'second gags'\n"
                "function OnPluginInstall()\n"
                "  Note(saver.lookup('gag') .. ' ' .. saver.add('z'))\n"
                "end"),
        base_dir=tmp_path, trusted=True,
    )
    pack.dispatch_install()
    assert _notes(sink) == ["first gags 2", "second gags 1"]
    assert pack._lua.eval("rawget(_G, 'saver')") is None  # not in the shared globals


def test_miriani_next_optional_plugins_are_still_read(tmp_path):
    # Miriani-Next's requirements.xml plugin lists optional plugins in a global; genericMud
    # reads it to say which ones the pack can do without.
    _sink, _engine, pack = _pack(_plugin(
        A, "requirements", "optional_plugins = {['" + B + "'] = 'extras'}",
    ))
    pack._record_optional_plugins()
    assert ("extras.xml", "declared optional by the pack") in pack._skipped_plugins


def test_reloading_a_plugin_gives_it_a_fresh_script_space(tmp_path):
    _write(tmp_path / "worlds" / "plugins" / "plug.xml", _plugin(
        A, "plug", "loaded = (loaded or 0) + 1\nfunction OnPluginInstall() Note(loaded) end",
    ))
    _write(tmp_path / "worlds" / "world.xml", (
        "<muclient><include name='plugins/plug.xml' plugin='y'/></muclient>"
    ))
    sink, _engine, pack = _pack(base_dir=tmp_path)
    pack.load_file(str(tmp_path / "worlds" / "world.xml"))
    pack.dispatch_install()
    assert pack._lua.eval(f"ReloadPlugin('{A}')") == 0  # installs it again
    assert _notes(sink) == ["1", "1"]


def test_a_plugins_globals_start_with_genericmuds_own():
    # Penlight reads rawget(_G, 'unpack'), strict.lua replaces _G's metatable, and some
    # plugins walk pairs(_G): each finds the host functions and Lua's libraries in the
    # plugin's own globals, as in a MUSHclient plugin's own Lua state.
    sink, _engine, _pack_ = _pack(_plugin(A, "first", (
        "Note(tostring(rawget(_G, 'unpack') ~= nil) .. ' '"
        " .. tostring(rawget(_G, 'Note') ~= nil))\n"
        "local names = 0\n"
        "for _ in pairs(_G) do names = names + 1 end\n"
        "Note(tostring(names > 100))\n"
        "local strict = {}\n"
        "setmetatable(_G, strict)\n"
        "strict.__index = function(_, name)\n"
        "  error('variable ' .. name .. ' is not declared', 2)\n"
        "end\n"
        "Note('still works')\n"
    )))
    assert _notes(sink) == ["true true", "true", "still works"]


def test_each_plugin_gets_the_definitions_of_a_file_it_includes(tmp_path):
    # MUSHclient's sample plugin includes constants.lua; five vault packs' plugins do.
    _write(tmp_path / "worlds" / "plugins" / "shared.lua", (
        "MARK = 'set'\nfunction helper() return 'helped' end\n"
    ))
    sink, _engine, pack = _pack(*(
        _plugin(
            plugin_id, name,
            f"function OnPluginInstall() Note('{name} ' .. MARK .. ' ' .. helper()) end",
            "<include name='shared.lua'/>",
        )
        for plugin_id, name in ((A, "first"), (B, "second"))
    ), base_dir=tmp_path)
    pack.dispatch_install()
    assert _notes(sink) == ["first set helped", "second set helped"]


def test_a_module_the_world_required_first_is_still_each_plugins_own(tmp_path):
    _write(tmp_path / "lua" / "whose.lua", "function whose() return owner end\n")
    sink, _engine, pack = _pack(
        "<muclient><script><![CDATA[owner = 'world'\nrequire 'whose'\nNote(whose())]]>"
        "</script></muclient>",
        _plugin(A, "first", "owner = 'first'\nrequire 'whose'\n"
                "function OnPluginInstall() Note(whose()) end"),
        base_dir=tmp_path, trusted=True,
    )
    pack.dispatch_install()
    assert _notes(sink) == ["world", "first"]


def test_a_plugins_modules_are_in_its_own_package_loaded(tmp_path):
    # And a module nested under a library genericMud provides, like LuaSocket's
    # module("socket.url"), leaves the rest of that library in place.
    _write(tmp_path / "lua" / "waiter.lua", "module('waiter', package.seeall)\n")
    _write(tmp_path / "lua" / "socket" / "url.lua", (
        "module('socket.url', package.seeall)\nfunction escape(text) return text end\n"
    ))
    sink, _engine, _pack_ = _pack(
        _plugin(A, "first", (
            "require 'waiter'\nrequire 'socket.url'\n"
            "Note(type(package.loaded.waiter) .. ' ' .. type(socket.http) .. ' '"
            " .. socket.url.escape('a'))"
        )),
        base_dir=tmp_path, trusted=True,
    )
    assert _notes(sink) == ["table table a"]


def test_after_a_reload_its_hotkeys_and_ppi_functions_run_the_new_code(tmp_path):
    # Packs reload plugins when they update them (Toastush, the V.I. bundle's updater,
    # Mush-Z's plugin manager). MUSHclient finds the plugin by its id when a key is pressed,
    # and ppi calls go through CallPlugin.
    def write_plugin(version: str) -> None:
        _write(tmp_path / "worlds" / "plugins" / "plug.xml", _plugin(
            A, "plug",
            f"version = '{version}'\n"
            "function which() return version end\n"
            "local ppi = require 'ppi'\n"
            "ppi.Expose('which')\n",
            "<aliases><alias match='bindkey' enabled='y' send_to='12'>"
            "<send>AcceleratorTo('f5', \"Note('key ' .. version)\", sendto.script)</send>"
            "</alias></aliases>",
        ))

    write_plugin("v1")
    _write(tmp_path / "worlds" / "plugins" / "user.xml", _plugin(
        B, "user",
        "local ppi = require 'ppi'\n"
        f"function OnPluginInstall() helper = ppi.Load('{A}') end\n"
        "function report() Note('ppi ' .. helper.which()) end",
    ))
    _write(tmp_path / "worlds" / "world.xml", (
        "<muclient><include name='plugins/plug.xml' plugin='y'/>"
        "<include name='plugins/user.xml' plugin='y'/></muclient>"
    ))
    sink, engine, pack = _pack(base_dir=tmp_path)
    pack.load_file(str(tmp_path / "worlds" / "world.xml"))
    pack.dispatch_install()
    engine.process_input("bindkey")
    write_plugin("v2")
    assert pack._lua.eval(f"ReloadPlugin('{A}')") == 0
    engine.press_key("f5")
    pack._in_plugin(B, pack._execute_lua, "report()")
    assert _notes(sink) == ["key v2", "ppi v2"]


def test_a_failing_module_is_reported_once(tmp_path):
    _write(tmp_path / "lua" / "broken.lua", "this is not lua\n")
    _sink, _engine, pack = _pack(*(
        _plugin(plugin_id, name, "require 'broken'")
        for plugin_id, name in ((A, "first"), (B, "second"))
    ), base_dir=tmp_path)
    assert len(pack._module_errors) == 1


def test_a_deferred_script_from_an_unloaded_plugin_does_nothing():
    sink, engine, pack = _pack(_plugin(
        A, "first", "function hello() Note('hello') end\nDoAfterSpecial(1, 'hello()', 12)",
    ))
    assert pack._lua.eval(f"UnloadPlugin('{A}')") == 0
    sink.run_pending()
    assert _notes(sink) == []
    assert not sink.spoken
