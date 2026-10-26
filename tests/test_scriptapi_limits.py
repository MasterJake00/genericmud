"""A hostile pack must not DoS the client through the legitimate ScriptApi (finding G)."""

from __future__ import annotations

from genericmud.automation.engine import AutomationEngine, EngineSink
from genericmud.scripting.api import (
    _MAX_ACTIVE_TIMERS,
    _MAX_VAR_VALUE_LEN,
    _MIN_TIMER_DELAY,
    ScriptApi,
)


class _RecordingSink(EngineSink):
    def __init__(self) -> None:
        self.scheduled: list[tuple[float, object]] = []

    def schedule(self, delay, callback) -> None:
        self.scheduled.append((delay, callback))


def test_add_timer_is_capped():
    api = ScriptApi(AutomationEngine())  # base sink.schedule is a no-op, so timers never "fire"
    for _ in range(_MAX_ACTIVE_TIMERS + 50):
        api.add_timer(0.0, lambda: None)
    assert api._active_timers == _MAX_ACTIVE_TIMERS  # excess refused


def test_a_refused_timer_is_noted_in_the_diagnostic_log():
    # A repeating MUSHclient timer that is refused once never fires again; at least say so.
    events: list[tuple[str, dict]] = []
    engine = AutomationEngine()
    engine.diag = type("Diag", (), {"event": lambda _self, name, **fields: events.append(
        (name, fields))})()
    api = ScriptApi(engine, source="pack")
    for _ in range(_MAX_ACTIVE_TIMERS + 3):
        api.add_timer(1.0, lambda: None)
    refusals = [fields for name, fields in events if name == "pack.timer_refused"]
    assert refusals == [{"source": "pack", "limit": _MAX_ACTIVE_TIMERS}]  # once, not per call


def test_add_timer_clamps_near_zero_delay():
    sink = _RecordingSink()
    api = ScriptApi(AutomationEngine(sink))
    api.add_timer(0.0, lambda: None)
    assert sink.scheduled[0][0] >= _MIN_TIMER_DELAY


def test_add_timer_frees_its_slot_when_it_fires():
    sink = _RecordingSink()
    api = ScriptApi(AutomationEngine(sink))
    api.add_timer(0.0, lambda: None)
    assert api._active_timers == 1
    _delay, wrapped = sink.scheduled[0]
    wrapped()  # the event loop fires the timer
    assert api._active_timers == 0  # a self-rearming heartbeat stays at steady state


class _Handle:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _CancellableSink(EngineSink):
    def __init__(self) -> None:
        self.handles: list[_Handle] = []

    def schedule(self, delay, callback) -> _Handle:
        self.handles.append(_Handle(callback))
        return self.handles[-1]


def test_a_cancelled_timer_frees_its_slot_once():
    # A dropped connection cancels a pack's pending timers. Their slots must come back, or a
    # pack that re-arms its timers on every reconnect is refused for good after a few drops.
    sink = _CancellableSink()
    engine = AutomationEngine(sink)
    api = ScriptApi(engine, source="pack")
    other = ScriptApi(engine, source="other")
    api.add_timer(1.0, lambda: None)
    api.add_timer(1.0, lambda: None)
    other.add_timer(1.0, lambda: None)
    engine.cancel_source_timers("pack")
    assert (api._active_timers, other._active_timers) == (0, 1)
    engine.cancel_timers()
    engine.cancel_timers()  # nothing is left to cancel a second time
    assert other._active_timers == 0
    assert all(handle.cancelled for handle in sink.handles)
    sink.handles[0].callback()  # a cancelled timer that runs anyway doesn't free it twice
    assert api._active_timers == 0


def test_set_var_rejects_oversized_value():
    engine = AutomationEngine()
    api = ScriptApi(engine)
    api.set_var("x", "a" * (_MAX_VAR_VALUE_LEN + 1))
    assert engine.get_var("x") == ""  # refused
    api.set_var("y", "fine")
    assert engine.get_var("y") == "fine"
