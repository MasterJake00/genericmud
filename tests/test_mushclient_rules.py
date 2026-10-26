"""MUSHclient triggers and aliases the way packs manage them at runtime.

ImportXML (Mush-Z brings in its whole sound set through one call), the list, info and option
calls its trigger manager uses, @variables in what a trigger matches, and send text filled
the way MUSHclient fills it. Expected behaviour follows MUSHclient's own source.
"""

from __future__ import annotations

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.render.ansi import parse_ansi
from genericmud.scripting import mushclient_compat
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from tests.helpers import RecordingSink


def _pack(
    script: str = "", rules: str = ""
) -> tuple[RecordingSink, AutomationEngine, MushclientPack]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(
        "<muclient><plugin name='p' id='aaaaaaaaaaaaaaaaaaaaaaaa'/>"
        f"<script><![CDATA[{script}]]></script>{rules}</muclient>"
    )
    return sink, engine, pack


def _line(pack: MushclientPack, engine: AutomationEngine, text: str, spans=None) -> Line:
    """A line from the MUD, fed the way the app feeds it: the pack hears it, then triggers."""
    pack.dispatch_line(text)
    return engine.process_line(Line(text, spans=spans or []))


class _RaisingPattern:
    """A compiled pattern that blows the engine's per-match time budget."""

    pattern = "slow"

    def search(self, text, **kwargs):
        raise TimeoutError("match budget exceeded")

    def match(self, text, **kwargs):
        raise TimeoutError("match budget exceeded")


# --- ImportXML ---


def test_import_xml_adds_triggers_that_play_their_sounds():
    # Mush-Z's sound_xml.lua: 857 triggers in one ImportXML call, each playing its cue.
    sink, engine, pack = _pack(
        "count = ImportXML([=[\n<triggers>\n"
        '<trigger enabled="y" match="Your skin swells and hardens*" send_to="14" '
        'sequence="100">\n<send>Sound("druid/barkskin.ogg")</send></trigger>\n'
        "</triggers>]=])\n"
        "Send(tostring(count))"
    )
    _line(pack, engine, "Your skin swells and hardens, taking on the texture of tree bark.")
    assert sink.sent == ["1"]
    assert [cue["file"].replace("\\", "/") for cue in sink.played] == ["druid/barkskin.ogg"]


def test_imported_alias_calls_a_function_defined_after_the_import():
    # Krinnzenn's help.lua imports its sphelp aliases, then defines their functions.
    sink, engine, _pack_ = _pack(
        "ImportXML([[<aliases>\n"
        '<alias name="sphelpIndex" match="sphelp" group="sphelp" script="sphelpIndex" '
        'enabled="y" send_to="12" ignore_case="y" sequence="100"></alias>\n'
        '<alias name="sphelpFile" match="sphelp *" group="sphelp" script="sphelp" '
        'enabled="y" send_to="12" ignore_case="y" sequence="100"></alias>\n'
        "</aliases>]])\n"
        "function sphelp(name, line, wildcards) Send('help ' .. wildcards[1]) end\n"
        "function sphelpIndex() Send('index') end\n"
    )
    assert engine.process_input("sphelp readme") == []
    assert engine.process_input("SPHELP") == []
    assert sink.sent == ["help readme", "index"]


def test_a_capitalised_function_defined_after_the_import_is_called():
    # An undefined CapWords global reads as the do-nothing stand-in, so looking the
    # function up when the trigger is added would keep that stand-in forever.
    sink, engine, pack = _pack(
        "ImportXML([[<triggers><trigger enabled='y' match='ping' script='OnPing'/>"
        "</triggers>]])\n"
        "function OnPing(name, line) Send('pong ' .. line) end\n"
    )
    _line(pack, engine, "ping")
    assert sink.sent == ["pong ping"]


