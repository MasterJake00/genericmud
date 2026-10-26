"""Golden tests for the VIPMud .set interpreter."""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import warnings
from pathlib import Path

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from genericmud.scripting.api import ScriptApi
from genericmud.scripting.vipmud_dialect import (
    VipMudPack,
    _expand_sound_variant,
    _parse_vip_settings,
    _serialize_vip_settings,
    _Tok,
    tokenize_statements,
    vip_pattern_to_regex,
)
from genericmud.sound.bus import SoundBus
from tests.helpers import RecordingSink


def _load(source: str, base_dir: str | None = None) -> tuple[RecordingSink, AutomationEngine]:
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    VipMudPack(ScriptApi(engine, source="vipmud", base_dir=base_dir)).load_source(source)
    return sink, engine


# The server-controlled sound core of real packs (Cosmic Rage VIPMud Immersion).
SPHOOK = """#trigger {$sphook &{action}:&{soundpath}:&{volume}:&{pitch}:&{pan}:&{id}} {
#if {@action = "loop"} {#playloop {@sppath/@soundpath.wav} @volume; #var @id %playhandle};
#if {@action = "play"} {#play {@sppath/@soundpath.wav} @volume};
}"""


def test_key_say_with_var_substitution():
    sink, engine = _load("#VAR hp {42}\n#KEY f2 {#say {@hp hp}}")
    assert engine.get_var("hp") == "42"
    engine.press_key("f2")
    assert sink.spoken == [("42 hp", "main", False)]


def test_trigger_wildcard_and_say():
    sink, engine = _load("#TR {You see *} {#say {found %1}}")
    engine.process_line(Line("You see a dragon"))
    assert sink.spoken == [("found a dragon", "main", False)]


def test_trigger_play_sound_with_volume():
    sink, engine = _load("#TRIGGER {* hits you} {#play {hit.wav} 80}")
    engine.process_line(Line("A goblin hits you"))
    assert sink.played and sink.played[0]["file"] == "hit.wav"
    assert abs(sink.played[0]["gain"] - 0.8) < 1e-9


def test_alias_with_wildcard_sends():
    sink, engine = _load("#ALIAS {gt *} {tell group %1}")
    assert engine.process_input("gt hello team") == []
    assert sink.sent == ["tell group hello team"]


def test_bare_line_in_body_sends():
    sink, engine = _load("#KEY f3 {kill orc}")
    engine.press_key("f3")
    assert sink.sent == ["kill orc"]


def test_tokenizer_preserves_nested_braces():
    statements = tokenize_statements("#KEY f2 {#say {@hp hp, @mp mp}}")
    command, args = statements[0]
    assert command == "KEY"
    assert args[0].text == "f2"
    assert args[1].text == "#say {@hp hp, @mp mp}"


def test_sphook_play_action_named_wildcards_and_sppath_default():
    # @sppath defaults to the pack dir; named wildcards resolve as @action/@soundpath/@volume.
    sink, engine = _load(SPHOOK, base_dir="/snd")
    engine.process_line(Line("$sphook play:general/misc/on:80:0:0:1"))
    assert sink.played, "no sound played"
    cue = sink.played[-1]
    assert cue["file"] == "/snd/general/misc/on.wav"
    assert abs(cue["gain"] - 0.8) < 1e-9
    assert cue["loop"] is False


def test_installer_declared_remote_sound_is_fetched_once_and_played(tmp_path, monkeypatch):
    (tmp_path / "SoundSync.exe").write_bytes(b"windows helper")
    (tmp_path / "installer.bat").write_text(
        'set "REPO_URL=https://gitea.example/Cosmic/Sounds"\n'
        'set "REPO_SUBFOLDER=wav"\n',
        encoding="latin-1",
    )
    downloads = []

    def download(url, destination, **_kwargs):
        downloads.append(url)
        destination.write_bytes(b"RIFF-fake-wave")

    monkeypatch.setattr("genericmud.packs.vault.download", download)
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path)))
    pack.load_source(SPHOOK)

    line = Line("$sphook play:general/misc/on:80:0:0:1")
    engine.process_line(line)
    engine.process_line(Line(line.plain_text))

    expected = tmp_path / "sounds" / "wav" / "general" / "misc" / "on.wav"
    assert expected.read_bytes() == b"RIFF-fake-wave"
    assert downloads == [
        "https://gitea.example/Cosmic/Sounds/raw/branch/main/wav/general/misc/on.wav"
    ]
    assert len(sink.played) == 2
    assert all(Path(cue["file"]) == expected for cue in sink.played)


