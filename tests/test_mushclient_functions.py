"""The rest of the MUSHclient functions soundpacks call, behaving as MUSHclient's source does.

Each was a do-nothing stand-in before, which broke whatever used its answer: Chatpack's
trigger sounds went through Replace, and every Miriani-Next sound through GetInfo(304).
"""

from __future__ import annotations

import time
from pathlib import Path

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.render.ansi import parse_ansi
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from tests.helpers import RecordingSink

PLUGIN_ID = "aaaaaaaaaaaaaaaaaaaaaaaa"


def _pack(
    script: str = "", rules: str = "", *, sink: RecordingSink | None = None
) -> tuple[RecordingSink, AutomationEngine, MushclientPack]:
    sink = sink if sink is not None else RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(
        f"<muclient><plugin name='p' id='{PLUGIN_ID}'/>"
        f"<script><![CDATA[{script}]]></script>{rules}</muclient>"
    )
    return sink, engine, pack


def _sent(script: str) -> list[str]:
    sink, _engine, _pack_ = _pack(script)
    return sink.sent


def test_replace_swaps_text_like_mushclient():
    # Chatpack builds its sound file names with Replace; a stand-in made every name garbage.
    assert _sent(
        "Send(Replace('a-b-c', '-', '+'))\n"
        "Send(Replace('a-b-c', '-', '+', false))\n"
        "Send(Replace('abc', '', 'x'))\n"
        "Send(Replace('sounds\\\\x.wav', '\\\\', '/'))"
    ) == ["a+b+c", "a+b-c", "abc", "sounds/x.wav"]


def test_strip_ansi_and_make_regular_expression():
    assert _sent(
        "Send(StripANSI('\\27[1;31mred\\27[0m text'))\n"
        "Send(MakeRegularExpression('You hit *.'))"
    ) == ["red text", "^You hit (.*?)\\.$"]


def test_get_info_answers_numbers_dates_and_flags():
    # Miriani-Next's sounds.lua does arithmetic on GetInfo(304); "" stopped every sound.
    sent = _sent(
        "Send(type(GetInfo(304)))\n"
        "Send(tostring(GetInfo(304)))\n"
        "Send(type(GetInfo(232)))\n"
        "Send(tostring(GetInfo(106)))\n"
        "Send(tostring(GetInfo(107)))\n"
        "Send(tostring(GetInfo(113)))\n"
        "Send(tostring(GetInfo(114)))\n"
        "Send(tostring(GetInfo(222) > 0))\n"
        "Send(type(GetInfo(281)))\n"
        "Send(tostring(GetInfo(301)))\n"
        "Send(tostring(GetInfo(305)))\n"
        "Send(tostring(GetInfo(99999)))\n"
    )
    assert sent[0] == "number"
    assert abs(float(sent[1]) - time.time()) < 5
    # (the engine starts out connected, so 106, "not connected", is false)
    assert sent[2:10] == ["number", "false", "false", "true", "false", "false", "number", "nil"]
    assert 0 < time.time() - float(sent[10]) < 86400  # when genericMud started
    assert sent[11] == "nil"


def test_set_variable_to_nil_deletes_it():
    assert _sent(
        "SetVariable('x', 'set'); SetVariable('x', nil); Send(tostring(GetVariable('x')))"
    ) == ["nil"]


def test_variable_lists_and_plugin_variables():
    assert _sent(
        "SetVariable('colour', 'blue')\n"
        "local all = GetVariableList()\n"
        "Send(all.colour)\n"
        f"Send(GetPluginVariable('{PLUGIN_ID}', 'colour'))\n"
        "Send(GetPluginVariable('', 'colour'))\n"
        f"Send(tostring(GetPluginVariable('{PLUGIN_ID}', 'missing')))\n"
        "Send(tostring(GetPluginVariable('nosuchplugin', 'colour')))\n"
        f"Send(GetPluginVariableList('{PLUGIN_ID}').colour)\n"
    ) == ["blue", "blue", "blue", "nil", "nil", "blue"]