def test_import_xml_counts_what_it_added_and_reports_bad_input():
    sink, _engine, _pack_ = _pack(
        "Send(tostring(ImportXML('just some text')))\n"
        "Send(tostring(ImportXML('<triggers><trigger match=\"x\"')))\n"
        "Send(tostring(ImportXML('<triggers></triggers>')))\n"
        "Send(tostring(ImportXML([[<triggers><trigger match='a' enabled='y'/>"
        "<trigger match='b' enabled='y'/></triggers>"
        "<aliases><alias match='c' enabled='y'/></aliases>"
        "<variables><variable name='v'>5</variable></variables>]])))\n"
        "Send(GetVariable('v'))"
    )
    # MUSHclient's methods_xml.cpp answers -1 both for text that isn't XML and for XML
    # that doesn't parse.
    assert sink.sent == ["-1", "-1", "0", "4", "5"]


def test_import_xml_replaces_a_rule_of_the_same_name_ignoring_case():
    sink, engine, pack = _pack(
        "ImportXML([[<triggers><trigger name='Greet' match='hello' enabled='y' "
        "send_to='12'><send>Send('old')</send></trigger></triggers>]])\n"
        "ImportXML([[<triggers><trigger name='greet' match='hi' enabled='y' "
        "send_to='12'><send>Send('new')</send></trigger></triggers>]])\n"
    )
    _line(pack, engine, "hello")
    _line(pack, engine, "hi")
    assert sink.sent == ["new"]


def test_importing_the_same_unnamed_trigger_twice_adds_it_once():
    xml = (
        "<triggers><trigger match='ping' enabled='y' send_to='12'>"
        "<send>Send('pong')</send></trigger></triggers>"
    )
    sink, engine, pack = _pack(
        f"Send(tostring(ImportXML([[{xml}]]))); Send(tostring(ImportXML([[{xml}]])))"
    )
    _line(pack, engine, "ping")
    assert sink.sent == ["1", "0", "pong"]


def test_import_xml_adds_timers():
    sink, _engine, _pack_ = _pack(
        "ImportXML([[<timers><timer name='tick' second='2' enabled='y' send_to='12'>"
        "<send>Send('tick')</send></timer></timers>]])"
    )
    sink.run_pending()
    assert sink.sent == ["tick"]


def test_set_trigger_option_script_points_a_trigger_at_a_function():
    # addxml.lua asserts ImportXML(...) == 1, then sets the script by name.
    sink, engine, pack = _pack(
        "function hit(name, line, wildcards) Send(name .. ':' .. wildcards[1]) end\n"
        "assert(ImportXML([[<triggers><trigger name='hit' match='You hit *.' "
        "enabled='y'><send></send></trigger></triggers>]]) == 1)\n"
        "Send(tostring(SetTriggerOption('hit', 'script', 'hit')))"
    )
    _line(pack, engine, "You hit the orc.")
    assert sink.sent == ["0", "hit:the orc"]


def test_time_spent_importing_does_not_count_against_the_script(monkeypatch):
    # Mush-Z's modules run their ImportXML calls inside the plugin's script, which has a time
    # limit. Seconds of genericMud's own work adding 857 triggers mustn't use it up.
    clock = [1000.0]
    monkeypatch.setattr(mushclient_compat.time, "monotonic", lambda: clock[0])
    sink = RecordingSink()
    pack = MushclientPack(ScriptApi(AutomationEngine(sink), source="m"))
    register = pack._register_rule

    def slow_register(rule):
        clock[0] += 0.5
        register(rule)

    monkeypatch.setattr(pack, "_register_rule", slow_register)
    pack.load_source(
        "<muclient><script><![CDATA["
        "ImportXML([[<triggers><trigger match='a' enabled='y'/><trigger match='b' "
        "enabled='y'/><trigger match='c' enabled='y'/></triggers>]])\n"
        "local n = 0 for i = 1, 200000 do n = n + i end\n"
        "Send('done')]]></script></muclient>"
    )
    assert sink.sent == ["done"]


def test_an_equals_quote_in_send_text_does_not_swallow_the_rules_after_it():
    # Mush-Z's send text runs Execute("history_add all=" .. msg). Repairing attribute values
    # used to read that =" as the start of one and escape every tag up to the next quote,
    # so its whole sound module (857 triggers) failed to load.
    sink, engine, pack = _pack(
        rules="<triggers>"
        '<trigger match="one" enabled="y" send_to="12"><send>local msg = "x"\n'
        'Execute("say all=" .. msg)</send></trigger>'
        '<trigger match="two" enabled="y" send_to="12"><send>Send("two")</send></trigger>'
        "</triggers>"
    )
    _line(pack, engine, "one")
    _line(pack, engine, "two")
    assert sink.sent == ["say all=x", "two"]


