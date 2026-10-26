"""The VIPMud language as its manual documents it: substitution, expressions and functions.

Most cases are the manual's own examples (VipMud Documentation V2.0, sections 2.4.4-2.4.7,
5.1.3, 5.2 and 5.3), so a failure here means a real pack script reads differently in
genericMud than in VIPMud.
"""

from __future__ import annotations

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.vipmud_dialect import VipMudPack
from genericmud.sound.bus import SoundBus
from tests.helpers import RecordingSink


def _load(source: str, base_dir: str | None = None):
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = VipMudPack(ScriptApi(engine, source="vip", base_dir=base_dir))
    pack.load_source(source)
    return sink, engine, pack


def _said(sink: RecordingSink) -> list[str]:
    return [text for text, _channel, _interrupt in sink.spoken]


# --- variables (manual 2.4.4 and 2.4.7) ---


def test_braced_variable_names_can_run_into_text():
    sink, _engine, _pack = _load("#var name wart\n#say {@{name}hog}")
    assert _said(sink) == ["warthog"]


def test_indirect_variables_resolve_through_the_named_variable():
    sink, engine, _pack = _load(
        "#var Name1 Name2\n"
        "#var @name1 hello\n"
        "#say @name1\n"
        "#say @Name2\n"
        "#say %var(@name1)\n"
        "#say @{@name1}\n"
    )
    assert engine.get_var("name2") == "hello"
    assert _said(sink) == ["Name2", "hello", "hello", "hello"]


def test_a_variable_name_starts_with_a_letter_and_stops_at_the_first_other_character():
    sink, _engine, _pack = _load("#var hp 40\n#say {@hp, then @hp.}\n#say {mail me@1 now}")
    assert _said(sink) == ["40, then 40.", "mail me@1 now"]


def test_var_without_a_value_defines_the_variable_empty():
    sink, _engine, _pack = _load(
        "#var abc\n"
        '#say {%case(%defined(abc)+1,"Not defined","defined")}\n'
        '#say {%case(%defined(xyz)+1,"Not defined","defined")}\n'
    )
    assert _said(sink) == ["defined", "Not defined"]


# --- the escape character (manual 5.1.2) ---


def test_tilde_makes_the_next_character_ordinary():
    sink, _engine, _pack = _load(
        "#var eight 8\n"
        "#say {see ya ~@eight}\n"
        "#say {100~% sure}\n"
        "#say {a ~~ b}\n"
    )
    assert _said(sink) == ["see ya @eight", "100% sure", "a ~ b"]


def test_prometheus_keeps_an_at_sign_in_its_sounds_path_through_exec(tmp_path):
    # The official Prometheus pack escapes every @ in its sounds path so the path survives
    # being expanded a second time by #Exec, which is how every one of its sounds plays.
    sound = tmp_path / "Sounds@Home" / "General" / "beep.wav"
    sound.parent.mkdir(parents=True)
    sound.write_bytes(b"RIFF")
    sink, engine, _pack = _load(
        "#var SoundsPath {Sounds~@Home}\n"
        "#Var StabilizedSoundsPath {%Replace(@SoundsPath,~@,~~~@)}\n"
        "#Var SoundTmp4 {@StabilizedSoundsPath\\}\n"
        "#Var SoundTmp2 {#Play^General\\beep.wav}\n"
        "#Exec {%Word(@SoundTmp2,^,1) {@SoundTmp4%Word(@SoundTmp2,^,2)} 80}\n",
        base_dir=str(tmp_path),
    )
    assert engine.get_var("stabilizedsoundspath") == "Sounds~@Home"
    assert sink.played, "nothing played"
    assert sink.played[-1]["file"].replace("\\", "/").endswith("Sounds@Home/General/beep.wav")
    assert abs(sink.played[-1]["gain"] - 0.8) < 1e-9


def test_exec_runs_the_expanded_text_as_commands():
    # Prometheus bakes an alias's parameters into an alarm this way; the alarm body
    # itself can't see %0 when it fires later.
    sink, _engine, _pack = _load("#alias spc {#Exec {#Alarm 0 {#say {copied %0}}}}")
    _engine.process_input("spc the hull report")
    sink.run_pending()
    assert _said(sink) == ["copied the hull report"]


def test_a_line_starting_with_an_angle_bracket_goes_to_the_mud_untouched():
    sink, _engine, _pack = _load(
        "#var version 4.2\n"
        "#key f1 {>say Meet me @ Village square; bring your #1 sword}\n"
        "#exec {>#$#register_soundpack Official VIP Mud Soundpack | @version}\n"
    )
    assert sink.sent == ["#$#register_soundpack Official VIP Mud Soundpack | 4.2"]
    _engine.press_key("f1")
    assert sink.sent[-1] == "say Meet me @ Village square; bring your #1 sword"


# --- expressions (manual 5.1.3) ---


def test_arithmetic_follows_the_manuals_precedence_and_stays_whole():
    _sink, engine, _pack = _load(
        "#var radius 7\n"
        "#math a {2 + 3 * 4}\n"
        "#math b {(2 + 3) * 4}\n"
        "#math c {7 / 2}\n"
        "#math d {7 \\ 2}\n"
        "#math e {2 ^ 10}\n"
        "#math f {-5 + 2}\n"
        "#math AreaOfCircle {22 * @radius ^ 2 /7}\n"
    )
    values = {name: engine.get_var(name) for name in "abcdef"}
    assert values == {"a": "14", "b": "20", "c": "3", "d": "1", "e": "1024", "f": "-3"}
    assert engine.get_var("areaofcircle") == "154"


def test_logical_operators_work_on_values_not_bits():
    sink, _engine, _pack = _load(
        "#say {%eval( 1 or 2)}\n"
        "#say {%eval(1 xor 1)}\n"
        "#say {%eval(1 xor 0)}\n"
        "#say {%eval(3 and 0)}\n"
    )
    assert _said(sink) == ["1", "0", "1", "0"]