def test_sphook_loop_action_loops_and_stores_handle(tmp_path):
    # A real file: a failed play now (correctly) stores handle 0, which is its own
    # test -- this one is about the handle bookkeeping for a play that worked.
    (tmp_path / "music").mkdir()
    (tmp_path / "music" / "intro.wav").write_bytes(b"RIFF")
    sink, engine = _load(SPHOOK, base_dir=str(tmp_path))
    engine.set_var("sppath", str(tmp_path))
    engine.process_line(Line("$sphook loop:music/intro:100:0:0:42"))
    cue = sink.played[-1]
    assert cue["loop"] is True
    # "#var @id %playhandle": @id == "42", so the handle is stored under var "42".
    assert engine.get_var("42") == cue["channel"].split("-")[1]


def test_sphook_unknown_action_plays_nothing():
    sink, engine = _load(SPHOOK, base_dir="/snd")
    engine.process_line(Line("$sphook bogus:x:50:0:0:1"))
    assert sink.played == []


def test_playloop_then_pc_stop_by_handle(tmp_path):
    (tmp_path / "a.wav").write_bytes(b"RIFF")
    sink, engine = _load(
        "#trigger {go} {#playloop {a.wav} 50}\n#trigger {halt} {#pc %playhandle stop}",
        base_dir=str(tmp_path),
    )
    engine.process_line(Line("go"))
    channel = sink.played[-1]["channel"]
    engine.process_line(Line("halt"))
    assert channel in sink.stopped


def test_if_numeric_branch_and_assignment_not_sent():
    sink, engine = _load("#alias {clamp} {#var v {150}; #if {@v > 100} {@v = 100}}")
    assert engine.process_input("clamp") == []
    assert engine.get_var("v") == "100"  # "@v = 100" assigns, not sent
    assert sink.sent == []


def test_load_chains_in_another_set_file(tmp_path):
    # #load pulls in the pack's other scripts, so triggers spread across files register.
    (tmp_path / "main.set").write_text("#load {sub.set}", encoding="utf-8")
    (tmp_path / "sub.set").write_text("#trigger {boom} {#play {bang.wav}}", encoding="utf-8")
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path))).load_source(
        (tmp_path / "main.set").read_text(encoding="utf-8")
    )
    engine.process_line(Line("boom"))
    # base_dir is set, so the relative sound resolves under the pack dir.
    assert any(played["file"].endswith("bang.wav") for played in sink.played)


def test_load_resolves_by_filename_across_layouts(tmp_path):
    # A loader referencing @scpath/x.set still finds x.set when the layout differs.
    (tmp_path / "boot.set").write_text("#load {@scpath/deep.set}", encoding="utf-8")
    nested = tmp_path / "Scripts"
    nested.mkdir()
    (nested / "deep.set").write_text("#trigger {hi} {#say {found}}", encoding="utf-8")
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    # @scpath defaults to the pack dir; deep.set lives in a subdir -> matched by name.
    VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path))).load_source(
        (tmp_path / "boot.set").read_text(encoding="utf-8")
    )
    engine.process_line(Line("hi"))
    assert sink.spoken and sink.spoken[-1][0] == "found"


def test_world_sounds_dir_overrides_pack_default_sppath():
    # The session sets @sppath from world.sounds before packs load; the pack must not
    # clobber it, so sounds resolve against the world's folder, not the pack dir.
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    engine.set_var("sppath", "/my/sounds")
    VipMudPack(ScriptApi(engine, source="vip", base_dir="/pack")).load_source(SPHOOK)
    engine.process_line(Line("$sphook play:combat/hit:100:0:0:1"))
    assert sink.played[-1]["file"] == "/my/sounds/combat/hit.wav"


def test_forall_iterates_with_loop_variable():
    # #ForAll runs its body once per |-item, substituting the %I loop token.
    sink, engine = _load("#KEY f1 {#ForAll {one|two|three} {#say {got %I}}}")
    engine.press_key("f1")
    assert [s[0] for s in sink.spoken] == ["got one", "got two", "got three"]


def test_forall_loads_each_listed_script(tmp_path):
    # The real loader idiom: #ForAll {a|b} {#load {Scripts\%I.set}} pulls in every script.
    (tmp_path / "main.set").write_text(
        "#ForAll {combat|keys} {#load {Scripts\\%I.set}}", encoding="utf-8"
    )
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "combat.set").write_text("#trigger {hit} {#say {boom}}", encoding="utf-8")
    (scripts / "keys.set").write_text("#key f1 {look}", encoding="utf-8")
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path))).load_source(
        (tmp_path / "main.set").read_text(encoding="utf-8")
    )
    engine.process_line(Line("hit"))
    assert sink.spoken and sink.spoken[-1][0] == "boom"
    engine.press_key("f1")
    assert sink.sent == ["look"]