def test_a_plugin_script_finds_its_own_rules_as_it_loads():
    # MUSHclient adds a file's rules before running its script, so the script can switch a
    # group on as it loads, and an ImportXML of a rule the file also has replaces it quietly
    # (Mush-Z's filters.lua does this to two of its plugin's aliases).
    sink, engine, pack = _pack(
        "EnableTriggerGroup('sounds', true)\n"
        "ImportXML([[<aliases><alias name='go' match='go' enabled='y' send_to='12'>"
        "<send>Send('imported')</send></alias></aliases>]])",
        "<triggers><trigger match='ping' group='sounds' send_to='12'><send>Send('pong')</send>"
        "</trigger></triggers>"
        "<aliases><alias name='go' match='go' enabled='y' send_to='12'><send>Send('file')</send>"
        "</alias></aliases>",
    )
    _line(pack, engine, "ping")
    engine.process_input("go")
    assert sink.sent == ["pong", "imported"]
    assert pack._rule_errors == []


# --- what Mush-Z's trigger manager asks about triggers ---


def test_trigger_list_names_unnamed_triggers_and_reports_their_groups():
    sink, _engine, _pack_ = _pack(
        "ImportXML([[<triggers>"
        "<trigger name='a' match='alpha' group='g1' enabled='y'/>"
        "<trigger match='beta' group='g1' enabled='y'/>"
        "</triggers>]])\n"
        "local names = GetTriggerList()\n"
        "table.sort(names)\n"
        "Send(tostring(#names))\n"
        "Send(tostring(string.match(names[1], '^%*trigger') ~= nil))\n"
        "Send(names[2])\n"
        "Send(GetTriggerInfo(names[1], 26))\n"
        "Send(GetTriggerInfo(names[1], 1))\n"
        "Send(tostring(GetTriggerInfo('A', 8)))\n"
        "for _, t in ipairs(GetTriggerList()) do\n"
        "  if GetTriggerInfo(t, 26) == 'g1' then SetTriggerOption(t, 'group', 'g2') end\n"
        "end\n"
        "Send(GetTriggerOption('a', 'group'))\n"
        "Send(GetTriggerInfo(names[1], 26))\n"
    )
    assert sink.sent == ["2", "true", "a", "g1", "beta", "true", "g2", "g2"]


def test_trigger_options_read_and_write_like_mushclient():
    sink, engine, pack = _pack(
        "ImportXML([[<triggers><trigger name='t' match='ping' enabled='y' "
        "omit_from_output='y' sequence='50' send_to='12'><send>Send('pong')</send>"
        "</trigger></triggers>]])\n"
        "Send(tostring(GetTriggerOption('t', 'enabled')))\n"
        "Send(tostring(GetTriggerOption('t', 'omit_from_output')))\n"
        "Send(tostring(GetTriggerOption('t', 'sequence')))\n"
        "Send(GetTriggerOption('t', 'match'))\n"
        "Send(GetTriggerOption('t', 'send'))\n"
        "Send(tostring(GetTriggerOption('t', 'no_such_option')))\n"
        "Send(tostring(GetTriggerOption('missing', 'enabled')))\n"
        "Send(tostring(SetTriggerOption('t', 'omit_from_output', false)))\n"
        "Send(tostring(SetTriggerOption('t', 'match', 'pang')))\n"
        "Send(tostring(SetTriggerOption('t', 'regexp', 'y')))\n"
        "Send(tostring(SetTriggerOption('t', 'no_such_option', 'y')))\n"
        "Send(tostring(SetTriggerOption('missing', 'enabled', 'y')))\n"
        "Send(tostring(SetTriggerOption('t', 'sequence', 'lots')))\n"
    )
    assert sink.sent == [
        "1", "1", "50", "ping", "Send('pong')", "nil", "nil",
        "0", "0", "30032", "30025", "30005", "30026",
    ]
    sink.sent.clear()
    assert _line(pack, engine, "ping").gagged is False
    line = _line(pack, engine, "pang")
    assert sink.sent == ["pong"]
    assert line.gagged is False  # omit_from_output was switched off


