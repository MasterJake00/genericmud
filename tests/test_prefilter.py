"""The text a trigger's pattern needs, looked for before running the regex."""

from __future__ import annotations

import random

import regex

from genericmud.automation.engine import AutomationEngine
from genericmud.automation.prefilter import required_text
from genericmud.model.buffer import Line
from tests.helpers import RecordingSink


def _text(pattern: str, flags: int = 0) -> str | None:
    return required_text(regex.compile(pattern, flags))


def test_a_vipmud_trigger_needs_its_longest_fixed_text():
    assert _text(r"^(.*?)\ breaks\ apart\ into\ pieces![ \t]*$") == " breaks apart into pieces!"
    assert _text(r"^You\ see\ (.*?)\ here\.[ \t]*$") == "You see "


def test_text_around_a_group_of_alternatives_is_still_needed():
    assert _text(r"^(?:foo|bar) baz qux") == " baz qux"
    assert _text(r"abc|abd") == "ab"  # what both alternatives start with


def test_nothing_is_certain_when_the_whole_pattern_is_alternatives():
    assert _text(r"north|south") is None
    assert _text(r"x(?:a|b)?y") is None  # "x" and "y" alone: too short to be worth a look


def test_repeats_count_only_the_copies_that_are_certain():
    assert _text(r"xa+y") == "xa"  # at least one "a", maybe more: the run ends there
    assert _text(r"colou?r") == "colo"  # an optional character ends the run before it
    assert _text(r"x(?:ab)+yz") == "yz"  # a repeated group ends the run too


def test_no_shortcut_for_case_insensitive_patterns():
    assert _text(r"hello there", regex.IGNORECASE) is None
    assert _text(r"(?i)hello there") is None


def test_no_shortcut_for_syntax_only_the_regex_module_reads():
    assert _text(r"\p{L}+ arrives") is None
    # Python's parser reads these as plain text; to the regex module they're a fuzzy match
    # and a class of digits, so a match needn't contain "{e<=1}" or "] hp".
    assert _text(r"(?:hello){e<=1} world") is None
    assert _text(r"[[:digit:]]] hp left") is None
    assert _text(r"a{3}b") is None  # any unescaped brace, to be sure


def test_escaped_braces_and_brackets_are_plain_text():
    assert _text(r"\{ok\}") == "{ok}"
    assert _text(r"^\[Fringe\ \|\ (.*?)\]\ says") == "[Fringe | "


def test_a_verbose_pattern_ignores_its_spaces():
    assert _text("hel lo  # a comment", regex.VERBOSE) == "hello"


def test_whatever_matches_contains_the_text():
    # Lines made of the patterns' own pieces, so plenty of them match.
    patterns = [
        r"^(.*?)\ breaks\ apart![ \t]*$", r"^You\ see\ (.*?)\ here\.[ \t]*$", r"xa+y",
        r"ab+c", r"^(?:foo|bar) baz", r"\d+ hp left", r"[.]txt$", r"(?:x|y)zzw",
    ]
    pieces = ["You see ", " here.", " breaks apart!", "a", "x", "y", "aa", "ab", "bb", "c",
              "foo", "bar", " baz", "12", " hp left", ".txt", "zz", "w", " ", "q", "\t",
              "xay", "abbc", "yzzw"]
    rng = random.Random(3)
    lines = ["".join(rng.choice(pieces) for _ in range(rng.randint(1, 7))) for _ in range(4000)]
    for source in patterns:
        compiled = regex.compile(source)
        needed = required_text(compiled)
        hits = 0
        for line in lines:
            if compiled.search(line):
                hits += 1
                assert needed is None or needed in line, (source, line)
        assert hits, f"no line matched {source}: the check proved nothing"


def test_a_trigger_still_fires_when_its_pattern_is_swapped():
    # MUSHclient's SetTriggerOption gives a live rule a new pattern; the text the old one
    # needed mustn't keep the line from reaching the new one.
    engine = AutomationEngine(RecordingSink())
    fired: list[str] = []
    rule = engine.add_trigger(r"^You\ see\ a\ (\w+)", lambda ctx: fired.append(ctx.wildcards[1]),
                              regex=True)
    engine.process_line(Line("You see a goblin"))
    rule.pattern = regex.compile(r"^There\ is\ a\ (\w+)")
    engine.process_line(Line("There is a gnome"))
    engine.process_line(Line("You see a troll"))
    assert fired == ["goblin", "gnome"]