def test_not_toggles_a_setting_like_the_manuals_speech_mute_key():
    _sink, engine, _pack = _load(
        "#key F10 {#if {not %defined(SpeechMute)} {#gVar SpeechMute 0};"
        " #GVar SpeechMute {%eval(not @SpeechMute)}}"
    )
    engine.press_key("f10")
    assert engine.get_var("speechmute") == "1"
    engine.press_key("f10")
    assert engine.get_var("speechmute") == "0"


def test_comparisons_take_bare_text_from_wildcards():
    sink, engine, _pack = _load(
        '#trigger {* hugs you.} {#if {%1 = "Brutus"} {#say bonk} {#say {hug %1}}}'
    )
    engine.process_line(Line("Brutus hugs you."))
    engine.process_line(Line("Fred hugs you."))
    assert _said(sink) == ["bonk", "hug Fred"]


def test_comparisons_are_numeric_when_both_sides_are_numbers_and_ignore_case_otherwise():
    sink, _engine, _pack = _load(
        "#var x 05\n#var name Fred\n#var hp 150\n"
        "#if {@x = 5} {#say numeric}\n"
        "#if {@name = fred} {#say caseless}\n"
        "#if {@hp > 20} {#say bigger}\n"
        "#if {@name <> fred} {#say wrong}\n"
    )
    assert _said(sink) == ["numeric", "caseless", "bigger"]


def test_an_empty_value_compares_as_empty_text():
    sink, _engine, _pack = _load(
        "#var e {}\n"
        '#if {@e = ""} {#say empty}\n'
        '#if {@e <> ""} {#say full}\n'
        '#if {@missing = ""} {#say unset}\n'
    )
    assert _said(sink) == ["empty", "unset"]


def test_wildcard_compare_matches_like_a_trigger_pattern():
    sink, engine, _pack = _load(
        '#trigger {*} {#if {%1 =* "Amy enters from the *"} {#say {hi Amy}}}'
    )
    engine.process_line(Line("Amy enters from the north"))
    engine.process_line(Line("Bob enters from the north"))
    assert _said(sink) == ["hi Amy"]


def test_a_function_result_is_one_value_even_with_operator_characters_in_it():
    # The official Prometheus sound engine tells loops apart with %Copy(%2,1,2)="l^";
    # pasting "l^" into the expression as text would read ^ as raising to a power.
    sink, engine, _pack = _load(
        '#alias pp {#If {%Copy(%1,1,2)="l^"} {#say looping} {#say once}}'
    )
    engine.process_input("pp l^Ship\\Engine@Ext")
    engine.process_input("pp General\\Beep@Ext")
    assert _said(sink) == ["looping", "once"]


def test_a_variable_goes_into_a_condition_as_text_quotes_and_all():
    # Cosmic Rage stores its settings with the quotes (#var speech "disabled", and the same
    # in its settings file) and checks them with @intromusic = "enabled"; that only holds if
    # the variable's text, quotes included, becomes part of the condition.
    sink, engine, _pack = _load(
        '#var intromusic "Enabled"\n'
        "#trigger {Welcome to: cosmic rage!}"
        ' {#if {@intromusic = "enabled"} {#say music}} {} AnyCase'
    )
    engine.process_line(Line("Welcome to: cosmic rage!"))
    assert engine.get_var("intromusic") == '"Enabled"'
    assert _said(sink) == ["music"]


def test_a_quoted_wildcard_compares_as_the_text_inside_the_quotes():
    sink, engine, _pack = _load('#trigger {* says, *} {#if {%2 = "Fred"} {#say match}}')
    engine.process_line(Line('Bob says, "Fred"'))
    assert _said(sink) == ["match"]


def test_text_with_a_hyphen_stays_text():
    sink, engine, _pack = _load('#trigger {wear *} {#if {%1 = gold-plated} {#say shiny}}')
    engine.process_line(Line("wear gold-plated"))
    assert _said(sink) == ["shiny"]


def test_a_real_footstep_condition_from_the_official_prometheus_pack():
    sink, engine, _pack = _load(
        "#var FootstepTmp1 {Metal}\n#var PlayOtherTreadSound 1\n#var PlayTreadSound 0\n"
        "#alias pstep {#If {%Length(@FootstepTmp1)>0 and ((%1=\"Other\" and"
        " @PlayOtherTreadSound=1) or (%1=\"You\" and @PlayTreadSound=1))} {#say step}}"
    )
    engine.process_input("pstep Other")
    engine.process_input("pstep You")
    assert _said(sink) == ["step"]


# --- functions (manual 5.3) ---


def test_functions_nest_and_do_their_own_arithmetic():
    sink, _engine, _pack = _load(
        "#var oldString  {This is the text we want, this is the text we do not want}\n"
        '#say  {%copy(@oldstring,1,%pos(",",@oldstring)-1)}\n'
    )
    assert _said(sink) == ["This is the text we want"]


def test_case_picks_a_value_by_number():
    _sink, engine, _pack = _load("#var InVal 3\n#var answer %case(@InVal,5,7,8,12)")
    assert engine.get_var("answer") == "8"


def test_chr_delete_and_proper():
    sink, _engine, _pack = _load(
        "#say {%chr(72)%chr(105)}\n"
        "#var TempVar {I am not going}\n"
        "#say %delete(@TempVar,6,4)\n"
        "#say {%proper(aMY.)}\n"
    )
    assert _said(sink) == ["Hi", "I am going", "Amy."]


def test_list_items_are_deleted_by_number_or_by_contents():
    sink, _engine, _pack = _load(
        '#say {%delNItem("Gloves|armour|boots",2)}\n'
        '#say {%delItem("gloves|armour|boots","boots")}\n'
    )
    assert _said(sink) == ["Gloves|boots", "gloves|armour"]


def test_eval_and_if_return_values_inside_text():
    sink, _engine, _pack = _load(
        "#var Experience 150\n#var oldExperience 100\n#var answer 1\n#var wrong 0\n"
        "#say {You have gained %eval(@Experience - @oldExperience) points}\n"
        '#say {%if(@answer,"Yes the answer is correct","Too bad")}\n'
        '#say {%if(@wrong,"Yes the answer is correct","Too bad")}\n'
    )
    assert _said(sink) == ["You have gained 50 points", "Yes the answer is correct", "Too bad"]