def test_gagline_voice_gags_speech_but_keeps_line_reviewable():
    sink, engine = _load("#TRIGGER {chatter *} {#gagline voice}")
    line = engine.process_line(Line("chatter from afar"))
    assert line.gagged is True
    assert line.display_when_gagged is True


def test_gagline_count_then_all_removes_line_entirely():
    # Prometheus writes "#gagline 1 all" (a count precedes the mode); no "voice" -> full gag.
    sink, engine = _load("#TRIGGER {spam} {#gagline 1 all}")
    line = engine.process_line(Line("spam"))
    assert line.gagged is True
    assert line.display_when_gagged is False


def test_alarm_defers_body_until_the_scheduler_fires():
    # Packs defer loading via "#alarm 0 {#load ...}" off a login trigger; nothing runs until
    # an event loop drives the scheduler (RecordingSink.run_pending stands in for it).
    sink, engine = _load("#TRIGGER {login} {#alarm 0 {#say {ready}}}")
    engine.process_line(Line("login"))
    assert sink.spoken == [] and sink.scheduled, "alarm body ran early or wasn't scheduled"
    sink.run_pending()
    assert sink.spoken == [("ready", "main", False)]


def test_abort_stops_the_rest_of_the_body():
    sink, engine = _load("#TRIGGER {go} {#say {first}; #abort; #say {second}}")
    engine.process_line(Line("go"))
    assert [s[0] for s in sink.spoken] == ["first"]


def test_sound_variant_expands_to_a_random_numbered_file():
    # VIPMud "name*N.wav" picks one of name1..nameN at random; plain names pass through.
    picks = {_expand_sound_variant("beep*3.wav") for _ in range(40)}
    assert picks <= {"beep1.wav", "beep2.wav", "beep3.wav"}
    assert len(picks) >= 2  # the choice varies (P(all 40 equal) is vanishing)
    plain = "Star Conquest\\Music\\plain.wav"  # no *N marker -> unchanged
    assert _expand_sound_variant(plain) == plain


def test_file_read_loads_settings_and_opens_the_sound_gate(tmp_path):
    # Star Conquest gates every #Play on @silent=1, a flag that lives in the pack's binary
    # Settings.set. Reading it (via #file/#Read) is what makes the pack audible.
    (tmp_path / "Settings.set").write_bytes(_serialize_vip_settings(["50", "1", "1"]))
    src = (
        "#if {NOT %Defined(silent)} {#var silent 0}\n"  # the pack's pre-read default: gated off
        "#file 6 {Star Conquest/Settings.set} 1\n"
        "#Read 6 vol 1\n#Read 6 socialson 2\n#Read 6 silent 3\n"
        "#TRIGGER {bonk} {#if {@silent = 1} {#play {CantGo.wav} @vol}}"
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path))).load_source(src)
    assert engine.get_var("silent") == "1"  # the read overrode the off-by-default
    assert engine.get_var("vol") == "50"
    engine.process_line(Line("bonk"))
    assert sink.played and sink.played[-1]["file"].endswith("CantGo.wav")


def test_file_write_close_persists_settings(tmp_path):
    settings = tmp_path / "Settings.set"
    settings.write_bytes(_serialize_vip_settings(["50", "0"]))
    engine = AutomationEngine(RecordingSink())
    pack = VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path)))
    pack.load_source("#file 6 {Settings.set}\n#Write 6 99 1\n#Write 6 1 2\n#Close 6")
    assert _parse_vip_settings(settings.read_bytes()) == ["99", "1"]  # flushed back in format


def test_gvar_sets_global_namespace_not_local():
    sink, engine = _load("#GVAR realm {aetherius}")
    assert engine.get_gvar("realm") == "aetherius"  # landed in the global map
    assert "realm" not in engine._vars  # not the local map (#GVAR is not #VAR)
    assert engine.get_var("realm") == "aetherius"  # @-reads still resolve it via fallback


# --- Cosmic Rage verification round (0.6.12): the parts of the real pack's $sphook
# dispatch that were broken -- stop-by-stored-handle, <>, comments, #math, #trig,
# alias expansion of bare body text, and failed-play handles.