def test_is_trigger_and_delete_trigger_cover_rules_from_the_file():
    sink, engine, pack = _pack(
        "function OnPluginInstall()\n"
        "  Send(tostring(IsTrigger('Fromfile')))\n"
        "  Send(tostring(IsTrigger('missing')))\n"
        "  Send(tostring(DeleteTrigger('fromfile')))\n"
        "  Send(tostring(IsTrigger('fromfile')))\n"
        "  Send(tostring(DeleteTrigger('fromfile')))\n"
        "end\n",
        "<triggers><trigger name='fromfile' match='ping' enabled='y' send_to='12'>"
        "<send>Send('pong')</send></trigger></triggers>",
    )
    pack.dispatch_install()
    _line(pack, engine, "ping")
    assert sink.sent == ["0", "30005", "0", "30005", "30005"]
    assert engine._triggers == []  # the deleted trigger's engine rule went with it


def test_trigger_list_is_nil_when_there_are_none():
    sink, _engine, _pack_ = _pack(
        "Send(tostring(GetTriggerList())); Send(tostring(GetAliasList())); "
        "Send(tostring(GetTimerList()))"
    )
    assert sink.sent == ["nil", "nil", "nil"]


def test_the_same_trigger_name_in_two_plugins_both_load():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"))
    plugins = (("b" * 24, "one"), ("c" * 24, "two"))
    for plugin_id, reply in plugins:
        pack.load_source(
            f"<muclient><plugin name='{reply}' id='{plugin_id}'/><triggers>"
            f"<trigger name='gag' match='ping' enabled='y' send_to='12'>"
            f"<send>Send('{reply}')</send></trigger></triggers></muclient>"
        )
    _line(pack, engine, "ping")
    assert sorted(sink.sent) == ["one", "two"]


def test_a_repeated_name_in_one_file_keeps_the_first_like_mushclient():
    sink, engine, pack = _pack(
        rules="<triggers>"
        "<trigger name='dup' match='ping' enabled='y' send_to='12'><send>Send('first')</send>"
        "</trigger>"
        "<trigger name='dup' match='ping' enabled='y' send_to='12'><send>Send('second')</send>"
        "</trigger></triggers>"
    )
    _line(pack, engine, "ping")
    assert sink.sent == ["first"]


# --- the order triggers are tried in, and stopping ---


def test_a_trigger_that_stops_evaluating_stops_only_its_own_plugin():
    # Mush-Z's catch-all channel trigger (sequence 200) only fires when none of its specific
    # ones (80 to 100) matched, because a match stops the rest of that plugin's triggers.
    # Another plugin's triggers, and the player's own, still hear the line.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(
        f"<muclient><plugin name='one' id='{'d' * 24}'/><triggers>"
        "<trigger match='^zchn:(.+)$' regexp='y' enabled='y' sequence='200' send_to='12'>"
        "<send>Send('fallback')</send></trigger>"
        "<trigger match='^zchn:You warn' regexp='y' enabled='y' sequence='80' send_to='12'>"
        "<send>Send('warn')</send></trigger>"
        "</triggers></muclient>"
    )
    pack.load_source(
        f"<muclient><plugin name='two' id='{'e' * 24}'/><triggers>"
        "<trigger match='^zchn:' regexp='y' enabled='y' sequence='300' send_to='12'>"
        "<send>Send('other plugin')</send></trigger>"
        "</triggers></muclient>"
    )
    engine.add_trigger("zchn", lambda _ctx: sink.sent.append("player"), source="player")
    _line(pack, engine, "zchn:You warn the group, 'run'")
    assert sorted(sink.sent) == ["other plugin", "player", "warn"]
    sink.sent.clear()
    _line(pack, engine, "zchn:something new")
    assert sorted(sink.sent) == ["fallback", "other plugin", "player"]


def test_triggers_of_equal_sequence_are_tried_in_match_text_order():
    # MUSHclient sorts triggers by sequence, then by their match text.
    sink, engine, pack = _pack(
        rules="<triggers>"
        "<trigger match='zebra *' enabled='y' send_to='12'><send>Send('zebra')</send></trigger>"
        "<trigger match='* crosses' enabled='y' send_to='12'><send>Send('star')</send>"
        "</trigger></triggers>"
    )
    _line(pack, engine, "zebra crosses")
    assert sink.sent == ["star"]


