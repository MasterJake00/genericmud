"""MUSHclient plugins as separate units in one shared Lua space.

Each plugin's rules, timers and functions are its own, as in MUSHclient, even though every
plugin's script runs in one Lua state here. These came out of reviewing the rule store.
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


def _engine() -> tuple[RecordingSink, AutomationEngine]:
    sink = RecordingSink()
    return sink, AutomationEngine(sink)


def _plugin(plugin_id: str, name: str, script: str = "", rules: str = "") -> str:
    return (
        f"<muclient><plugin name='{name}' id='{plugin_id}'/>"
        f"<script><![CDATA[{script}]]></script>{rules}</muclient>"
    )


def _line(pack: MushclientPack, engine: AutomationEngine, text: str) -> Line:
    pack.dispatch_line(text)
    return engine.process_line(Line(text))


def _notes(sink: RecordingSink) -> list[str]:
    return [text for text, _channel in sink.echoed]


def test_two_plugins_with_a_function_of_the_same_name_each_call_their_own():
    # Materia Magica's plugins each define OnHelp for their help alias. A rule must call its
    # own plugin's function, not one an earlier plugin left in the shared script space.
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m"))
    for plugin_id, name in ((A, "first"), (B, "second")):
        pack.load_source(_plugin(
            plugin_id, name, f"function OnHelp() Note('help for {name}') end",
            f"<aliases><alias match='{name}:help' enabled='y' script='OnHelp'/></aliases>",
        ))
    engine.process_input("first:help")
    engine.process_input("second:help")
    assert _notes(sink) == ["help for first", "help for second"]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="latin-1")


def _reloadable(tmp_path: Path, version: str) -> None:
    _write(tmp_path / "worlds" / "plugins" / "shared.xml", (
        "<muclient><triggers><trigger match='shared line' enabled='y' send_to='12'>"
        "<send>Note('shared')</send></trigger></triggers></muclient>"
    ))
    _write(tmp_path / "worlds" / "plugins" / "plug.xml", _plugin(
        B, "plug", f"function handler() Note('handler {version}') end",
        "<include name='shared.xml'/>"
        "<aliases><alias match='xtest' enabled='y' script='handler'/></aliases>",
    ))


def test_reload_plugin_runs_the_new_code_and_keeps_its_included_rules(tmp_path):
    _reloadable(tmp_path, "v1")
    _write(tmp_path / "worlds" / "world.xml", (
        "<muclient><include name='plugins/plug.xml' plugin='y'/></muclient>"
    ))
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_file(str(tmp_path / "worlds" / "world.xml"))
    engine.process_input("xtest")
    _reloadable(tmp_path, "v2")  # the plugin is edited
    assert pack._lua.eval(f"ReloadPlugin('{B}')") == 0
    engine.process_input("xtest")
    _line(pack, engine, "shared line")
    assert _notes(sink) == ["handler v1", "handler v2", "shared"]


def test_unload_then_load_plugin_installs_it_again(tmp_path):
    # Mush-Z's plugin manager removes and adds plugins this way.
    _reloadable(tmp_path, "v1")
    _write(tmp_path / "worlds" / "world.xml", (
        "<muclient><include name='plugins/plug.xml' plugin='y'/></muclient>"
    ))
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_file(str(tmp_path / "worlds" / "world.xml"))
    assert pack._lua.eval(f"UnloadPlugin('{B}')") == 0
    assert pack._lua.eval(f"IsPluginInstalled('{B}')") is False
    engine.process_input("xtest")
    assert pack._lua.eval("LoadPlugin('worlds/plugins/plug.xml')") == 0
    assert pack._lua.eval(f"IsPluginInstalled('{B}')") is True
    engine.process_input("xtest")
    assert _notes(sink) == ["handler v1"]


def test_a_plugin_loaded_while_playing_hears_its_install_hook(tmp_path):
    _write(tmp_path / "worlds" / "plugins" / "late.xml", _plugin(
        B, "late", "function OnPluginInstall() Note('late installed') end",
    ))
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_source(_plugin(A, "first"))
    pack.dispatch_install()
    pack._lua.execute("LoadPlugin('worlds/plugins/late.xml')")
    assert _notes(sink) == ["late installed"]


C = "c" * 24

# Erion's MSDP handler: MUSHclient calls OnPluginConnect only when the world connects, so a
# plugin that needs it when installed into a connected world calls it itself.
_RUNS_OWN_CONNECT = (
    "function OnPluginInstall()\n"
    "  Note('{name} installed')\n"
    "  if IsConnected() then OnPluginConnect() end\n"
    "end\n"
    "function OnPluginConnect() Note('{name} connected') end"
)


def test_a_plugin_loaded_while_connected_hears_only_its_install(tmp_path):
    _write(tmp_path / "worlds" / "plugins" / "late.xml", _plugin(
        B, "late", _RUNS_OWN_CONNECT.replace("{name}", "late"),
    ))
    _write(tmp_path / "worlds" / "plugins" / "quiet.xml", _plugin(
        C, "quiet",
        "function OnPluginInstall() Note('quiet installed') end\n"
        "function OnPluginConnect() Note('quiet connected') end",
    ))
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_source(_plugin(A, "first"))
    pack.dispatch_install()
    pack.dispatch_connect()
    pack._lua.execute("LoadPlugin('worlds/plugins/late.xml')")
    pack._lua.execute("LoadPlugin('worlds/plugins/quiet.xml')")
    assert _notes(sink) == ["late installed", "late connected", "quiet installed"]


def test_a_reloaded_plugin_hears_only_its_install(tmp_path):
    # ReloadPlugin loads the plugin as MUSHclient loads any: OnPluginInstall, no connect.
    _write(tmp_path / "worlds" / "plugins" / "late.xml", _plugin(
        B, "late", _RUNS_OWN_CONNECT.replace("{name}", "late"),
    ))
    _write(tmp_path / "worlds" / "world.xml", (
        "<muclient><include name='plugins/late.xml' plugin='y'/></muclient>"
    ))
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_file(str(tmp_path / "worlds" / "world.xml"))
    pack.dispatch_install()
    pack.dispatch_connect()
    sink.echoed.clear()
    assert pack._lua.eval(f"ReloadPlugin('{B}')") == 0
    assert _notes(sink) == ["late installed", "late connected"]


def test_a_script_hotkey_of_an_unloaded_plugin_does_nothing():
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(_plugin(
        A, "first", "function bump() Note('bump') end\n"
        "AcceleratorTo('f5', 'bump()', sendto.script)",
    ))
    assert pack._lua.eval(f"UnloadPlugin('{A}')") == 0
    engine.press_key("f5")
    assert _notes(sink) == []
    assert not sink.spoken


def test_plugins_installed_as_the_session_opens_hear_connect_once():
    # MUSHclient installs a world's plugins when it opens, before it connects. genericMud
    # installs them as the connection opens, so IsConnected() answers false until then.
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(_plugin(A, "msdp", _RUNS_OWN_CONNECT.replace("{name}", "msdp")
                             + "\nfunction OnPluginEnable() Note(tostring(GetInfo(106))) end"))
    pack.dispatch_install()
    pack.dispatch_connect()
    pack._in_plugin(A, pack._execute_lua, "OnPluginEnable()")
    assert _notes(sink) == ["msdp installed", "msdp connected", "false"]


def test_a_plain_include_that_fails_leaves_its_plugin_working(tmp_path):
    # The include's error is recorded; the plugin's own script still runs, its rules find
    # their functions, and a rule it adds later in play is bound at once.
    from genericmud.scripting.mushclient_rules import ALIAS

    _write(tmp_path / "worlds" / "plugins" / "broken.xml", (
        "<muclient><script><![CDATA[this is not lua]]></script></muclient>"
    ))
    _write(tmp_path / "worlds" / "plugins" / "plug.xml", _plugin(
        B, "plug", "function handler() Note('handler') end",
        "<include name='broken.xml'/>"
        "<aliases><alias match='xtest' enabled='y' script='handler'/></aliases>",
    ))
    _write(tmp_path / "worlds" / "world.xml", (
        "<muclient><include name='plugins/plug.xml' plugin='y'/></muclient>"
    ))
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_file(str(tmp_path / "worlds" / "world.xml"))
    assert any(name == "broken.xml" for name, _error in pack._include_errors)
    assert pack._loading_plugins == {}
    pack._in_plugin(
        B, pack._lua.execute, "AddAlias('later', 'later', '', alias_flag.Enabled, 'handler')"
    )
    assert pack._rules[ALIAS][(B, "later")].handler is not None
    engine.process_input("xtest")
    engine.process_input("later")
    assert _notes(sink) == ["handler", "handler"]


def test_the_world_files_handlers_run_around_the_plugins_hooks():
    # MUSHclient runs on_world_open once the world and its plugins are loaded, before it
    # connects, and the world's connect and disconnect handlers before the plugins'. yugioh
    # sets up its settings in OnWorldOpen. A handler starting with ! is a command.
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(
        "<muclient><world name='w' on_world_open='Opened' on_world_connect='Connected'"
        " on_world_disconnect='Dropped' on_world_close='!say bye'/><script><![CDATA[\n"
        "function Opened() Note('world open, connected ' .. tostring(IsConnected())) end\n"
        "function Connected() Note('world connect') end\n"
        "function Dropped() Note('world disconnect') end\n"
        "]]></script></muclient>"
    )
    pack.load_source(_plugin(A, "p", (
        "function OnPluginInstall() Note('plugin install') end\n"
        "function OnPluginConnect() Note('plugin connect') end\n"
        "function OnPluginDisconnect() Note('plugin disconnect') end\n"
        "function OnPluginClose() Note('plugin close') end"
    )))
    pack.dispatch_install()
    pack.dispatch_connect()
    pack.dispatch_disconnect()
    pack.dispatch_close()
    assert _notes(sink) == [
        "plugin install", "world open, connected false", "world connect", "plugin connect",
        "world disconnect", "plugin disconnect", "plugin close",
    ]
    assert sink.sent == ["say bye"]


def test_the_world_and_its_plugins_hear_it_come_to_the_front_and_go_behind():
    # MUSHclient tells the world file's focus handlers, then each plugin, when the world's
    # window comes to the front or goes behind. Miriani-Next plays its room ambience only
    # while in front, and the Yu-Gi-Oh pack pauses its music behind.
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(
        "<muclient><world name='w' on_world_get_focus='Front' on_world_lose_focus='Behind'/>"
        "<script><![CDATA[\n"
        "function Front() Note('world front') end\n"
        "function Behind() Note('world behind') end\n"
        "]]></script></muclient>"
    )
    pack.load_source(_plugin(A, "p", (
        "function OnPluginGetFocus() Note('plugin front') end\n"
        "function OnPluginLoseFocus() Note('plugin behind') end"
    )))
    pack.dispatch_install()
    pack.dispatch_focus(True)
    pack.dispatch_focus(False)
    assert _notes(sink) == ["world front", "plugin front", "world behind", "plugin behind"]


def test_a_disabled_plugins_alias_lets_the_command_through():
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(_plugin(
        A, "first", "",
        "<aliases><alias match='hello' enabled='y' send_to='12'><send>Note('alias')</send>"
        "</alias></aliases>",
    ))
    pack._lua.execute(f"EnablePlugin('{A}', false)")
    assert engine.process_input("hello") == ["hello"]
    pack._lua.execute(f"EnablePlugin('{A}', true)")
    assert engine.process_input("hello") == []
    assert _notes(sink) == ["alias"]


def test_a_trigger_deleted_or_changed_earlier_in_the_line_no_longer_acts():
    rules = (
        "<triggers>"
        "<trigger name='a' match='x' enabled='y' keep_evaluating='y' sequence='10' "
        "send_to='12'><send>CHANGE</send></trigger>"
        "<trigger name='b' match='x' enabled='y' sequence='20' send_to='12'>"
        "<send>Note('B')</send></trigger>"
        "<trigger name='c' match='x' enabled='y' sequence='30' send_to='12'>"
        "<send>Note('C')</send></trigger>"
        "</triggers>"
    )
    for change in (
        "DeleteTrigger('b')",
        "SetTriggerOption('b', 'match', 'zzz')",
        "AddTriggerEx('b', 'zzz', '', 1+1024+8, -1, 0, '', '', 12, 20)",
    ):
        sink, engine = _engine()
        pack = MushclientPack(ScriptApi(engine, source="m"))
        pack.load_source(_plugin(A, "p", "", rules.replace("CHANGE", change)))
        _line(pack, engine, "x")
        assert _notes(sink) == ["C"], change


def test_a_plugin_only_reaches_its_own_rules_and_timers_by_name():
    # Two plugins both name a trigger "temp", as shared helper libraries do. Deleting or
    # switching off your own must leave the other plugin's alone, as in MUSHclient.
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(_plugin(
        A, "first", "AddTimer('tick', 0, 0, 5, '', 1, '')",
        "<triggers><trigger name='temp' match='ping' group='g' enabled='y' "
        "keep_evaluating='y' send_to='12'><send>Note('first')</send></trigger></triggers>",
    ))
    pack.load_source(_plugin(
        B, "second",
        "Note(tostring(IsTrigger('temp')))\n"
        "Note(tostring(DeleteTrigger('temp')))\n"
        "Note(tostring(EnableTriggerGroup('g', false)))\n"
        "Note(tostring(DeleteTimer('tick')))\n",
    ))
    _line(pack, engine, "ping")
    assert _notes(sink) == ["30005", "30005", "0", "30017", "first"]


def test_functions_a_plugin_exposes_run_as_that_plugin():
    # ppi.Load hands another plugin these functions; they work on their own plugin's rules.
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(_plugin(
        A, "helper",
        "local ppi = require 'ppi'\n"
        "function quiet() return EnableTrigger('beep', false), GetPluginID() end\n"
        "ppi.Expose('quiet')\n",
        "<triggers><trigger name='beep' match='beep' enabled='y' send_to='12'>"
        "<send>Note('beep')</send></trigger></triggers>",
    ))
    pack.load_source(_plugin(
        B, "user",
        "local ppi = require 'ppi'\n"
        f"local helper = ppi.Load('{A}')\n"
        "local code, who = helper.quiet()\n"
        "Note(tostring(code) .. ' ' .. who .. ' ' .. GetPluginID())\n",
    ))
    _line(pack, engine, "beep")
    assert _notes(sink) == [f"0 {A} {B}"]


def test_broadcast_plugin_reaches_the_others_and_counts_them():
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m"))
    for plugin_id, name in ((A, "sender"), (B, "listener")):
        pack.load_source(_plugin(
            plugin_id, name,
            f"function OnPluginBroadcast(msg, id, from, text) Note('{name} heard ' .. text) end",
        ))
    pack._current_plugin = A
    try:
        assert pack._lua.eval("BroadcastPlugin(1, 'hi')") == 1
    finally:
        pack._current_plugin = "world"
    assert _notes(sink) == ["listener heard hi"]


def test_mushclient_aliases_stop_only_their_own_plugin_and_swallow_the_command():
    # Mush-Z's cast alias keeps evaluating and sends the cast itself; genericMud also sent
    # what was typed, so every spell went out twice. One plugin's alias no longer stops
    # another plugin's alias for the same command either.
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(_plugin(
        A, "first", "",
        "<aliases><alias match='^c (.+)$' regexp='y' enabled='y' keep_evaluating='y' "
        "send_to='12'><send>Send('cast %1')</send></alias>"
        "<alias match='^c (.+)$' regexp='y' enabled='y' sequence='200' send_to='12'>"
        "<send>Note('same plugin, later')</send></alias></aliases>",
    ))
    pack.load_source(_plugin(
        B, "second", "",
        "<aliases><alias match='^c (.+)$' regexp='y' enabled='y' send_to='12'>"
        "<send>Note('other plugin')</send></alias></aliases>",
    ))
    assert engine.process_input("c fireball") == []
    assert sink.sent == ["cast fireball"]
    assert sorted(_notes(sink)) == ["other plugin", "same plugin, later"]


def _described(plugin_id: str, name: str, script: str = "", rules: str = "") -> str:
    return (
        f"<muclient><plugin name='{name}' id='{plugin_id}' author='Ruthgul' purpose='less spam'"
        " language='Lua' date_written='2012-04-27 18:46:16' date_modified='2014-01-20'"
        " requires='4.71' version='1.5' sequence='-50' save_state='y'>"
        "<description trim='y'>\n\n  Gags most melee attacks.\n\n</description></plugin>"
        f"<script><![CDATA[{script}]]></script>{rules}</muclient>"
    )


def test_get_plugin_info_answers_every_type_mushclient_does():
    import datetime

    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(_described(
        A, "sbrief", "function report(...) for _, n in ipairs({...}) do"
        " Note(n .. '=' .. tostring(GetPluginInfo(GetPluginID(), n))) end end",
        "<triggers><trigger match='x' enabled='y' send_to='12'><send>Note('x')</send>"
        "</trigger></triggers><aliases><alias match='y' enabled='y'/></aliases>",
    ))
    pack.load_source(_plugin(B, "other"))
    pack._in_plugin(A, pack._execute_lua, "report(1, 2, 3, 5, 7, 8, 9, 10, 11, 15, 16, 17, "
                    "18, 19, 21, 23, 25)")
    written = int(datetime.datetime(2012, 4, 27, 18, 46, 16).timestamp())
    modified = int(datetime.datetime(2014, 1, 20).timestamp())
    assert _notes(sink) == [
        "1=sbrief", "2=Ruthgul", "3=Gags most melee attacks.", "5=Lua", f"7={A}",
        "8=less spam", "9=1", "10=1", "11=0", "15=true", "16=true", "17=true", "18=4.71",
        "19=1.5", "21=1", "23=", "25=-50",
    ]
    assert pack._lua.eval(f"GetPluginInfo('{A}', 13)") == written
    assert pack._lua.eval(f"GetPluginInfo('{A}', 14)") == modified
    assert pack._lua.eval(f"GetPluginInfo('{B}', 13)") is None  # no date given
    assert pack._lua.eval(f"GetPluginInfo('{B}', 21)") == 2
    assert pack._lua.eval(f"GetPluginInfo('{A}', 4)").startswith("function report")
    assert pack._lua.eval(f"GetPluginInfo('{A}', 99)") is None
    assert pack._lua.eval("GetPluginInfo('nobody', 1)") is None


def test_a_called_plugin_knows_who_called_it():
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(_plugin(
        A, "callee", "function who() Note('called by ' .. GetPluginInfo(GetPluginID(), 23)) end",
    ))
    pack.load_source(_plugin(B, "caller", f"CallPlugin('{A}', 'who')"))
    pack._lua.execute(f"CallPlugin('{A}', 'who')")  # the world isn't a plugin
    assert _notes(sink) == [f"called by {B}", "called by "]


def test_materia_magica_help_prints_the_plugins_description():
    # Each of the V.I. bundle's plugins answers "<name> help" with its description and the
    # date it was last changed; both came out blank, and os.date choked on the date.
    sink, engine = _engine()
    pack = MushclientPack(ScriptApi(engine, source="m"), full_stdlib=True)  # trusted, as it is
    pack.load_source(_described(
        A, "sbrief",
        "function OnHelp()\n"
        "  ColourNote('silver', 'black', world.GetPluginInfo(world.GetPluginID(), 3))\n"
        "  ColourNote('silver', 'black', '(this version: ' .."
        " os.date('%Y', GetPluginInfo(GetPluginID(), 14)) .. ')')\n"
        "end",
        "<aliases><alias match='sbrief help' enabled='y' script='OnHelp'/></aliases>",
    ))
    assert engine.process_input("sbrief help") == []
    assert _notes(sink) == ["Gags most melee attacks.", "(this version: 2014)"]