CR_DISPATCH = """#trigger {$sphook &{action}:&{soundpath}:&{volume}:&{pitch}:&{pan}:&{id}} {
#if {@action = "loop"} {#playloop {@sppath/@soundpath.wav} @volume; #var @id %playhandle};
#if {@action = "play"} {#play {@sppath/@soundpath.wav} @volume};
#if {@pan <> "na"} {#math pan {@pan * 50}; #pc %playhandle pan @pan};
#if {@action = "stop"} {#if {%defined(@id) = 1} {#pc %var(@id) stop}}}"""


def _cr(tmp_path):
    (tmp_path / "amb.wav").write_bytes(b"RIFF")
    sink, engine = _load(CR_DISPATCH, base_dir=str(tmp_path))
    engine.set_var("sppath", str(tmp_path))
    return sink, engine


def test_cr_stop_by_stored_handle(tmp_path):
    # The server stores the loop's handle under a name it chose (%var/%defined were
    # unimplemented, so a server-issued stop was a no-op -> ambience stacking).
    sink, engine = _cr(tmp_path)
    engine.process_line(Line("$sphook loop:amb:50:na:na:h1"))
    assert sink.played[-1]["loop"] is True
    assert engine.get_var("h1") == "1"
    engine.process_line(Line("$sphook stop:na:na:na:na:h1"))
    assert sink.stopped == ["vip-1"]


def test_cr_not_equal_gates_and_math_pan(tmp_path):
    # <> (not-equal) wasn't parsed and #math wasn't implemented: every directional
    # cue collapsed to centre. pan "2" -> #math 2*50=100 -> #pc pan (live adjust).
    sink, engine = _cr(tmp_path)
    engine.process_line(Line("$sphook play:amb:80:na:2:na"))
    assert sink.played[-1]["gain"] == 0.8
    assert engine.get_var("pan") == "100"  # the #math result landed


def test_comment_lines_are_not_sent():
    # ";core variables"-style comment lines were tokenized as bare text and SENT to
    # the MUD at load. Mid-line ';' stays a statement separator.
    sink, _engine = _load(";comment at line start\n#var a 1;#var b 2\n  ;indented comment\n")
    assert sink.sent == []


def test_trig_abbreviation_registers_a_trigger():
    # Cosmic Rage's voice-only line arrives via `#trig {$buffer *}` -- TRIG wasn't a
    # recognized definition command, so the pack's speech line never registered.
    sink, engine = _load("#trig {$buffer *} {#say %1}")
    engine.process_line(Line("$buffer docking clamps released"))
    assert [s[0] for s in sink.spoken] == ["docking clamps released"]


def test_bare_body_text_expands_aliases():
    # VIPMud script bodies run text "as if typed": a body may call the pack's own
    # alias by name (CR's `makebetter`). Unmatched text still reaches the wire.
    sink, engine = _load(
        "#alias {makebetter} {#var better 1}\n"
        "#trigger {improve} {makebetter; look}\n"
    )
    engine.process_line(Line("improve"))
    assert engine.get_var("better") == "1"  # alias consumed
    assert sink.sent == ["look"]  # non-alias text sent


def test_failed_play_reports_handle_zero(tmp_path):
    # VIPMud reports a failed play as %playhandle 0; CR branches on it to speak the
    # failure and auto-fetch the file.
    sink, engine = _load(
        "#trigger {chirp} {#play {missing.wav}; #if {%playhandle=0} {#say failed}}",
        base_dir=str(tmp_path),
    )
    engine.process_line(Line("chirp"))
    assert [s[0] for s in sink.spoken] == ["failed"]


def test_unvar_deletes_for_defined(tmp_path):
    sink, engine = _load(
        "#trigger {setup} {#var flag 1; #unvar flag;"
        " #if {%defined(flag) = 0} {#say gone}}"
    )
    engine.process_line(Line("setup"))
    assert [s[0] for s in sink.spoken] == ["gone"]


# --- character classes in patterns ---
#
# Real patterns lifted from the Star Conquest pack (sc-release-full), matched against real
# lines from a session transcript. Every one of these silently matched nothing before
# vip_pattern_to_regex learned that [...] is a class: the packs write a literal bracket as
# "[[]", and escaping it produced a regex demanding the three characters "[[]".

CHANNEL_CASES = [
    ("[[]*] * *", '[General Communication] Sam Caldwell transmits, "Hello all."'),
    ("[[]*] * *", '[Fringe | 10: Conquest Status] A liaison transmits, "Come on!"'),
    ("[[]CrewFinder] *", "[CrewFinder] Someone is recruiting."),
    ("[[]Notification from *] *", "[Notification from Ops] Report in."),
    ('[[]Mentor Channel[]] * says, "*"', '[Mentor Channel] Tutor says, "Welcome aboard."'),
    ("[[]*[]] Jump In: *", "[Rustbucket] Jump In: Sol system."),
]