# --- enabling, and aliases that are switched off ---


def test_a_disabled_alias_lets_the_command_through():
    sink, engine, pack = _pack(
        "function OnPluginInstall() EnableAlias('k', true) end",
        "<aliases><alias name='k' match='foo' enabled='n' send_to='12'>"
        "<send>Send('bar')</send></alias></aliases>",
    )
    assert engine.process_input("foo") == ["foo"]
    pack.dispatch_install()
    assert engine.process_input("foo") == []
    assert sink.sent == ["bar"]


def test_enabling_a_trigger_does_not_rearm_one_the_engine_stopped():
    _sink, engine, pack = _pack(
        "function OnPluginInstall() EnableTrigger('slow', true) end",
        "<triggers><trigger name='slow' match='x' enabled='y' send_to='12'>"
        "<send>Send('x')</send></trigger></triggers>",
    )
    (rule,) = engine._triggers
    rule.pattern = _RaisingPattern()
    _line(pack, engine, "x")
    assert rule.enabled is False
    pack.dispatch_install()
    assert rule.enabled is False


def test_add_alias_registers_an_alias_with_mushclient_flags():
    # Toastush makes its aliases with AddAlias(name, match, response, flags, "").
    sink, engine, _pack_ = _pack(
        "Send(tostring(AddAlias('greet', 'hi *', 'say hello %1', "
        "alias_flag.Enabled + alias_flag.IgnoreAliasCase, '')))\n"
        "Send(tostring(AddAlias('greet', 'hey', 'say hey', alias_flag.Enabled, '')))\n"
        "Send(tostring(IsAlias('greet')))\n"
        "Send(GetAliasOption('greet', 'match'))\n"
    )
    assert engine.process_input("HI bob") == []
    assert sink.sent == ["0", "30011", "0", "hi *", "say hello bob"]


def test_alias_list_info_and_delete():
    sink, engine, _pack_ = _pack(
        "ImportXML([[<aliases><alias name='a' match='go' group='moves' enabled='y'>"
        "<send>north</send></alias></aliases>]])\n"
        "Send(table.concat(GetAliasList(), ','))\n"
        "Send(GetAliasInfo('a', 16))\n"
        "Send(tostring(GetAliasInfo('a', 6)))\n"
        "Send(tostring(SetAliasOption('a', 'send', 'south')))\n"
        "Send(tostring(DeleteAlias('nope')))\n"
    )
    assert engine.process_input("go") == []
    assert sink.sent == ["a", "moves", "true", "0", "30010", "south"]


# --- timers ---


def test_timer_list_info_and_options():
    # Mush-Z's timer helpers read the temporary flag (14) and group (19).
    sink, _engine, _pack_ = _pack(
        "ImportXML([[<timers><timer name='beat' second='5' group='pulse' enabled='y' "
        "temporary='y' send_to='12'><send>Send('beat')</send></timer></timers>]])\n"
        "Send(table.concat(GetTimerList(), ','))\n"
        "Send(tostring(GetTimerInfo('beat', 14)))\n"
        "Send(GetTimerInfo('beat', 19))\n"
        "Send(tostring(GetTimerInfo('beat', 3)))\n"
        "Send(tostring(GetTimerInfo('beat', 6)))\n"
        "Send(tostring(IsTimer('beat')))\n"
        "Send(tostring(IsTimer('nope')))\n"
        "Send(tostring(SetTimerOption('beat', 'enabled', 'n')))\n"
        "Send(tostring(GetTimerOption('beat', 'enabled')))\n"
        "Send(tostring(GetTimerInfo('nope', 6)))\n"
    )
    sink.run_pending()
    assert sink.sent == ["beat", "true", "pulse", "5", "true", "0", "30017", "0", "0", "nil"]


# --- @variables in what a trigger matches ---