def test_plugin_supports_names_the_plugins_functions():
    # Mush-Z checks PluginSupports(id, f) == 0 before every CallPlugin.
    assert _sent(
        "function exposed() end\n"
        "function OnPluginBroadcast() end\n"
        f"Send(tostring(PluginSupports('{PLUGIN_ID}', 'exposed')))\n"
        f"Send(tostring(PluginSupports('{PLUGIN_ID}', 'missing')))\n"
        "Send(tostring(PluginSupports('nosuchplugin', 'exposed')))\n"
    ) == ["0", "30036", "30034"]


def test_call_plugin_returns_results_and_runs_as_that_plugin():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(
        "<muclient><plugin name='helper' id='bbbbbbbbbbbbbbbbbbbbbbbb'/><script><![CDATA["
        "function add(a, b) return a + b, GetPluginID() end\n"
        "function broken() error('nope') end\n"
        "]]></script></muclient>"
    )
    pack.load_source(
        f"<muclient><plugin name='caller' id='{PLUGIN_ID}'/><script><![CDATA["
        "local rc, sum, who = CallPlugin('bbbbbbbbbbbbbbbbbbbbbbbb', 'add', 2, 3)\n"
        "Send(rc .. ' ' .. sum .. ' ' .. who)\n"
        "Send(tostring((CallPlugin('nosuchplugin', 'add'))))\n"
        "Send(tostring((CallPlugin('bbbbbbbbbbbbbbbbbbbbbbbb', 'missing'))))\n"
        "Send(tostring((CallPlugin('bbbbbbbbbbbbbbbbbbbbbbbb', 'broken'))))\n"
        "Send(GetPluginID())\n"
        "]]></script></muclient>"
    )
    assert sink.sent == [
        "0 5 bbbbbbbbbbbbbbbbbbbbbbbb", "30034", "30036", "30040", PLUGIN_ID,
    ]


def test_arrays_behave_like_mushclients():
    assert _sent(
        "Send(tostring(ArrayCreate('a')))\n"
        "Send(tostring(ArrayCreate('a')))\n"
        "Send(tostring(ArraySet('a', 'nick', 'fish')))\n"
        "Send(tostring(ArraySet('a', 'nick', 'chips')))\n"
        "Send(tostring(ArraySet('missing', 'k', 'v')))\n"
        "Send(ArrayGet('a', 'nick'))\n"
        "Send(tostring(ArrayGet('a', 'nobody')))\n"
        "Send(tostring(ArrayImport('a', 'b,two,a,one,c\\\\,d,three', ',')))\n"
        "Send(ArrayExport('a', '|'))\n"
        "Send(ArrayExportKeys('a', ','))\n"
        "Send(table.concat(ArrayListKeys('a'), ','))\n"
        "Send(table.concat(ArrayListValues('a'), ','))\n"
        "Send(ArrayGetFirstKey('a') .. ' ' .. ArrayGetLastKey('a'))\n"
        "Send(tostring(ArraySize('a')) .. ' ' .. tostring(ArrayCount()))\n"
        "Send(tostring(ArrayKeyExists('a', 'a')) .. ' ' .. tostring(ArrayExists('zzz')))\n"
        "Send(tostring(ArrayDeleteKey('a', 'a')) .. ' ' .. tostring(ArrayDeleteKey('a', 'a')))\n"
        "Send(tostring(ArrayImport('a', 'odd', ',')))\n"
        "Send(tostring(ArrayExport('a', '\\\\')))\n"
        "Send(table.concat(ArrayListAll(), ','))\n"
        "Send(tostring(ArrayClear('a')) .. ' ' .. tostring(ArraySize('a')))\n"
        "Send(tostring(ArrayDelete('a')) .. ' ' .. tostring(ArrayDelete('a')))\n"
    ) == [
        "0", "30055", "0", "30060", "30056", "chips", "nil", "0",
        "a|one|b|two|c,d|three|nick|chips", "a,b,c\\,d,nick", "a,b,c,d,nick",
        "one,two,three,chips", "a nick", "4 1", "true false", "0 30061", "30057",
        "30059", "a", "0 0", "0 30056",
    ]