def test_ifword_and_isnumber():
    sink, _engine, _pack = _load(
        "#var cmd kill\n"
        '#say {%IfWord(@cmd,enter|k|kill|flee,"|")}\n'
        '#say {%IfWord(flee~s,enter|k|kill|flee,"|")}\n'
        "#say {%IsNumber(42) %IsNumber(abc) %isnumber(-3)}\n"
    )
    assert _said(sink) == ["1", "0", "1 0 1"]


def test_string_slicing_length_and_trims():
    sink, _engine, _pack = _load(
        "#var TestVar {123456789}\n"
        "#say {%left(@testvar,5) %right(@testvar,2) %length(@testvar)}\n"
        '#var s1 {"   The time has come.  "}\n'
        "#var s1 {%trim(@s1)}\n"
        "#say {[@s1]}\n"
        "#say {[%ltrim(  x )][%rtrim(  y  )]}\n"
    )
    assert _said(sink) == ["12345 89 9", "[The time has come.]", "[x ][  y]"]


def test_min_and_max_take_any_number_of_values():
    sink, _engine, _pack = _load(
        "#var potionCount 3\n#var experience 500\n#var mana 80\n"
        "#say {Buy %max(5,10-@potionCount) potions}\n"
        "#say {%min(@experience,@mana, 100)}\n"
    )
    assert _said(sink) == ["Buy 7 potions", "80"]


def test_word_and_numwords():
    sink, _engine, _pack = _load(
        "#var sentence {the quick brown fox}\n"
        "#say {%word(@sentence, ,%numWords(@sentence)-1)}\n"
        '#say {%word("a|b|c","|",2) %word("a|b|c","|",9) %numwords("a|b|c","|")}\n'
    )
    assert _said(sink) == ["brown", "b c 3"]


def test_push_and_pop_keep_a_stack_in_a_list_variable():
    sink, engine, _pack = _load(
        "#var travels {}\n"
        "#alias JustSeen {@travels = %push(@travels,%0)}\n"
        "#alias LookFor {#say %pop(travels)}\n"
    )
    for place in ("gold palace", "iron bridge", "Wishing Well"):
        engine.process_input(f"justSeen {place}")
    assert engine.get_var("travels") == "Wishing Well|iron bridge|gold palace"
    for _ in range(3):
        engine.process_input("LookFor")
    assert _said(sink) == ["Wishing Well", "iron bridge", "gold palace"]
    assert engine.get_var("travels") == ""


def test_random_stays_inside_its_bounds():
    _sink, engine, _pack = _load("#key f1 {#var roll %random(1,6)}")
    rolls = set()
    for _ in range(60):
        engine.press_key("f1")
        rolls.add(int(engine.get_var("roll")))
    assert rolls <= set(range(1, 7))
    assert len(rolls) > 1


def test_replace_reverse_and_sort():
    sink, _engine, _pack = _load(
        "#var s1 {Thexbrownxdogxranxquickly.}\n"
        "#var findString x\n"
        '#var replaceString {" "}\n'
        "#say {%replace(@s1,@findString,@replaceString)}\n"
        "#var s {A|b|c|d}\n#say {%reverseList(@s)}\n"
        "#var s {b|c|a|e|d}\n#say {%sort(@s,0)}\n#say {%sort(@s,1)}\n"
    )
    assert _said(sink) == [
        "The brown dog ran quickly.", "d|c|b|A", "a|b|c|d|e", "e|d|c|b|a",
    ]


def test_switch_direction_uses_the_direction_table():
    sink, _engine, _pack = _load("#say {%SwitchDirection(W) %switchdirection(north)}")
    assert _said(sink) == ["E south"]


def test_system_variables():
    sink, engine, _pack = _load("#var line {a%crlf%b}\n#say {[%version]}")
    assert engine.get_var("line") == "a\r\n%b"  # %b isn't a system variable
    version = _said(sink)[0]
    assert version.startswith("[") and version[1:-1].isdigit()
    assert len(version) == 8  # %version is xyyzzz


def test_an_unknown_function_or_a_lone_percent_sign_is_left_as_written():
    sink, _engine, _pack = _load("#say {50%off(sale) and 100% sure}")
    assert _said(sink) == ["50%off(sale) and 100% sure"]


# --- rules (manual 2.4.2, 5.2.54, 6.2) ---

BLOOD = [
    "Your blood freezes when you here the scream from the skunk.",
    "Your blood freezes when you here the scream from the turkey.",
    "YOUR blood freezes when you here the scream from the giant squirrel.",
    "Yes, your blood freezes when you here the scream from the rabid rat.",
    "YOUR blood freezes when you here the scream, from the red fox.",
]


def test_a_trigger_matches_the_whole_line_like_the_manuals_example():
    # "Note that you must include a leading or trailing asterisk if there is any text
    # expected before or after the search string."
    sink, engine, _pack = _load(
        "#trigger {Your blood freezes when you here the scream from *} {#say {%1}}"
    )
    for line in [*BLOOD, "Then Your blood freezes when you here the scream from the cat."]:
        engine.process_line(Line(line))
    assert _said(sink) == ["the skunk.", "the turkey."]


def test_a_leading_asterisk_lets_text_come_before_the_pattern():
    sink, engine, _pack = _load(
        "#trigger {*your blood freezes when you here the scream from *} {#say {%2}}"
    )
    for line in BLOOD:
        engine.process_line(Line(line))
    assert _said(sink) == ["the rabid rat."]


def test_spaces_the_mud_leaves_at_the_end_of_a_line_dont_stop_a_match():
    sink, engine, _pack = _load("#trigger {You are hungry.} {#say food}")
    engine.process_line(Line("You are hungry.   "))
    assert _said(sink) == ["food"]