SC_COMMUNICATOR_TRIGGER = r"""
#var silent 1
#var commson 1
#var vol 50
#trig {[[]*] * *} {
#if {%ifWord(Fringe,%1," ")} {
#if {@commson = 1} {
#if {@silent = 1} {
#play {Star Conquest\general\comm\alliance.wav} @vol}};
#abort 1};
#if {%ifWord(General,%1," ")} {
#if {@commson = 1} {
#if {@silent = 1} {
#play {Star Conquest\General\Comm\General.wav} @vol}};
#abort 1};
#if {%ifWord(Newbie,%1," ")} {
#if {@commson = 1} {
#if {@silent = 1} {
#play {Star Conquest\General\Comm\newbie.wav} @vol}};
#abort 1};
#if {%ifWord(Tactical,%1," ")} {
#if {@commson = 1} {
#if {@silent = 1} {
#play {Star Conquest\General\Comm\tactical.wav} @vol}};
#abort 1}}
"""


@pytest.mark.parametrize(("pattern", "line"), CHANNEL_CASES)
def test_bracketed_channel_patterns_match_their_lines(pattern, line):
    assert re.search(vip_pattern_to_regex(pattern), line)


@pytest.mark.parametrize(
    ("line", "cue"),
    [
        ('[General Communication] Sam transmits, "Hello."', "Comm/General.wav"),
        ('[Newbie Help] Sam transmits, "Hello."', "Comm/newbie.wav"),
        ('[Tactical Communication] Sam transmits, "Hello."', "Comm/tactical.wav"),
        ('[Fringe | 10: Conquest Status] Sam transmits, "Hello."', "comm/alliance.wav"),
    ],
)
def test_star_conquest_communicator_dispatches_a_distinct_channel_cue(line, cue):
    sink, engine = _load(SC_COMMUNICATOR_TRIGGER)
    engine.process_line(Line(line))
    assert len(sink.played) == 1
    # The pack references the cue with backslashes; resolution normalizes separators so
    # the path is real on every platform. Identity, not spelling, is the contract.
    played = sink.played[0]["file"].replace("\\", "/").lower()
    assert played.endswith(cue.lower())


def test_star_conquest_custom_community_channel_supports_compound_condition():
    sink, engine = _load(
        r"""
        #var silent 1
        #var commson 1
        #var org {Explorers Communication}
        #trig {[[]*] * *} {
        #if {(%ifword(%1,@org," ")) and (@commson = 1) and (@silent = 1)} {
        #play {Star Conquest\general\comm\community1.wav} 50}}
        """
    )
    engine.process_line(Line('[Explorers Communication] Sam transmits, "Hello."'))
    assert sink.played
    played = sink.played[-1]["file"].replace("\\", "/")
    assert played.endswith("comm/community1.wav")


def test_star_conquest_not_defined_guard_initializes_a_missing_setting():
    _sink, engine = _load("#if {NOT %Defined(commson)} {#var commson 1}")
    assert engine.get_var("commson") == "1"


def test_ifword_composes_with_or_without_matching_partial_words():
    sink, engine = _load(
        r"""
        #trigger {channel *} {
        #if {%ifword(Newbie,%1," ") or %ifword(General,%1," ")} {#say matched}}
        """
    )
    engine.process_line(Line("channel General Communication"))
    engine.process_line(Line("channel Generalized"))
    assert [text for text, _channel, _interrupt in sink.spoken] == ["matched"]


def test_literal_bracket_class_captures_the_channel_name():
    match = re.search(vip_pattern_to_regex("[[]*] * *"), "[General Communication] Sam waves.")
    assert match is not None
    assert match.group(1) == "General Communication"


def test_a_class_of_alternatives_matches_either_member():
    # "[ [ ]" is "a space or a bracket" -- some of these lines arrive with a leading space.
    regex = vip_pattern_to_regex("[ [ ]MTTS] *: *")
    assert re.search(regex, " MTTS] Someone: hello")
    assert re.search(regex, "[MTTS] Someone: hello")


def test_negated_class_is_preserved():
    regex = vip_pattern_to_regex("say [^0-9]*")
    assert re.search(regex, "say hello")
    assert not re.search(regex, "say 9 lives")


