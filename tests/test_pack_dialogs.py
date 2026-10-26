"""Soundpack menus and prompts: VIPMud's #Pick, %Pick and #Prompt, MUSHclient's utils dialogs.

The player's answers are scripted on the RecordingSink; a front end without dialogs (the web
renderer, headless) answers None, which every dialect has to read as Cancel.
"""

from __future__ import annotations

import time

from genericmud.automation.engine import AutomationEngine
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.mushclient_compat import MushclientPack
from genericmud.scripting.vipmud_dialect import VipMudPack
from tests.helpers import RecordingSink


def _vip(source: str, answers: list | None = None):
    sink = RecordingSink(answers=list(answers or []))
    engine = AutomationEngine(sink)
    VipMudPack(ScriptApi(engine, source="vip")).load_source(source)
    return sink, engine


def _said(sink: RecordingSink) -> list[str]:
    return [text for text, _channel, _interrupt in sink.spoken]


# --- VIPMud (manual 5.2.36, 5.2.39, 5.3.23) ---


def test_pick_shows_the_list_and_sends_the_chosen_value():
    # The manual's F2 example: a title, three monsters, the cursor starting on fox.
    sink, engine = _vip(
        "#key f2 {#pick {p:select monster|rabbit:kill rabbit|*fox:kill fox|badger:kill badger}}",
        answers=[2],
    )
    engine.press_key("f2")
    assert sink.asked == [{
        "kind": "choice", "title": "select monster", "prompt": "select monster",
        "items": ["rabbit", "fox", "badger"], "selected": 1,
    }]
    assert sink.sent == ["kill badger"]


def test_a_picked_value_runs_as_typed_in_the_script_that_asked():
    # Cosmic Rage's gag creator picks alias names and commands; "#abort 1" ends the script.
    script = (
        "#alias addnewgag {#say adding}\n"
        "#key f3 {#pick {go:addnewgag|stop:#abort 1};#say after}"
    )
    sink, engine = _vip(script, answers=[0, 1])
    engine.press_key("f3")
    engine.press_key("f3")
    assert _said(sink) == ["adding", "after"]


def test_cancelling_a_pick_sends_nothing_and_the_script_goes_on():
    sink, engine = _vip("#key f4 {#pick {rabbit:kill rabbit|fox:kill fox};#say after}")
    engine.press_key("f4")
    assert sink.sent == []
    assert _said(sink) == ["after"]


def test_pick_function_returns_the_chosen_value():
    # The manual's example: "witch" is shown and "Broomhilda" is what comes back.
    sink, engine = _vip(
        "#alias k {kill %pick(p:select monster|rabbit|*fox|witch:Broomhilda)}", answers=[2]
    )
    engine.process_input("k")
    assert sink.asked[0]["items"] == ["rabbit", "fox", "witch"]
    assert sink.asked[0]["selected"] == 1
    assert sink.sent == ["kill Broomhilda"]


def test_pick_function_is_empty_when_cancelled():
    sink, engine = _vip("#alias k {#var choice {[%pick(rabbit|fox)]}}")
    engine.process_input("k")
    assert engine.get_var("choice") == "[]"


def test_pick_list_is_expanded_when_shown_as_the_prometheus_toggles_need():
    # The unofficial Prometheus options menu: captions come from variables, and each toggle
    # words its message by the value the variable had when the menu opened.
    script = (
        "#var SilentMode 0\n"
        "#var silent {Silent mode}\n"
        "#alias opts {#pick {p:Soundpack Options|DecreaseVolume:\n"
        "#say lower\n"
        "|@silent:\n"
        "#Var SilentMode {%eval(NOT @SilentMode)};\n"
        "#if {@SilentMode = 1} {\n"
        "#Say {Sounds on}} {\n"
        "#say {Sounds off}}\n"
        "}}"
    )
    sink, engine = _vip(script, answers=[1])
    engine.process_input("opts")
    assert sink.asked[0]["title"] == "Soundpack Options"
    assert sink.asked[0]["items"] == ["DecreaseVolume", "Silent mode"]
    assert engine.get_var("silentmode") == "1"
    assert _said(sink) == ["Sounds off"]
    assert sink.sent == []