def test_anycase_makes_a_trigger_ignore_case():
    sink, engine, _pack = _load(
        "#trig {* shouts '*fred*'} {#say {shout from %1}} {} AnyCase\n"
        "#trig {Hello there} {#say exact}\n"
    )
    engine.process_line(Line("Bob shouts 'FRED, come here!'"))
    engine.process_line(Line("hello there"))
    assert _said(sink) == ["shout from Bob"]


def test_a_question_mark_matches_one_character_and_doesnt_take_a_wildcard_number():
    # Only the asterisks number the wildcards (manual 5.2.54).
    sink, engine, _pack = _load("#trigger {?ou hit *} {#say {%1}}")
    engine.process_line(Line("You hit the orc"))
    assert _said(sink) == ["the orc"]


def test_an_exclamation_mark_negates_a_character_class():
    sink, engine, _pack = _load("#trigger {[!x-z]ebra} {#say {%0}}")
    engine.process_line(Line("zebra"))
    engine.process_line(Line("Debra"))
    assert _said(sink) == ["Debra"]


def test_a_tilde_makes_a_wildcard_character_literal_in_a_pattern():
    sink, engine, _pack = _load("#trigger {Score~: ~*~*~* *} {#say {%1}}")
    engine.process_line(Line("Score: *** 42"))
    engine.process_line(Line("Score: abc 42"))
    assert _said(sink) == ["42"]


def test_named_triggers_with_the_same_pattern_both_fire():
    sink, engine, _pack = _load(
        "#trigger {<Alas1>Alas, you can't go that way.} {#say one}\n"
        "#trigger {<Alas2>Alas, you can't go that way.} {#say two}\n"
    )
    engine.process_line(Line("Alas, you can't go that way."))
    assert sorted(_said(sink)) == ["one", "two"]


def test_a_pattern_that_is_all_angle_brackets_is_text_not_a_name():
    # COGG triggers on its "<Sanity -*>" prompt; read as a trigger name, nothing is left
    # to match and the trigger could never fire.
    sink, engine, _pack = _load("#trigger {<Sanity -*>} {#say {sanity %1}}")
    engine.process_line(Line("<Sanity -5>"))
    assert _said(sink) == ["sanity 5"]


def test_defining_the_same_trigger_again_replaces_it():
    # A pack reloaded on every login would otherwise play each sound once per load.
    sink, engine, _pack = _load("#trigger {boom} {#say one}\n#trigger {boom} {#say two}")
    engine.process_line(Line("boom"))
    assert _said(sink) == ["two"]


def test_defining_the_same_alias_or_key_again_replaces_it():
    sink, engine, _pack = _load(
        "#alias {hi} {#say first}\n#alias {HI} {#say second}\n"
        "#key f2 {#say one}\n#key F2 {#say two}\n"
    )
    engine.process_input("hi")
    engine.press_key("f2")
    assert _said(sink) == ["second", "two"]


def test_rules_defined_inside_a_script_work():
    sink, engine, _pack = _load("#alias arm {#trigger {pong} {#say {got %0}}}")
    engine.process_input("arm")
    engine.process_line(Line("pong"))
    assert _said(sink) == ["got pong"]


def test_untrigger_unalias_and_unkey_remove_rules():
    sink, engine, _pack = _load(
        "#trigger {<DroppedSword>Your sword slips from your hands.} {#say grab}\n"
        "#trigger {A scream escapes your lips *} {#say scream}\n"
        "#alias gt {#say alias}\n#key Shift-F9 {#say key}\n"
        "#UnTrig DroppedSword\n"
        "#Untrigger {A scream escapes your lips *}\n"
        "#UnAlias Gt\n#UnKey Shift-F9\n"
    )
    engine.process_line(Line("Your sword slips from your hands."))
    engine.process_line(Line("A scream escapes your lips loudly"))
    assert engine.process_input("gt") == ["gt"]
    engine.press_key("shift+f9")
    assert _said(sink) == []


def test_a_comment_at_the_end_of_a_line_is_not_part_of_it():
    sink, engine, _pack = _load(
        "#alias ccorpse {take all from corpse;wear all}  //used to pickup all your eq\n"
        "#key f1 {look  // glance about\n}\n"
    )
    engine.process_input("ccorpse")
    engine.press_key("f1")
    assert sink.sent == ["take all from corpse", "wear all", "look"]


# --- classes (manual 2.4.8 and 5.2.9) and the special aliases (2.4.1) ---


def test_rules_in_a_class_only_work_while_it_is_enabled():
    sink, engine, _pack = _load(
        "#key numpad+ {disarm @monster} battle\n"
        "#alias k {#var monster %1; kill @monster; #class battle enable}\n"
        "#trigger {You bury the corpse.} {#class battle disable} battle\n"
    )
    assert not engine.has_key("numpad+")  # a disabled class's key falls through
    engine.process_input("k rabbit")
    assert engine.press_key("numpad+")
    engine.process_line(Line("You bury the corpse."))
    assert not engine.press_key("numpad+")
    assert sink.sent == ["kill rabbit", "disarm rabbit"]


def test_the_current_class_covers_the_rules_defined_after_it():
    sink, engine, _pack = _load(
        "#class battle\n#key F7 {kick monster}\n#key F8 {punch monster}\n#class 0\n"
        "#Key F10 {#class battle enable}\n#Key shift-F10 {#class battle disable}\n"
    )
    assert not engine.press_key("f7")
    engine.press_key("f10")
    engine.press_key("f7")
    engine.press_key("f8")
    engine.press_key("shift+f10")
    assert not engine.press_key("f8")
    assert sink.sent == ["kick monster", "punch monster"]


def test_a_key_goes_back_to_its_plain_definition_when_its_class_is_off():
    sink, engine, _pack = _load(
        "#key F7 {look}\n#key F7 {kick monster} battle\n"
        "#key F10 {#class battle enable}\n#key F11 {#class battle disable}\n"
    )
    for key in ("f7", "f10", "f7", "f11", "f7"):
        engine.press_key(key)
    assert sink.sent == ["look", "kick monster", "look"]


