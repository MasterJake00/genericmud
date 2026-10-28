"""Unit tests for the dialect-agnostic AutomationEngine."""

from __future__ import annotations

import pytest

from genericmud.automation.engine import AutomationEngine
from genericmud.model.buffer import Line
from tests.helpers import RecordingSink


def test_trigger_fires_with_wildcards():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    captured: dict[str, list[str]] = {}

    def cb(ctx):
        captured["wc"] = ctx.wildcards
        ctx.engine.sink.send("got " + ctx.wildcards[1])

    engine.add_trigger("You see *", cb)
    engine.process_line(Line("You see a dragon"))
    assert captured["wc"] == ["You see a dragon", "a dragon"]
    assert sink.sent == ["got a dragon"]


def test_redos_trigger_times_out_and_is_disabled():
    pytest.importorskip("regex")  # the per-match timeout needs the regex module
    engine = AutomationEngine(RecordingSink())
    fired: list[int] = []
    # A catastrophic pack pattern must not hang the engine on a crafted line: the per-match
    # timeout fires, the line is treated as no-match, and the offending rule is disabled.
    engine.add_trigger(r"(a|a)+$", lambda ctx: fired.append(1), regex=True, name="redos")
    engine.process_line(Line("a" * 60 + "!"))
    assert fired == []
    assert all(not rule.enabled for rule in engine._triggers if rule.name == "redos")


def test_gag_and_gag_but_display():
    engine = AutomationEngine()
    engine.add_trigger("noise", None, gag=True)
    engine.add_trigger("spammy", None, gag_but_display=True)
    assert engine.process_line(Line("noise here")).display_when_gagged is False
    spam = engine.process_line(Line("spammy tick"))
    assert spam.gagged and spam.display_when_gagged


def test_priority_then_keep_evaluating():
    engine = AutomationEngine()
    order: list[str] = []
    engine.add_trigger("x", lambda c: order.append("low"), priority=1)
    engine.add_trigger("x", lambda c: order.append("high"), priority=10)
    engine.process_line(Line("x"))
    assert order == ["high", "low"]

    stop_order: list[str] = []
    engine2 = AutomationEngine()
    engine2.add_trigger(
        "y", lambda c: stop_order.append("first"), priority=10, keep_evaluating=False
    )
    engine2.add_trigger("y", lambda c: stop_order.append("second"), priority=1)
    engine2.process_line(Line("y"))
    assert stop_order == ["first"]


def test_alias_consumes_or_passes_through():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    engine.add_alias(
        r"^/gather (.+)$",
        lambda c: c.engine.sink.send("gather " + c.wildcards[1]),
        regex=True,
    )
    assert engine.process_input("/gather mining") == []
    assert sink.sent == ["gather mining"]
    assert engine.process_input("north") == ["north"]


def test_a_scoped_stop_only_ends_its_own_scope():
    # A soundpack's rule that stops evaluation ends that pack's rules for the line or command,
    # not everyone's; a rule with no scope still stops everything after it.
    engine = AutomationEngine()
    fired: list[str] = []

    def add(label: str, priority: int, scope: str | None) -> None:
        engine.add_trigger(
            "x", lambda c: fired.append(label), priority=priority, keep_evaluating=False,
            stop_scope=scope,
        )

    add("a1", 30, "a")
    add("a2", 20, "a")
    add("b", 10, "b")
    add("plain", 5, None)
    add("after plain", 1, None)
    engine.process_line(Line("x"))
    assert fired == ["a1", "b", "plain"]


def test_aliases_that_take_the_command_and_fallbacks():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    said: list[str] = []
    # Keeps evaluating but still takes the command: what was typed isn't sent as well.
    engine.add_alias(
        "c *", lambda c: c.engine.sink.send("cast " + c.wildcards[1]), priority=30,
        keep_evaluating=True, stop_scope="a", consumes=True,
    )
    engine.add_alias("c *", lambda c: said.append("b"), priority=20, stop_scope="b")
    # Only runs when nothing before it took the command.
    engine.add_alias("*", lambda c: said.append("fallback"), priority=-10, fallback=True)
    assert engine.process_input("c fireball") == []
    assert sink.sent == ["cast fireball"]
    assert said == ["b"]
    assert engine.process_input("look") == []
    assert said == ["b", "fallback"]


def test_a_keep_evaluating_alias_that_doesnt_take_the_command_lets_it_through():
    engine = AutomationEngine()
    seen: list[str] = []
    engine.add_alias("north", lambda c: seen.append("noted"), keep_evaluating=True)
    assert engine.process_input("north") == ["north"]
    assert seen == ["noted"]


def test_key_press():
    sink = RecordingSink()
    engine = AutomationEngine(sink)
    engine.add_key("f2", lambda c: c.engine.sink.send("score"))
    assert engine.press_key("F2") is True
    assert engine.press_key("f9") is False
    assert sink.sent == ["score"]


def test_var_and_gvar_fallback():
    engine = AutomationEngine()
    engine.set_var("hp", 42)
    assert engine.get_var("hp") == "42"
    engine.set_gvar("realm", "Erion")
    assert engine.get_var("realm") == "Erion"


def test_structured_mud_variables_resolve_longest_prefix_and_case_insensitively():
    engine = AutomationEngine()
    engine.set_mud_var("Char.Vitals", {"HP": 42, "effects": [{"name": "haste"}]})
    assert engine.get_mud_var("char.vitals.hp") == 42
    assert engine.get_mud_var("Char.Vitals.effects.0.name") == "haste"
    assert engine.get_mud_var("Char.Vitals.missing", "fallback") == "fallback"


class _RaisingPattern:
    """Stands in for a compiled pattern that blows the per-match timeout budget."""

    pattern = "(a+)+$"  # the classic runaway shape, with no fixed text to look for first

    def search(self, text, **kwargs):
        raise TimeoutError("match budget exceeded")

    def match(self, text, **kwargs):
        raise TimeoutError("match budget exceeded")


class _FakeDiag:
    def __init__(self):
        self.events = []

    def event(self, stage, **fields):
        self.events.append((stage, fields))


def test_trigger_match_timeout_disables_and_traces():
    engine = AutomationEngine(RecordingSink())
    engine.diag = _FakeDiag()
    engine.add_trigger("catastrophic", lambda ctx: None, source="evilpack")
    engine._triggers[0].pattern = _RaisingPattern()
    engine.process_line(Line("a line that makes it backtrack"))
    assert engine._triggers[0].enabled is False  # disabled so it can't hang every future line
    stages = [s for s, _ in engine.diag.events]
    assert "trigger.timeout_disabled" in stages  # and the silence left a durable trace


def test_alias_match_timeout_disables_and_traces():
    engine = AutomationEngine(RecordingSink())
    engine.diag = _FakeDiag()
    engine.add_alias("catastrophic", lambda ctx: None, source="evilpack")
    engine._aliases[0].pattern = _RaisingPattern()
    engine.process_input("some input")
    assert engine._aliases[0].enabled is False
    assert "alias.timeout_disabled" in [s for s, _ in engine.diag.events]


def test_has_key_reports_bound_macros_only():
    # The UI consults this before consuming a keypress: unbound combos must fall
    # through to the platform (menu access keys), bound ones must reach the macro.
    engine = AutomationEngine()
    engine.add_key("Alt+G", lambda _ctx: None)
    assert engine.has_key("alt+g")  # case-insensitive, as press_key is
    assert not engine.has_key("alt+f")