def test_an_unclosed_bracket_stays_a_literal():
    assert re.search(vip_pattern_to_regex("cost [50 credits"), "cost [50 credits")


def test_a_malformed_class_falls_back_to_a_literal_rather_than_failing_the_load():
    # "[z-a]" is a bad range; escaping beats raising, which would kill the whole pack load.
    assert re.search(vip_pattern_to_regex("range [z-a] here"), "range [z-a] here")


def test_bracket_patterns_compile_without_a_nested_set_warning():
    # Python warns on "[[]"; the converter emits "[\[]" instead so packs load quietly.
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        for pattern, _line in CHANNEL_CASES:
            re.compile(vip_pattern_to_regex(pattern))


def test_plain_patterns_are_unchanged_by_the_class_support():
    assert vip_pattern_to_regex("You see * here.") == r"You\ see\ (.*?)\ here\."
    # ? matches one character but doesn't take a wildcard number: only * does.
    assert vip_pattern_to_regex("?Enter ?yes' or ?no'?") == r".Enter\ .yes'\ or\ .no'."


def test_variable_names_are_case_insensitive():
    # VIPMud folds variable case; real packs rely on it (Star Conquest's communicator
    # sets `vol` and reads both `@vol` and `@Vol` -- the mismatch read as "", which
    # #play turned into the default volume of 100).
    sink, engine = _load(
        "#VAR Vol {35}\n"
        "#TRIGGER {go} {#play {x.wav} @vOL}\n"
        "#TRIGGER {check} {#if {%defined(VOL)} {#say {have it}}}"
    )
    engine.process_line(Line("go"))
    assert sink.played and abs(sink.played[0]["gain"] - 0.35) < 1e-9
    engine.process_line(Line("check"))
    assert ("have it", "main", False) in sink.spoken


def test_math_assignment_and_read_fold_case_together():
    sink, engine = _load(
        "#TRIGGER {calc} {#math {Total} {2 + 3}; #say {@toTAL done}}"
    )
    engine.process_line(Line("calc"))
    assert sink.spoken == [("5 done", "main", False)]


def test_stop_reaches_the_dialects_own_cues_not_the_default_channel():
    # #Stop halts every playing sound. Plays go to per-cue vip-N channels, so the old
    # stop of the api default channel ("sound") never touched the dialect's own loops.
    class _FlushBus(SoundBus):
        def __init__(self) -> None:
            super().__init__()
            self.flushes = 0

        def flush(self) -> None:
            self.flushes += 1

    bus = _FlushBus()
    sink = RecordingSink()
    engine = AutomationEngine(sink, sound=bus)
    VipMudPack(ScriptApi(engine, source="vipmud")).load_source(
        "#TRIGGER {go} {#playloop {amb.wav}}\n#TRIGGER {halt} {#stop}"
    )
    engine.process_line(Line("go"))
    assert sink.played and sink.played[0]["channel"] == "vip-1"
    engine.process_line(Line("halt"))
    assert bus.flushes == 1
    assert sink.stopped == []  # not the old, useless stop("sound")


def test_alias_name_is_the_first_word_and_its_words_are_the_parameters():
    # Real packs: #alias {dk} {dirtkick %1} (Cogg) and #Alias {reply} {tell @tellname %0}.
    sink, engine = _load(
        "#var tellname bob\n#alias {dk} {dirtkick %1}\n#Alias {reply} {tell @tellname %0} {}"
    )
    engine.process_input("dk goblin")
    engine.process_input("reply thanks for the help")
    assert sink.sent == ["dirtkick goblin", "tell bob thanks for the help"]


def test_short_alias_names_leave_longer_commands_alone():
    # Prometheus's fly-mode file defines one-letter aliases s, se, n, e, w...; they used to
    # swallow say, score, north and exits, and "s" caught "se" before its own alias.
    sink, engine = _load("#alias s {south}\n#alias se {southeast}")
    assert engine.process_input("say hello") == ["say hello"]
    engine.process_input("se")
    engine.process_input("S")  # alias names are case-insensitive, like VIPMud variables
    assert sink.sent == ["southeast", "south"]