def test_an_alias_in_a_disabled_class_lets_the_command_through():
    # The unofficial Prometheus pack's fly mode turns "n" into a flight command only while
    # its fly class is on; with it off, "n" has to reach the MUD.
    sink, engine, _pack = _load("#alias {n} {fly north} {fly}\n#key f2 {#class fly enable}")
    assert engine.process_input("n") == ["n"]
    engine.press_key("f2")
    assert engine.process_input("n") == []
    assert sink.sent == ["fly north"]


def test_a_rule_in_several_classes_needs_all_of_them():
    sink, engine, _pack = _load(
        "#trigger {You walk *} {#say step} {footsteps|global}\n#alias on {#class %1 enable}\n"
    )
    engine.process_line(Line("You walk north."))
    engine.process_input("on footsteps")
    engine.process_line(Line("You walk north."))
    engine.process_input("on global")
    engine.process_line(Line("You walk north."))
    assert _said(sink) == ["step"]


def test_unclass_makes_a_classes_rules_work_all_the_time():
    sink, engine, _pack = _load("#trigger {ping} {#say pong} battle\n#unclass battle")
    engine.process_line(Line("ping"))
    assert _said(sink) == ["pong"]


def test_delclass_removes_a_classes_rules():
    sink, engine, _pack = _load(
        "#trigger {ping} {#say pong} battle\n#class battle enable\n#delclass battle"
    )
    engine.process_line(Line("ping"))
    assert _said(sink) == []


def test_untrigger_with_a_class_only_removes_the_trigger_in_that_class():
    sink, engine, _pack = _load(
        "#trigger {ping} {#say pong} battle\n#untrigger {ping} other\n#class battle enable"
    )
    engine.process_line(Line("ping"))
    assert _said(sink) == ["pong"]


def test_onconnect_and_ondisconnect_run_when_the_connection_changes():
    # The unofficial Prometheus pack loads its settings and switches its classes on in
    # OnConnect, and saves them in OnDisconnect.
    sink, engine, pack = _load(
        "#alias OnConnect {#class global enable;#say hello}\n"
        "#alias {onDisconnect} {#say bye}\n"
        "#trigger {ping} {#say pong} global\n"
    )
    engine.process_line(Line("ping"))
    pack.connection_changed(True)
    engine.process_line(Line("ping"))
    pack.connection_changed(False)
    assert _said(sink) == ["hello", "pong", "bye"]


# --- sound (manual 5.2.37) ---


class _SpyBus(SoundBus):
    def __init__(self) -> None:
        super().__init__()
        self.adjusted: list[tuple[str, float | None, float | None]] = []
        self.busy: set[str] = set()

    def adjust(self, channel, gain=None, pan=None) -> None:
        self.adjusted.append((channel, gain, pan))

    def is_playing(self, channel) -> bool:
        return channel in self.busy


def test_pan_runs_from_minus_5000_to_5000_the_way_packs_write_it(tmp_path):
    # Star Conquest pans to +-3500, Prometheus to +-1500 and +-4000, and Cosmic Rage
    # multiplies the server's pan by 50. Read as -100..100 they all went hard left or right.
    (tmp_path / "hit.wav").write_bytes(b"RIFF")
    sink = RecordingSink()
    bus = _SpyBus()
    engine = AutomationEngine(sink, sound=bus)
    VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path))).load_source(
        "#alias pan {#play {hit.wav}; #pc %playhandle pan %0}"
    )
    for value in ("-3500", "1500", "5000", "-9000", "0"):
        engine.process_input(f"pan {value}")
    assert [pan for _channel, _gain, pan in bus.adjusted] == [-0.7, 0.3, 1.0, -1.0, 0.0]


def test_isplaying_tells_the_pack_whether_its_sound_is_still_going(tmp_path):
    (tmp_path / "loop.wav").write_bytes(b"RIFF")
    sink = RecordingSink()
    bus = _SpyBus()
    engine = AutomationEngine(sink, sound=bus)
    VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path))).load_source(
        "#playloop {loop.wav}\n#var h %playhandle\n"
        "#alias check {#pc @h isplaying playing}"
    )
    engine.process_input("check")
    assert engine.get_var("playing") == "0"
    bus.busy.add("vip-1")
    engine.process_input("check")
    assert engine.get_var("playing") == "1"


# --- control flow (manual 5.2.1, 5.2.3, 5.2.55, 5.2.64, 5.2.68, 5.2.69, 5.2.72) ---

POTIONS = (
    "#var hitPoints 10\n"
    "#alias drink {#10 {\n"
    "    #Say {Try %i};\n"
    "    #if {@hitPoints > 30} {\n"
    "        #abort%LEVEL%} {\n"
    "        get potion from bag;quaff potion;#math hitPoints {@hitPoints + 5}};\n"
    "    #say {Success %i}\n"
    "};#say {done}}\n"
)


def test_abort_without_a_value_ends_only_the_current_level():
    # The manual's potion example: the #If runs all ten times, "Success" only follows a
    # quaff, and "done" is said whatever happens.
    sink, engine, _pack = _load(POTIONS.replace("%LEVEL%", ""))
    engine.process_input("drink")
    said = _said(sink)
    assert said[:2] == ["Try 1", "Success 1"]
    assert said.count("Success 5") == 1 and "Success 6" not in said
    assert said[-2:] == ["Try 10", "done"]
    assert sink.sent.count("quaff potion") == 5


def test_abort_with_a_value_ends_the_whole_script():
    sink, engine, _pack = _load(POTIONS.replace("%LEVEL%", " 1"))
    engine.process_input("drink")
    said = _said(sink)
    assert said[-1] == "Try 6"
    assert "done" not in said