def test_an_escaped_pipe_stays_inside_its_item():
    # Miriani's destination list is built with Entry~~%Chr(124)NoReturn, which stores
    # "Entry~|NoReturn" inside one item's value.
    script = (
        "#var dest {P:Select Destination|Earth:#Say {cgc 1} Entry~~%Chr(124)NoReturn}\n"
        "#alias go {#Pick {@dest}}"
    )
    sink, engine = _vip(script, answers=[0])
    engine.process_input("go")
    assert sink.asked[0]["items"] == ["Earth"]
    assert _said(sink) == ["cgc 1"]
    assert sink.sent == []


def test_the_first_colon_splits_so_a_title_can_end_in_one():
    # Cosmic Rage: "#pick {P:Soundpack path Configuration:|...}".
    sink, engine = _vip(
        "#alias pc {#pick {P:Soundpack path Configuration:|\nApply:#say applied}}", answers=[0]
    )
    engine.process_input("pc")
    assert sink.asked[0]["title"] == "Soundpack path Configuration:"
    assert sink.asked[0]["items"] == ["Apply"]
    assert _said(sink) == ["applied"]


def test_pick_takes_a_list_from_a_variable_without_braces():
    # Miriani's History.set: "#Pick @HistorySelectionTmp1".
    sink, engine = _vip("#var choices {one:#say 1|two:#say 2}\n#alias h {#Pick @choices}", [1])
    engine.process_input("h")
    assert sink.asked[0]["items"] == ["one", "two"]
    assert sink.asked[0]["title"] == ""  # the window names it after the world
    assert _said(sink) == ["2"]


def test_pick_function_keeps_commas_in_its_list():
    sink, engine = _vip("#alias k {#say {%pick(p:Pick one|a, b:first|c:second)}}", answers=[0])
    engine.process_input("k")
    assert sink.asked[0]["items"] == ["a, b", "c"]
    assert _said(sink) == ["first"]


def test_picking_abort_all_stops_the_script_that_asked():
    # Cosmic Rage's dev.set: "#pick {P:Are you sure|yes:#DelFile %1.set|*No:#abort all}".
    sink, engine = _vip(
        "#alias del {#pick {P:Are you sure|yes:#say {deleting %1}|*No:#abort all};#say done}",
        answers=[1, 0],
    )
    engine.process_input("del notes")
    assert sink.asked[0]["selected"] == 1
    assert _said(sink) == []
    engine.process_input("del notes")
    assert _said(sink) == ["deleting notes", "done"]