@pytest.mark.parametrize(
    ("pack_key", "pressed"),
    [
        ("alt-f2", "alt+f2"),  # Cosmic Rage VIPMud
        ("Alt-shift-right", "alt+shift+right"),
        ("Control-alt-shift-S", "ctrl+alt+shift+s"),  # Star Conquest
        ("Shift-Alt-PageUp", "alt+shift+pageup"),
        ("control-f11", "ctrl+f11"),  # Prometheus
        ("Alt--", "alt+-"),  # the minus key itself
        ("f9", "f9"),
    ],
)
def test_hotkey_names_reach_the_combo_the_window_sends(pack_key, pressed):
    # Packs write "alt-f2"; the window sends "alt+f2". Taken literally, every hotkey with a
    # modifier was dead: 124 of Cosmic Rage's 148, 32 of Star Conquest's 40.
    sink, engine = _load(f"#key {pack_key} {{score}}")
    assert engine.press_key(pressed)
    assert sink.sent == ["score"]


def _remote_pack(tmp_path):
    (tmp_path / "SoundSync.exe").write_bytes(b"windows helper")
    (tmp_path / "installer.bat").write_text(
        'set "REPO_URL=https://gitea.example/Cosmic/Sounds"\nset "REPO_SUBFOLDER=wav"\n',
        encoding="latin-1",
    )
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    pack = VipMudPack(ScriptApi(engine, source="vip", base_dir=str(tmp_path)))
    pack.load_source(
        SPHOOK + "\n#alias {quiet} {#pc 1 stop}\n#alias {place} {#pc 1 volume 30;#pc 1 pan -2500}"
    )
    return sink, engine