def test_repeat_counts_with_repeatnum_and_i():
    sink, engine, _pack = _load(
        "#key f1 {#5 {#math countdown {6-%repeatnum};#say @countdown};#say BlastOff}\n"
        "#key f2 {#26 {#var abc {@abc%chr(%i+64)}}}\n"
    )
    engine.press_key("f1")
    engine.press_key("f2")
    assert _said(sink) == ["5", "4", "3", "2", "1", "BlastOff"]
    assert engine.get_var("abc") == "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def test_while_and_until_count_down():
    sink, engine, _pack = _load(
        "#VAR countdown 4\n"
        "#While {@countdown > 1} {#say @countdown; #math countdown {@countdown-1}};#say lift\n"
        "#VAR countdown 3\n"
        "#until {@countdown < 1} {#say @countdown;#math countdown {@countdown-1}};#say blastoff\n"
    )
    assert _said(sink) == ["4", "3", "2", "lift", "3", "2", "1", "blastoff"]


def test_a_loop_that_never_ends_stops_instead_of_freezing_the_client():
    _sink, engine, _pack = _load("#var n 0\n#while {1} {#math n {@n + 1}}\n#var after yes")
    assert 0 < int(engine.get_var("n")) <= 100_000
    assert engine.get_var("after") == "yes"


def test_wait_holds_the_rest_of_the_commands():
    sink, engine, _pack = _load(
        "#alias RDi {#say {Rolling};#wait 0.3;#say {You rolled}}"
    )
    engine.process_input("RDi")
    assert _said(sink) == ["Rolling"]
    assert [delay for delay, _callback in sink.scheduled] == [0.3]
    sink.run_pending()
    assert _said(sink) == ["Rolling", "You rolled"]


def test_a_named_repeating_alarm_runs_until_unalarm():
    sink, engine, _pack = _load(
        "#alias go {#alarm tick -5 {#say tick}}\n"
        "#alias halt {#unalarm @tick}\n"
    )
    engine.process_input("go")
    assert [delay for delay, _callback in sink.scheduled] == [5]
    sink.run_pending()
    sink.run_pending()
    engine.process_input("halt")
    sink.run_pending()
    assert _said(sink) == ["tick", "tick"]
    assert sink.scheduled == []


def test_unalarm_takes_the_variables_name_too_and_minus_one_stops_every_alarm():
    sink, engine, _pack = _load(
        "#alarm LagTest -1 {#say lag}\n#alarm Other 3 {#say other}\n"
        "#alias stoplag {#unalarm LagTest}\n#alias stopall {#unalarm -1}\n"
    )
    engine.process_input("stoplag")
    sink.run_pending()
    assert _said(sink) == ["other"]
    sink.scheduled.clear()
    engine.process_input("stopall")
    assert _said(sink) == ["other"]


def test_an_alarm_given_only_a_time_changes_how_often_it_runs():
    sink, engine, _pack = _load("#alarm check -120 {save}\n#alarm check -300\n")
    assert [delay for delay, _callback in sink.scheduled] == [120, 300]
    sink.run_pending()  # the old schedule is dropped; the new one sends once
    assert sink.sent == ["save"]


def test_alarm_times_take_a_decimal_comma():
    # Cosmic Rage and the unofficial Prometheus pack ask which decimal separator the
    # system uses and build their alarm times with it: "0,50" is half a second.
    sink, _engine, _pack = _load("#var decchar {,}\n#alarm 0@{decchar}50 {#say half}")
    assert [delay for delay, _callback in sink.scheduled] == [0.5]


def test_a_wildcard_number_can_come_from_a_variable():
    sink, engine, _pack = _load("#trigger {* and *} {#var which 2; #say {%{@which}}}")
    engine.process_line(Line("salt and pepper"))
    assert _said(sink) == ["pepper"]


# --- output (manual 5.2.22, 5.2.24, 5.2.25, 5.2.47, 5.2.49, 5.2.51, 5.2.58, 5.3.16) ---


def test_sub_replaces_the_line_and_other_triggers_still_see_the_original():
    sink, engine, _pack = _load(
        "#trigger {* tells you '*'} {#sub {%1: %2}}\n"
        "#trigger {* tells you *} {#say told}\n"
    )
    line = engine.process_line(Line("james tells you 'hi'"))
    assert line.substitute == "james: hi"
    assert _said(sink) == ["told"]


def test_say_shows_and_speaks_unless_told_otherwise():
    sink, _engine, _pack = _load(
        "#say {both}\n#say {heard} NoDisplay\n#say {seen} NoVoice\n"
        "#say {voice} VoiceOnly|current\n#say {}\n"
    )
    assert _said(sink) == ["both", "heard", "voice"]
    assert [text for text, _channel in sink.echoed] == ["both", "seen"]


def test_setspeech_switches_incoming_speech_and_stops_speech():
    sink, engine, _pack = _load(
        "#key f10 {#SetSpeech Incoming Off}\n#key f11 {#SetSpeech Incoming On}\n"
        "#key f12 {#setspeech all stop}\n"
        "#key f1 {#say {said}}\n#key f2 {#say {quiet} VoiceOnly}\n"
    )
    engine.press_key("f10")
    assert engine.line_speech is False
    engine.press_key("f1")  # #say speaks in the Incoming voice, which is off
    engine.press_key("f2")
    engine.press_key("f11")
    assert engine.line_speech is True
    engine.press_key("f12")
    assert _said(sink) == ["quiet"]
    assert [text for text, _channel in sink.echoed] == ["said"]
    assert sink.speech_stops == 1


def test_gagon_voice_quiets_lines_until_ungagon():
    # The manual's xp example: speech is gagged while the score scrolls past, and the last
    # line gags itself because #UnGagOn ungags from the start of its own line.
    _sink, engine, _pack = _load(
        "#alias {xp} {#gagon voice;score}\n"
        "#Trigger {You are a protected citizen.} {#ungagon;#gagline voice}\n"
    )
    engine.process_input("xp")
    assert engine.line_speech is False
    line = engine.process_line(Line("You are a protected citizen."))
    assert engine.line_speech is True
    assert line.gagged


def test_gagon_all_hides_lines_until_ungagon():
    _sink, engine, _pack = _load("#alias hush {#gagon all}\n#alias talk {#ungagon}\n")
    engine.process_input("hush")
    hidden = engine.process_line(Line("spam spam"))
    engine.process_input("talk")
    shown = engine.process_line(Line("hello"))
    assert hidden.gagged and not hidden.display_when_gagged
    assert not shown.gagged