def test_trigger_match_expands_variables_like_mushclient():
    sink, engine, pack = _pack(
        "SetVariable('target', 'a.b')\nSetVariable('alt', '(?:x|y)')\n",
        "<triggers>"
        "<trigger match='^hit @target$' regexp='y' expand_variables='y' enabled='y' "
        "send_to='12'><send>Send('hit')</send></trigger>"
        "<trigger match='^@!alt now$' regexp='y' expand_variables='y' enabled='y' "
        "send_to='12'><send>Send('alt')</send></trigger>"
        "<trigger match='mail for @@home' expand_variables='y' enabled='y' send_to='12'>"
        "<send>Send('at')</send></trigger>"
        "<trigger match='^@nothing(\\d+) coins$' regexp='y' expand_variables='y' "
        "enabled='y' send_to='12'><send>Send('coins')</send></trigger>"
        "</triggers>",
    )
    for text in ("hit axb", "hit a.b", "y now", "mail for @home", "12 coins"):
        _line(pack, engine, text)
    assert sink.sent == ["hit", "alt", "at", "coins"]


def test_trigger_match_follows_the_variable_as_it_changes():
    sink, engine, pack = _pack(
        "SetVariable('who', 'Bob')\n"
        "function OnPluginInstall() SetVariable('who', 'Ann') end\n",
        "<triggers><trigger match='@who waves.' expand_variables='y' enabled='y' "
        "send_to='12'><send>Send('wave')</send></trigger></triggers>",
    )
    _line(pack, engine, "Bob waves.")
    pack.dispatch_install()
    _line(pack, engine, "Bob waves.")
    _line(pack, engine, "Ann waves.")
    assert sink.sent == ["wave", "wave"]


# --- send text, filled the way MUSHclient fills it ---


def test_wildcards_sent_to_a_script_are_escaped_for_lua():
    sink, engine, pack = _pack(
        rules="<triggers><trigger match='^(.*) says hi$' regexp='y' enabled='y' "
        "send_to='12'><send>Send(\"%1\")</send></trigger></triggers>"
    )
    _line(pack, engine, 'Bob "the\\builder" says hi')
    assert sink.sent == ['Bob "the\\builder"']


def test_send_text_backslash_sequences_are_converted_first():
    # Mush-Z writes Simulate("%1\\r\\n"): MUSHclient turns \\ into \ before Lua sees it.
    sink, engine, pack = _pack(
        rules="<triggers><trigger match='tab' enabled='y' send_to='12'>"
        "<send>Send(\"a\\\\tb\")</send></trigger></triggers>"
    )
    _line(pack, engine, "tab")
    assert sink.sent == ["a\tb"]


def test_send_text_expands_variables_when_asked():
    sink, engine, pack = _pack(
        "SetVariable('target', 'orc')",
        "<triggers>"
        "<trigger match='fight' enabled='y' expand_variables='y'><send>kill @target</send>"
        "</trigger>"
        "<trigger match='plain' enabled='y'><send>kill @target</send></trigger>"
        "</triggers>",
    )
    _line(pack, engine, "fight")
    _line(pack, engine, "plain")
    assert sink.sent == ["kill orc", "kill @target"]


def test_script_gets_named_wildcards_the_whole_match_and_style_runs():
    sink, engine, pack = _pack(
        "function waved(name, line, wildcards, styles)\n"
        "  Send(wildcards.who); Send(wildcards[1]); Send(wildcards[0])\n"
        "  Send(styles[1].text); Send(tostring(styles[1].textcolour))\n"
        "  Send(tostring(styles[2].length))\n"
        "end\n",
        "<triggers><trigger match='^(?P<who>\\w+) waves$' regexp='y' enabled='y' "
        "script='waved'/></triggers>",
    )
    _line(pack, engine, "Bob waves", spans=parse_ansi("\x1b[31mBob\x1b[0m waves"))
    assert sink.sent == ["Bob", "Bob", "Bob waves", "Bob", "128", "6"]