async def _until(condition, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting"
        await asyncio.sleep(0.01)


async def test_a_missing_sound_downloads_off_the_loop_and_plays_when_it_lands(
    tmp_path, monkeypatch
):
    # The download used to run inside line processing, on the loop every tab and all speech
    # share, with no timeout: each new Cosmic Rage sound froze the whole client.
    release = threading.Event()

    def download(url, destination, **_kwargs):
        release.wait(5)
        destination.write_bytes(b"RIFF-fake-wave")

    monkeypatch.setattr("genericmud.packs.vault.download", download)
    sink, engine = _remote_pack(tmp_path)
    engine.process_line(Line("$sphook play:general/misc/on:80:0:0:1"))
    assert sink.played == []  # line processing didn't wait for the download
    release.set()
    await _until(lambda: sink.played)
    assert Path(sink.played[0]["file"]).read_bytes() == b"RIFF-fake-wave"


async def test_a_cue_stopped_or_closed_while_downloading_never_starts(tmp_path, monkeypatch):
    release = threading.Event()
    finished = threading.Event()

    def download(url, destination, **_kwargs):
        release.wait(5)
        destination.write_bytes(b"RIFF")
        finished.set()

    monkeypatch.setattr("genericmud.packs.vault.download", download)
    sink, engine = _remote_pack(tmp_path)
    engine.process_line(Line("$sphook play:a:80:0:0:1"))
    engine.process_input("quiet")  # #pc 1 stop while the file is still on its way
    engine.process_line(Line("$sphook play:b:80:0:0:2"))
    engine.cancel_timers()  # what closing the session does
    release.set()
    await asyncio.get_running_loop().run_in_executor(None, finished.wait, 5)
    await asyncio.sleep(0.05)
    assert sink.played == []


async def test_a_sound_the_server_lacks_is_not_fetched_again_on_every_cue(tmp_path, monkeypatch):
    calls: list[str] = []

    def download(url, destination, **_kwargs):
        calls.append(url)
        raise OSError("404")

    monkeypatch.setattr("genericmud.packs.vault.download", download)
    sink, engine = _remote_pack(tmp_path)
    engine.process_line(Line("$sphook play:gone:80:0:0:1"))
    await _until(lambda: calls)
    await asyncio.sleep(0.05)
    for _ in range(4):
        engine.process_line(Line("$sphook play:gone:80:0:0:1"))
    assert len(calls) == 1


async def test_volume_and_pan_set_while_a_sound_downloads_are_kept(tmp_path, monkeypatch):
    # Packs place a cue right after #play. While the file was still downloading, #pc
    # adjusted a channel with nothing on it, and the cue then started at its old volume,
    # centred.
    release = threading.Event()

    def download(url, destination, **_kwargs):
        release.wait(5)
        destination.write_bytes(b"RIFF")

    monkeypatch.setattr("genericmud.packs.vault.download", download)
    sink, engine = _remote_pack(tmp_path)
    engine.process_line(Line("$sphook play:a:80:0:0:1"))
    engine.process_input("place")  # #pc 1 volume 30 and #pc 1 pan -2500, before it lands
    release.set()
    await _until(lambda: sink.played)
    assert (sink.played[0]["gain"], sink.played[0]["pan"]) == (0.3, -0.5)


async def test_a_sound_that_failed_to_download_is_tried_again_later(tmp_path, monkeypatch):
    # One timeout on a multi-gigabyte sound tree shouldn't silence that sound until restart.
    calls: list[str] = []

    def download(url, destination, **_kwargs):
        calls.append(url)
        raise OSError("timed out")

    monkeypatch.setattr("genericmud.packs.vault.download", download)
    monkeypatch.setattr("genericmud.scripting.vipmud_dialect._FETCH_RETRY_SECONDS", 0.0)
    sink, engine = _remote_pack(tmp_path)
    engine.process_line(Line("$sphook play:flaky:80:0:0:1"))
    await _until(lambda: calls)
    await asyncio.sleep(0.05)
    engine.process_line(Line("$sphook play:flaky:80:0:0:1"))
    await _until(lambda: len(calls) == 2)


def _installer_pack(tmp_path):
    (tmp_path / "SoundSync.exe").write_bytes(b"windows helper")
    (tmp_path / "installer.bat").write_text(
        'set "REPO_URL=https://gitea.example/Cosmic/Sounds"\nset "REPO_SUBFOLDER=wav"\n',
        encoding="latin-1",
    )
    pack = VipMudPack(
        ScriptApi(AutomationEngine(RecordingSink()), source="vip", base_dir=str(tmp_path))
    )
    pack.load_source(SPHOOK)
    return pack


def test_whether_a_sound_can_be_fetched_is_decided_without_asking_the_disk(
    tmp_path, monkeypatch
):
    # On Windows, resolve() turned down a second missing sound while the first one's
    # download was creating the sounds folder, so the second was played as a miss and
    # never fetched. The decision is made on the path alone now.
    pack = _installer_pack(tmp_path)
    sounds = tmp_path / "sounds" / "wav"

    def unreliable(_path, strict=False):
        raise OSError("the folder is being created")

    monkeypatch.setattr(Path, "resolve", unreliable)
    assert pack.can_fetch_sound(str(sounds / "combat" / "hit.wav"))
    assert not pack.can_fetch_sound(str(sounds / ".." / ".." / "escape.wav"))
    assert not pack.can_fetch_sound(str(sounds / "notes.txt"))


@pytest.mark.skipif(sys.platform == "win32", reason="making a link needs admin rights there")
def test_a_download_is_never_written_through_a_link_out_of_the_sounds_folder(
    tmp_path, monkeypatch
):
    pack = _installer_pack(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sounds = tmp_path / "sounds" / "wav"
    sounds.mkdir(parents=True)
    (sounds / "evil").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        "genericmud.packs.vault.download",
        lambda _url, destination, **_kwargs: destination.write_bytes(b"RIFF"),
    )
    assert pack._fetch_remote_sound(str(sounds / "evil" / "hit.wav")) is False
    assert pack._fetch_remote_sound(str(sounds / "evil" / "deeper" / "hit.wav")) is False
    assert list(outside.iterdir()) == []  # no file, and no folder made for one either
    assert pack._fetch_remote_sound(str(sounds / "combat" / "hit.wav")) is True



def test_a_tilde_keeps_braces_and_semicolons_from_ending_their_statement():
    # VIPMud's escape character: ~ makes the next character ordinary, ~~ is a tilde. The
    # parser took ~} as the end of a block and ~; as a statement break.
    assert tokenize_statements("say a~;b\n#var x {a~}b~~}") == [
        (None, [_Tok(False, "say a~;b")]),
        ("VAR", [_Tok(False, "x"), _Tok(True, "a~}b~~")]),
    ]


def test_loading_prometheus_save_config_sends_nothing_to_the_mud():
    # The official Prometheus pack's Conf.set. Its escaped braces closed the alias body
    # early, and the two leftover "}" lines went to the MUD right after login.
    sink, engine = _load(
        "#alias SaveConfig {\n"
        " #For @SPOptions {\n"
        "  #If {%IsNumber(@{@saveTmp1})=1} {\n"
        "   #Var saveTmp2 {@saveTmp2#Var @saveTmp1 @{@saveTmp1}%CRLF}\n"
        "  } {\n"
        "   #Var saveTmp3 {%Replace(@{@saveTmp1},~~,~~~~)};\n"
        "   #Var saveTmp2 {@saveTmp2#Var @saveTmp1 {%Replace(@saveTmp3,~},~~~})}%CRLF}\n"
        "  }\n"
        " }\n"
        "}\n"
        "#alias spload {#say loaded}\n"
    )
    assert sink.sent == []
    assert engine.process_input("spload") == []