def test_ungagline_takes_back_a_gagline():
    _sink, engine, _pack = _load("#trigger {spam} {#gagline all; #if {1} {#ungagline}}")
    assert not engine.process_line(Line("spam")).gagged


def test_lastline_reads_the_output_window_from_the_bottom():
    sink, engine, _pack = _load(
        "#key Control-1 {#say %lastline(1) NoDisplay}\n"
        "#key Control-2 {#say {%lastline(2)} NoDisplay}\n"
    )
    sink.lines.extend(["first", "second", "third"])
    engine.press_key("ctrl+1")
    engine.press_key("ctrl+2")
    assert _said(sink) == ["third", "second"]


def test_find_puts_the_matching_line_in_foundline():
    # Cosmic Rage's find keys: Control+F searches back from the bottom, F3 finds the next.
    sink, engine, _pack = _load(
        "#var temp {dragon}\n"
        "#key f1 {#find {@temp} FIRST|Reverse ; #say {%foundline} NoDisplay}\n"
        "#key f3 {#find {@temp} reverse|next ; #say {%foundline} NoDisplay}\n"
    )
    sink.lines.extend(["a red dragon", "a goblin", "the Dragon roars"])
    for key in ("f1", "f3", "f3"):
        engine.press_key(key)
    assert _said(sink) == ["the Dragon roars", "a red dragon"]


# --- lists, files and loading (manual 5.2.2, 5.2.15, 5.2.21, 5.2.30, 5.2.42, 5.2.48, 5.2.61,
# 5.2.71, 5.3.10) ---


def test_additem_appends_to_a_list_variable():
    _sink, engine, _pack = _load(
        "#Var SampleList {Ann|Bob|Charles}\n#AddItem SampleList David\n"
        "#AddItem Empty First\n#AddItem SampleList bob NoDuplicates\n"
    )
    assert engine.get_var("samplelist") == "Ann|Bob|Charles|David"
    assert engine.get_var("empty") == "First"


def test_star_conquest_saves_and_loads_its_buffer_list_in_a_text_file(tmp_path):
    sink, engine, _pack = _load(
        "#alias savebuffers {#delfile StarConquestBufferlist.txt;"
        " #file 5 StarConquestBufferlist.txt; #write 5 @CurrentBufferList; #close 5}\n"
        "#alias loadbuffers {#file 5 StarConquestBufferlist.txt;"
        " #read 5 CurrentBufferList; #close 5}\n",
        base_dir=str(tmp_path),
    )
    engine.set_var("currentbufferlist", "General|Tactical")
    engine.process_input("savebuffers")
    engine.process_input("savebuffers")  # #DelFile first, so it doesn't append twice
    assert (tmp_path / "StarConquestBufferlist.txt").read_bytes() == b"General|Tactical\r\n"
    engine.set_var("currentbufferlist", "")
    engine.process_input("loadbuffers")
    assert engine.get_var("currentbufferlist") == "General|Tactical"


def test_a_sequential_file_reads_line_by_line_until_end_of_file(tmp_path):
    # The manual's shout example, with #While and %EndOfFile.
    (tmp_path / "Shout.txt").write_bytes(b"Hello there\r\nGoodbye\r\n")
    sink, engine, _pack = _load(
        "#alias shouts {#File 3 Shout.txt; #while {%EndOfFile(3)=0} {"
        " #read 3 TempVariable; shout @TempVariable}; #read 3 After}",
        base_dir=str(tmp_path),
    )
    engine.process_input("shouts")
    assert sink.sent == ["shout Hello there", "shout Goodbye"]
    assert engine.get_var("after") == "[EOF]"


def test_write_noreturn_leaves_the_line_open(tmp_path):
    _sink, _engine, _pack = _load(
        "#file 1 {log.txt}\n#write 1 {north;} NoReturn\n#write 1 {east;} NoReturn\n#close 1",
        base_dir=str(tmp_path),
    )
    assert (tmp_path / "log.txt").read_bytes() == b"north;east;"


def test_a_saved_configuration_comes_back_when_the_file_is_loaded(tmp_path):
    # The official Prometheus pack saves its settings as a script of #Var commands and
    # loads it at startup; nothing was ever written before.
    _sink, engine, pack = _load(
        "#var varFile {Prometheus/variables.set}\n"
        "#alias save {#DelFile {@varFile}; #File 3 {@varFile};"
        " #Write 3 {~#Var vol {@vol}%CRLF~#Var silent {@silent}} NoReturn; #Close 3}",
        base_dir=str(tmp_path),
    )
    engine.set_var("vol", "75")
    engine.set_var("silent", "1")
    engine.process_input("save")
    engine.set_var("vol", "")
    pack.load_source("#load {@varFile}")
    assert engine.get_var("vol") == "75"
    assert engine.get_var("silent") == "1"


def test_delfile_only_deletes_that_file_in_the_pack(tmp_path):
    (tmp_path / "keep").mkdir()
    kept = tmp_path / "keep" / "notes.txt"
    kept.write_text("mine", encoding="latin-1")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("not the pack's", encoding="latin-1")
    _sink, _engine, _pack = _load(
        "#DelFile notes.txt\n#DelFile ../outside.txt\n", base_dir=str(tmp_path)
    )
    assert kept.exists()  # a same-named file elsewhere in the pack isn't "notes.txt"
    assert outside.exists()


def test_send_sends_a_file_with_a_prefix(tmp_path):
    (tmp_path / "message.txt").write_bytes(b"first line\r\nsecond line\r\n")
    sink, engine, _pack = _load(
        "#alias post {#send message.txt {shout } {%crlf}}", base_dir=str(tmp_path)
    )
    engine.process_input("post")
    assert sink.sent == ["shout first line", "shout second line"]