def test_script_after_omit_can_reprint_the_line_in_its_colours():
    # Mush-Z's channel triggers hide the line and reprint it reworded, in the colour of
    # its last style run. A line with no colour still has one run, like MUSHclient's.
    sink, engine, pack = _pack(
        rules="<triggers><trigger match='^You warn the group, (.+)$' regexp='y' "
        "enabled='y' omit_from_output='y' send_to='14'><send>"
        "ColourNote(RGBColourToName(TriggerStyleRuns[#TriggerStyleRuns]['textcolour']), "
        "RGBColourToName(TriggerStyleRuns[#TriggerStyleRuns]['backcolour']), "
        "'You warn, %1 ' .. RGBColourToName(TriggerStyleRuns[1].textcolour))"
        "</send></trigger></triggers>"
    )
    line = _line(pack, engine, "You warn the group, run")
    assert line.gagged is True
    assert sink.echoed == [("You warn, run silver", "main")]


def test_colour_names_and_numbers_match_mushclient():
    sink, _engine, _pack_ = _pack(
        "Send(RGBColourToName(255)); Send(RGBColourToName(0x563412))\n"
        "Send(tostring(ColourNameToRGB('red'))); Send(tostring(ColourNameToRGB('nonsense')))\n"
        "Send(tostring(ColourNameToRGB('#123456')))\n"
        "Send(tostring(GetNormalColour(2))); Send(tostring(GetBoldColour(8)))\n"
        "Send(RGBColourToName(TriggerStyleRunsThatDoNotExist))\n"
    )
    assert sink.sent == ["red", "#123456", "255", "-1", "5649426", "128", "16777215", ""]


def test_lowercase_wildcard_makes_wildcards_lower_case():
    sink, engine, pack = _pack(
        rules="<triggers><trigger match='^(\\w+) arrives$' regexp='y' enabled='y' "
        "lowercase_wildcard='y'><send>greet %1</send></trigger></triggers>"
    )
    _line(pack, engine, "BOB arrives")
    assert sink.sent == ["greet bob"]


def test_a_files_variables_are_its_starting_values():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    engine.set_var("volume", "30")  # what the player's last session saved
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(
        "<muclient><plugin name='p' id='aaaaaaaaaaaaaaaaaaaaaaaa'/>"
        "<variables><variable name='music'>on</variable>"
        "<variable name='volume'>100</variable></variables>"
        "<script><![CDATA[Send(GetVariable('music')); Send(GetVariable('volume'))]]></script>"
        "</muclient>"
    )
    assert sink.sent == ["on", "30"]


# --- found in review ---


def test_a_superscript_digit_after_a_percent_sign_is_just_text():
    # A latin-1 pack's "%²" used to raise inside the trigger, and the line was never shown.
    sink, engine, pack = _pack(
        rules="<triggers><trigger match='^go (.+)$' regexp='y' enabled='y'>"
        "<send>say %1 is 5%² and %&lt;²&gt; done</send></trigger></triggers>"
    )
    line = _line(pack, engine, "go north")
    assert sink.sent == ["say north is 5%² and  done"]
    assert line.plain_text == "go north"


def test_a_fault_inside_a_rule_is_reported_and_the_line_still_arrives(monkeypatch):
    from genericmud.scripting import mushclient_compat

    def broken(*_args, **_kwargs):
        raise ValueError("a bug in genericMud")

    sink, engine, pack = _pack(
        rules="<triggers><trigger match='ping' enabled='y'><send>pong</send></trigger>"
        "</triggers>"
    )
    monkeypatch.setattr(mushclient_compat, "fill_text", broken)
    line = _line(pack, engine, "ping")
    assert line.plain_text == "ping"
    assert any("script error" in text for text, _channel, _interrupt in sink.spoken)


def test_import_xml_takes_a_fragment_that_starts_with_a_doctype():
    sink, engine, pack = _pack(
        "Send(tostring(ImportXML([[<!DOCTYPE muclient>\n"
        "<triggers><trigger match='abc' enabled='y' send_to='12'><send>Send('hit')</send>"
        "</trigger></triggers>]])))"
    )
    _line(pack, engine, "abc")
    assert sink.sent == ["1", "hit"]


def test_markup_inside_a_comment_is_left_alone_by_the_attribute_repair():
    from genericmud.scripting.mushclient_compat import _sanitize_attr_markup

    xml = "<a><!-- <b c='d --><t m='x'/><u n='(?P<w>.)'/></a>"
    assert _sanitize_attr_markup(xml) == "<a><!-- <b c='d --><t m='x'/><u n='(?P&lt;w>.)'/></a>"