def test_save_state_runs_the_plugins_save_hook_and_asks_for_a_save():
    # Miriani-Next's channel history writes its database and settings in OnPluginSaveState.
    sink, _engine, pack = _pack(
        "function OnPluginSaveState() SetVariable('saved', 'yes') end\n"
        "function OnPluginInstall() Send(tostring(SaveState())) end\n"
    )
    pack.load_source(
        "<muclient><script><![CDATA[Send(tostring(SaveState()))]]></script></muclient>"
    )
    pack.dispatch_install()
    assert sink.sent == ["30035", "0"]  # the world isn't a plugin, as MUSHclient answers
    assert pack._api.get_var("saved") == "yes"
    assert sink.save_requests == 1


def test_simulate_hands_the_text_to_the_window_as_if_from_the_mud():
    sink, _engine, _pack_ = _pack("Simulate('You feel ', 'better.', '\\r\\n')")
    assert sink.simulated == ["You feel better.\r\n"]


def test_error_desc_and_the_error_tables():
    assert _sent(
        "Send(ErrorDesc(30005))\n"
        "Send(error_desc[error_code.eTriggerNotFound])\n"
        "Send(tostring(error_code.eNoSuchPlugin))\n"
        "Send(ErrorDesc(0))\n"
        "Send(ErrorDesc(12345))\n"
    ) == [
        "The specified trigger name does not exist",
        "The specified trigger name does not exist", "30034", "No error",
        "12345",
    ]


def test_a_disabled_plugin_hears_nothing_until_enabled_again():
    sink, engine, pack = _pack(
        "function OnPluginDisable() Send('disabled') end\n"
        "function OnPluginEnable() Send('enabled') end\n"
        "function OnPluginLineReceived(line) Send('heard ' .. line) end\n",
        "<triggers><trigger match='ping' enabled='y' send_to='12'><send>Send('pong')</send>"
        "</trigger></triggers>",
    )
    pack._lua.execute(f"EnablePlugin('{PLUGIN_ID}', false)")
    pack.dispatch_line("ping")
    engine.process_line(Line("ping"))
    pack._lua.execute(f"EnablePlugin('{PLUGIN_ID}', true)")
    pack.dispatch_line("ping")
    engine.process_line(Line("ping"))
    assert sink.sent == ["disabled", "enabled", "heard ping", "pong"]