def test_loading_a_file_again_runs_it_again_without_doubling_its_rules(tmp_path):
    # Prometheus loads its whole pack on every "Logged in!"; VIPMud replaces what the
    # file defined last time instead of keeping two of everything.
    (tmp_path / "sub.set").write_text(
        "#math loads {@loads + 1}\n#trigger {boom} {#say bang}\n", encoding="latin-1"
    )
    sink, engine, pack = _load("#load {sub.set}\n#load {sub.set}", base_dir=str(tmp_path))
    engine.process_line(Line("boom"))
    assert engine.get_var("loads") == "2"
    assert _said(sink) == ["bang"]


def test_unload_removes_what_a_file_defined(tmp_path):
    # Cosmic Rage switches its gag files on and off with #Load and #UnLoad.
    (tmp_path / "gags.set").write_text("#trigger {spam} {#gagline all}\n", encoding="latin-1")
    _sink, engine, pack = _load(
        "#alias gagson {#load {gags.set}}\n#alias gagsoff {#unload {gags.set}}\n",
        base_dir=str(tmp_path),
    )
    engine.process_input("gagson")
    assert engine.process_line(Line("spam")).gagged
    engine.process_input("gagsoff")
    assert not engine.process_line(Line("spam")).gagged
    engine.process_input("gagson")
    assert engine.process_line(Line("spam")).gagged


def test_commands_can_be_abbreviated_like_in_vipmud(tmp_path):
    (tmp_path / "sub.set").write_text("#VA loaded yes", encoding="latin-1")
    sink, engine, _pack = _load(
        "#V a 1\n#LO {sub.set}\n#TR {ping} {#Exec {pong}}\n"
        "#For {x|y} {#VAR {item%i} %i}\n#Unvar a\n",
        base_dir=str(tmp_path),
    )
    engine.process_line(Line("ping"))
    assert engine.get_var("a", None) is None
    assert engine.get_var("loaded") == "yes"
    assert engine.get_var("itemx") == "x"
    assert sink.sent == ["pong"]


# --- packs set up the way VIP Mud's own installers set them up ---


def test_a_linked_pack_knows_where_its_scripts_sounds_and_themes_are(tmp_path):
    # Miriani's and the official Prometheus pack's setup scripts write ScriptPath,
    # SoundsPath and ThemesPath into a Conf.set. genericMud doesn't run batch files, so it
    # finds the folders those scripts would have linked, and loads the scripts' Main.set
    # even where an older install recorded the sounds folder's main.set as the entry.
    from tests.test_setup import _linked_vipmud_pack

    pack_dir = _linked_vipmud_pack(tmp_path)
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = VipMudPack(ScriptApi(engine, source="vip", base_dir=str(pack_dir)))
    pack.load_entry(pack_dir / "Miriani Soundpack Sounds Version 7" / "main.set")
    scripts = pack_dir / "Miriani Soundpack for VIP Mud" / "Scripts"
    sounds = pack_dir / "Miriani Soundpack Sounds Version 7"
    assert engine.get_var("scriptpath") == str(scripts)
    assert engine.get_var("soundspath") == str(sounds)
    assert engine.get_var("themespath") == str(sounds / "Themes")
    assert engine.get_var("linked") == str(scripts)  # the scripts' Main.set ran


def test_a_script_that_breaks_stops_there_and_the_rest_keeps_working():
    # The manual's own example runs whatever another player tells you through #Exec, so the
    # MUD decides what that script contains; nesting it 3000 deep mustn't take the line
    # processing down with it.
    nested = "%trim(" * 3000 + "x" + ")" * 3000
    sink, engine, _pack = _load(
        "#trigger {Amy tells you '*'} {#Exec %1}\n#trigger {ping} {#say pong}\n"
    )
    engine.process_line(Line(f"Amy tells you '#say {nested}'"))
    engine.process_line(Line("ping"))
    assert _said(sink)[-1] == "pong"


def test_gagline_with_a_count_gags_the_lines_after_it_too():
    # Miriani hides its starmap's atmosphere block with "#GagLine 4 All" on its first line.
    _sink, engine, _pack = _load(
        "#Trigger {Atmosphere of *} {#GagLine 3 All}\n#Trigger {Quiet *} {#GagLine 2 Voice}\n"
    )
    texts = ["Atmosphere of Kryton", "oxygen 21%", "nitrogen 78%", "Next room."]
    lines = [engine.process_line(Line(text)) for text in texts]
    assert [line.gagged for line in lines] == [True, True, True, False]
    quiet = [engine.process_line(Line(text)) for text in ("Quiet please", "whisper", "loud")]
    assert [(line.gagged, line.display_when_gagged) for line in quiet] == [
        (True, True), (True, True), (False, False),
    ]


def test_ungagline_also_stops_the_lines_still_to_be_gagged():
    _sink, engine, _pack = _load(
        "#Trigger {Starmap *} {#GagLine 5 All}\n#Trigger {End of map} {#UnGagLine}\n"
    )
    lines = [engine.process_line(Line(t)) for t in ("Starmap 1", "row", "End of map", "after")]
    assert [line.gagged for line in lines] == [True, True, False, False]


def test_reload_starts_the_pack_again_from_its_entry(tmp_path):
    # The packs' "reload the soundpack" commands (Prometheus's spreload, Star Conquest's
    # reload key, Miriani's SPReload) end in #Reload: VIPMud clears what the pack defined
    # and set, then runs its start-up scripts again.
    entry = tmp_path / "pack.set"
    entry.write_text(
        "#math loads {@loads + 1}\n#trigger {boom} {#say bang}\n"
        "#alias spreload {#var leftover 1;#class extra enable;#reload}\n"
        "#trigger {extra} {#say extra} extra\n",
        encoding="latin-1",
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path)))
    pack.load_entry(entry)
    engine.set_var("usersetting", "mine")  # not the pack's
    engine.process_input("spreload")
    engine.process_line(Line("boom"))
    engine.process_line(Line("extra"))
    assert _said(sink) == ["bang"]  # one trigger, and the class is off again
    assert engine.get_var("loads") == "1"
    assert engine.get_var("leftover", None) is None
    assert engine.get_var("usersetting") == "mine"