def test_a_slow_answer_doesnt_use_up_the_scripts_time(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class SlowPlayer(RecordingSink):
        def ask(self, request):
            clock[0] += 60.0  # a minute arrowing through Cosmic Rage's paragraphs
            return super().ask(request)

    sink = SlowPlayer(answers=[0, "goblin"])
    engine = AutomationEngine(sink)
    VipMudPack(ScriptApi(engine, source="vip")).load_source(
        "#alias go {#pick {a:#say picked};#prompt monster {Name?};#say {after @monster}}"
    )
    engine.process_input("go")
    assert _said(sink) == ["picked", "after goblin"]


def test_gprompt_sets_a_global_variable():
    sink, engine = _vip("#alias gp {#gprompt target {Who?}}", answers=["orc"])
    engine.process_input("gp")
    assert engine.get_gvar("target") == "orc"


def test_prompt_sets_the_variable_and_cancel_leaves_it_alone():
    sink, engine = _vip(
        "#var monster {orc}\n#alias pm {#prompt monster {Monster name?}}", answers=["goblin", None]
    )
    engine.process_input("pm")
    assert sink.asked[0] == {
        "kind": "text", "title": "Monster name?", "prompt": "Monster name?", "default": "",
        "multiline": False,
    }
    assert engine.get_var("monster") == "goblin"
    engine.process_input("pm")
    assert engine.get_var("monster") == "goblin"


# --- MUSHclient utils (gammon.com.au/scripts/doc.php?lua=utils.*) ---


def _mush(script: str, answers: list | None = None):
    sink = RecordingSink(answers=list(answers or []))
    engine = AutomationEngine(sink)
    pack = MushclientPack(ScriptApi(engine, source="m"))
    pack.load_source(f"<muclient><script><![CDATA[\n{script}\n]]></script></muclient>")
    return sink, engine, pack


def test_msgbox_asks_with_its_buttons_and_returns_the_buttons_name():
    sink, _engine, pack = _mush(
        'SetVariable("r", utils.msgbox("Delete it?", "Confirm", "yesno", "?", 2))',
        answers=["no"],
    )
    assert sink.asked == [{
        "kind": "message", "title": "Confirm", "prompt": "Delete it?", "buttons": "yesno",
        "icon": "?", "default": 2,
    }]
    assert pack._api.get_var("r") == "no"


def test_msgbox_defaults_to_one_ok_button_titled_mushclient():
    sink, _engine, pack = _mush('SetVariable("r", utils.msgbox("Saved."))', answers=["ok"])
    assert sink.asked[0]["title"] == "MUSHclient"
    assert sink.asked[0]["buttons"] == "ok" and sink.asked[0]["default"] == 1
    assert pack._api.get_var("r") == "ok"


def test_msgbox_without_a_dialog_answers_like_the_player_backed_out():
    # MUSHclient's msgbox never returns nil, and packs call methods on the result.
    sink, _engine, pack = _mush(
        'SetVariable("ok", utils.msgbox("a"))\n'
        'SetVariable("okcancel", utils.msgbox("b", nil, "okcancel"))\n'
        'SetVariable("yesno", utils.msgbox("c", nil, "yesno"))\n'
        'SetVariable("yesnocancel", utils.msgbox("d", nil, "yesnocancel"))\n'
        'SetVariable("ari", utils.msgbox("e", nil, "abortretryignore"))\n'
    )
    names = ("ok", "okcancel", "yesno", "yesnocancel", "ari")
    values = {name: pack._api.get_var(name) for name in names}
    assert values == {
        "ok": "ok", "okcancel": "cancel", "yesno": "no", "yesnocancel": "cancel", "ari": "abort",
    }


def test_inputbox_returns_what_was_typed_or_nil():
    sink, _engine, pack = _mush(
        'SetVariable("a", utils.inputbox("Name?", "Who", "Bob") or "<nil>")\n'
        'SetVariable("b", utils.inputbox("Again?") or "<nil>")\n',
        answers=["Alice", None],
    )
    assert sink.asked[0] == {
        "kind": "text", "title": "Who", "prompt": "Name?", "default": "Bob", "multiline": False,
    }
    assert pack._api.get_var("a") == "Alice"
    assert pack._api.get_var("b") == "<nil>"


def test_editbox_asks_for_several_lines_and_ends_them_like_windows():
    # A Windows edit box's lines end in CR LF; capture2dworld collapses blank lines by
    # looking for "\r\n\r\n".
    sink, _engine, pack = _mush(
        'SetVariable("a", utils.editbox("Notes", "Edit", "x\\r\\ny"))', answers=["x\ny\r\nz"]
    )
    assert sink.asked[0]["multiline"] is True
    assert sink.asked[0]["default"] == "x\ny"
    assert pack._api.get_var("a") == "x\r\ny\r\nz"


def test_inputbox_keeps_asking_until_the_packs_validate_function_accepts():
    # The mapper's number prompts explain the problem in a msgbox and return false.
    sink, _engine, pack = _mush(
        'local function check(text)\n'
        '  if tonumber(text) then return true end\n'
        '  utils.msgbox("must be a number", "Incorrect input", "ok", "!", 1)\n'
        '  return false\n'
        'end\n'
        'SetVariable("a", utils.inputbox("Speed?", "Speed", "5", nil, nil, {validate = check}))\n',
        answers=["fast", "ok", "7"],
    )
    assert [request["kind"] for request in sink.asked] == ["text", "message", "text"]
    assert sink.asked[2]["default"] == "fast"
    assert pack._api.get_var("a") == "7"


def test_listbox_shows_values_sorted_and_returns_the_chosen_key():
    sink, _engine, pack = _mush(
        't = {c = "Cherry", apple = "Apple", b = "banana"}\n'
        'SetVariable("r", utils.listbox("Pick a fruit", "Fruit", t, "c") or "<nil>")\n',
        answers=[1],
    )
    assert sink.asked[0]["items"] == ["Apple", "banana", "Cherry"]
    assert sink.asked[0]["selected"] == 2
    assert pack._api.get_var("r") == "b"


def test_choose_works_like_listbox_and_cancel_is_nil():
    sink, _engine, pack = _mush(
        'SetVariable("r", utils.choose("Which?", "Help", {"readme", "changelog"}) or "<nil>")\n'
        'SetVariable("s", utils.choose("Which?", "Help", {"readme", "changelog"}) or "<nil>")\n',
        answers=[0, None],
    )
    assert sink.asked[0]["items"] == ["changelog", "readme"]
    assert sink.asked[0]["selected"] is None
    assert pack._api.get_var("r") == "2"  # the key of "changelog" in the table
    assert pack._api.get_var("s") == "<nil>"


def test_multilistbox_returns_the_chosen_keys():
    sink, _engine, pack = _mush(
        't = {fruit = "apple", vegetable = "potato", spice = "pepper"}\n'
        'r = utils.multilistbox("Pick", "Food", t, {fruit = true})\n'
        'keys = {}\n'
        'for key in pairs(r) do table.insert(keys, key) end\n'
        'table.sort(keys)\n'
        'SetVariable("r", table.concat(keys, ","))\n',
        answers=[[0, 2]],
    )
    assert sink.asked[0]["kind"] == "choices"
    assert sink.asked[0]["items"] == ["apple", "pepper", "potato"]
    assert sink.asked[0]["selected"] == [0]
    assert pack._api.get_var("r") == "fruit,vegetable"


def test_a_slow_answer_doesnt_use_up_the_lua_time_limit(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class SlowPlayer(RecordingSink):
        def ask(self, request):
            clock[0] += 60.0
            return super().ask(request)

    sink = SlowPlayer(answers=["yes"])
    pack = MushclientPack(ScriptApi(AutomationEngine(sink), source="m"))
    # The loop runs the Lua time-limit hook, which would stop a script that had used up
    # its second waiting for the player.
    pack.load_source(
        "<muclient><script><![CDATA[\n"
        'local answer = utils.msgbox("Sure?", "Question", "yesno")\n'
        "for i = 1, 200000 do end\n"
        'SetVariable("after", answer)\n'
        "]]></script></muclient>"
    )
    assert pack._api.get_var("after") == "yes"


# --- the plumbing ---


def test_an_answer_that_doesnt_fit_the_question_counts_as_cancel():
    sink = RecordingSink(answers=[7, "x", [9], 3])
    api = ScriptApi(AutomationEngine(sink))
    assert api.ask_choice("t", "p", ["a", "b"]) is None
    assert api.ask_choice("t", "p", ["a", "b"]) is None
    assert api.ask_choices("t", "p", ["a", "b"]) is None
    assert api.ask_text("t", "p") is None


def test_the_app_hands_a_packs_question_to_whoever_shows_dialogs():
    from genericmud.app import EngineApp
    from genericmud.voice.router import VoiceRouter
    from tests.helpers import RecordingBackend

    app = EngineApp(VoiceRouter(RecordingBackend(), clock=lambda: 0.0))
    api = ScriptApi(app.engine)
    assert api.ask_text("Name", "Who are you?") is None  # no window: cancelled
    asked = []
    app.asker = lambda request: asked.append(request) or "Bob"
    assert api.ask_text("Name", "Who are you?") == "Bob"
    assert asked[0]["kind"] == "text"


def test_a_key_bound_only_to_vipmuds_own_menus_stays_genericmuds():
    # Cosmic Rage's keys.set re-binds VIPMud's menu and system keys. genericMud ignores those
    # commands, so the keys must reach genericMud's own menus instead of doing nothing.
    from genericmud.scripting.vipmud_dialect import vip_key_to_combo

    _sink, engine = _vip(
        "#key Alt-F {#Automenu main|file}\n"
        "#key Control-O {#autoMenu main|connect|character}\n"
        "#key Alt-space {#Syskey INSERT_TAB_WORD}\n"
        "#key Escape {#Syskey clear_line}\n"
        "#key control-e {#window ErrorLog}\n"
        "#key control-q #quit\n"
        "#key f5 {#say hello}\n"
        "#key f6 {#automenu main;#say menu}\n"
    )
    for vip_key in ("Alt-F", "Control-O", "Alt-space", "Escape", "control-e", "control-q"):
        assert not engine.has_key(vip_key_to_combo(vip_key)), vip_key
    assert engine.has_key("f5")
    assert engine.has_key("f6")  # it does something of its own too