def test_copy_output_is_left_to_genericmud(tmp_path):
    # It binds Ctrl+C to read a selection genericMud's output window owns.
    (tmp_path / "Copy_Output.xml").write_text(
        "<muclient><plugin name='Copy_Output' id='a0df42a8e796d974f4364245'/><script>"
        "<![CDATA[Accelerator('Ctrl+C', 'copy it')]]></script></muclient>",
        encoding="latin-1",
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m", base_dir=str(tmp_path)))
    pack.load_source(
        "<muclient><include name='Copy_Output.xml' plugin='y'/></muclient>"
    )
    assert not engine.has_key("ctrl+c")
    assert [name for name, _reason in pack._skipped_plugins] == ["Copy_Output.xml"]


def test_the_output_buffer_can_be_read_back():
    sink = RecordingSink()
    sink.lines.extend(["first line", "second line"])
    sink.styled[1] = parse_ansi("\x1b[31msecond\x1b[0m line")
    sent = _pack(
        "local count = GetLinesInBufferCount()\n"
        "Send(tostring(count))\n"
        "Send(GetLineInfo(1, 1))\n"
        "Send(tostring(GetLineInfo(2, 2)))\n"
        "Send(GetLineInfo(2).text)\n"
        "Send(tostring(GetLineInfo(9, 1)))\n"
        "Send(GetStyleInfo(2, 1, 1) .. ' ' .. tostring(GetStyleInfo(2, 1, 14)))\n"
        "local styles = GetStyleInfo(count)\n"
        "Send(tostring(#styles) .. ' ' .. styles[2].text)\n",
        sink=sink,
    )[0].sent
    assert sent == ["2", "first line", "11", "second line", "nil", "second 128", "2  line"]


def test_small_helpers_answer_sensibly():
    sink, _engine, _pack_ = _pack(
        "local r = MtRand(); Send(tostring(r >= 0 and r < 1))\n"
        "Send(EvaluateSpeedwalk('3n2e'))\n"
        "Send(EvaluateSpeedwalk('3nx'))\n"
        "Send(tostring(GetConnectDuration()))\n"
        "Send(tostring(ReloadPlugin('nosuchplugin')))\n"
        "SetStatus('ready')\n"
        "Send(tostring(DoCommand('preferences')))\n"
        "AnsiNote('\\27[32mgreen\\27[0m note')\n"
    )
    assert sink.sent == [
        "true", "n\r\nn\r\nn\r\ne\r\ne\r\n", "*Invalid speed walk string", "0",
        "30034", "30054",
    ]
    assert ("green note", "main") in sink.echoed


def test_utils_hashes_hex_base64_and_timer_match_mushclient():
    # The expected values are the examples in MUSHclient's own documentation.
    sink, _engine, _pack_ = _pack(
        "Send(utils.tohex(utils.md5('nick gammon')))\n"
        "Send(utils.hash('Nick Gammon'))\n"
        "Send(utils.tohex('Nick Gammon'))\n"
        "Send(utils.tohex(utils.sha256('nick gammon')))\n"
        "Send(utils.base64encode('Nick Gammon'))\n"
        "Send(utils.base64decode('TmljayBHYW1tb24='))\n"
        "Send(tostring(utils.edit_distance('food', 'fodder')))\n"
        "Send(type(utils.timer()))\n"
        "Send(Hash('Nick Gammon'))\n"
        "Send(tostring(EditDistance('food', 'fodder')))\n"
        "Send(Base64Encode('Nick Gammon'))\n"
    )
    assert sink.sent == [
        "9A380FD967D936AC99ED73B4A038CE8C", "fe09b07227a4e006213ac005831d55b20508a568",
        "4E69636B2047616D6D6F6E",
        "B3223193E1C89CB1E42E2BE2DF34874320F43E149DC315A381B08B7BC52849AD",
        "TmljayBHYW1tb24=", "Nick Gammon", "3", "number",
        "fe09b07227a4e006213ac005831d55b20508a568", "3", "TmljayBHYW1tb24=",
    ]


def test_miriani_next_bass_device_checks_answer_ok(tmp_path):
    # Miriani-Next checks the audio device before every sound and recovers it on an error;
    # without these every one of its sounds stopped at a nil method.
    sink = RecordingSink()
    pack = MushclientPack(ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path)))
    pack.load_source(
        "<muclient><script><![CDATA["
        "local bass = require('miriani.lib.audio.bass')()\n"
        "Send(tostring(bass:CheckDeviceHealth()))\n"
        "local ok, why = bass:RecoverFromDeviceFailure(); Send(tostring(ok))\n"
        "Send(tostring(bass:IsInitialized()))\n"
        "local stream = bass:StreamCreateFile(false, 'x.ogg', 0, 0, 0)\n"
        "Send(tostring(stream:GetPosition()) .. ' ' .. tostring(stream:GetLength()))\n"
        "Send(tostring(bass:SomethingNew()))\n"
        "]]></script></muclient>"
    )
    assert sink.sent == ["0", "true", "true", "0 0", "0"]


def test_chatpacks_own_bass_wrapper_plays_through_genericmud(tmp_path):
    # Chatpack and the yugioh pack ship the BASS wrapper Miriani-Next has, as "audio.bass".
    # It reaches bass.dll through LuaJIT's ffi, which genericMud doesn't have, so every one
    # of their sounds went nowhere.
    lua = tmp_path / "lua"
    (lua / "audio" / "bass").mkdir(parents=True)
    (lua / "audio.lua").write_text(
        "return {BASS = require('audio.bass'), CONST = require('audio.bass.constants')}",
        encoding="latin-1",
    )
    (lua / "audio" / "bass.lua").write_text(
        "local ffi = require('ffi')\nlocal class = {}\nreturn class", encoding="latin-1"
    )
    (lua / "audio" / "bass" / "constants.lua").write_text(
        "return {attribute = {volume = 2}, stream = {auto_free = 262144}}", encoding="latin-1"
    )
    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / "huh.m4a").write_bytes(b"audio")
    sink = RecordingSink()
    pack = MushclientPack(
        ScriptApi(AutomationEngine(sink), source="m", base_dir=str(tmp_path)), full_stdlib=True
    )
    pack.load_source(
        "<muclient><script><![CDATA["
        "Audio = require('audio')\n"
        "BASS = Audio.BASS()\n"
        "local stream = BASS:StreamCreateFile(false, 'sounds/huh.m4a', 0, 0, "
        "Audio.CONST.stream.auto_free)\n"
        "stream:SetAttribute(Audio.CONST.attribute.volume, 0.5)\n"
        "stream:Play()"
        "]]></script></muclient>"
    )
    assert [Path(cue["file"]).name for cue in sink.played] == ["huh.m4a"]
    assert sink.played[0]["gain"] == 0.5


def test_add_trigger_ex_sending_to_a_variable_stores_it_under_the_triggers_name():
    sink, engine, pack = _pack(
        "AddTriggerEx('hp', 'HP: *', '%1', trigger_flag.Enabled, -1, 0, '', '', "
        "sendto.variable, 100)"
    )
    pack.dispatch_line("HP: 42")
    engine.process_line(Line("HP: 42"))
    assert pack._api.get_var("hp") == "42"


def test_save_state_at_close_runs_each_plugins_save_hook_once():
    sink, _engine, pack = _pack(
        "function OnPluginSaveState() Note('saving'); SaveState() end",
        rules="",
    )
    pack._plugin_info[PLUGIN_ID]["save_state"] = True
    pack.dispatch_close()
    assert [text for text, _channel in sink.echoed] == ["saving"]


def test_style_and_line_info_types_answer_like_mushclient():
    sink = RecordingSink()
    sink.lines.append("hello world")
    sent = _pack(
        "Send(type(GetStyleInfo(1, 1, 4)) .. ' ' .. tostring(GetStyleInfo(1, 1, 4)))\n"
        "Send(type(GetStyleInfo(1, 1, 7)))\n"
        "Send(type(GetLineInfo(1, 0)))\n"
        "AddTriggerEx('t', 'a *', '', 1, -1, 0, '', '', 0, 100)\n",
        sink=sink,
    )[0].sent
    assert sent == ["number 0", "string", "table"]


def test_trigger_info_gives_the_last_wildcards_and_a_whole_second_time():
    sink, engine, pack = _pack(
        "AddTriggerEx('t', 'a * b *', '', trigger_flag.Enabled + trigger_flag.KeepEvaluating, "
        "-1, 0, '', '', 0, 100)"
    )
    pack.dispatch_line("a one b two")
    engine.process_line(Line("a one b two"))
    pack._in_plugin(  # the plugin asks about its own trigger
        PLUGIN_ID, pack._lua.execute,
        "Send(GetTriggerInfo('t', 101) .. ',' .. GetTriggerInfo('t', 102))\n"
        "Send(tostring(GetTriggerInfo('t', 22) == math.floor(GetTriggerInfo('t', 22))))",
    )
    assert sink.sent[-2:] == ["one,two", "true"]


def test_get_world_finds_this_world_and_no_other():
    # Materia Magica's GMCP handler sends the room to a mapping world only when GetWorld
    # finds that world open; the do-nothing stand-in made it send junk to the MUD.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(
        "<muclient><world name='Materia Magica'/><script><![CDATA[\n"
        "local dune = GetWorld('sandbox.dune.net')\n"
        "if dune ~= nil then Send(dune, 'room.info {}') end\n"
        "local me = GetWorld('materia magica')\n"
        "me:Note('found ' .. tostring(GetWorldById(GetWorldID()) ~= nil) .. ' '"
        " .. tostring(GetWorldIdList()[1] == GetWorldID()))\n"
        "]]></script></muclient>"
    )
    assert sink.sent == []
    assert [text for text, _channel in sink.echoed] == ["found true true"]
