"""Safe-walk: step-by-step movement, GMCP/timeout advance, blocked halt, stop key."""

from __future__ import annotations

from genericmud.app import EngineApp
from genericmud.config.keymap import load_keymap
from genericmud.navigation import SafeWalk
from genericmud.protocol.telnet import OPT_GMCP, DataReceived, Subnegotiation
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend


class _Scheduler:
    """Manual scheduler: captures scheduled callbacks so tests fire them explicitly."""

    def __init__(self) -> None:
        self.calls: list = []
        self.delays: list[float] = []

    def __call__(self, delay, callback) -> None:
        self.calls.append(callback)
        self.delays.append(delay)

    def fire_last(self) -> None:
        self.calls[-1]()


def _walk(steps, **kwargs):
    sent: list[str] = []
    announced: list[str] = []
    sched = _Scheduler()
    walk = SafeWalk(steps, send=sent.append, schedule=sched, announce=announced.append, **kwargs)
    return walk, sent, announced, sched


def test_safe_walk_advances_on_timeout_then_arrives():
    walk, sent, announced, sched = _walk(["n", "e"])
    walk.start()
    assert sent == ["n"]  # only the first step goes out
    sched.fire_last()  # per-step timeout -> next step
    assert sent == ["n", "e"]
    sched.fire_last()  # timeout past the last step -> arrived
    assert "arrived" in announced
    assert not walk.active


def test_safe_walk_advances_on_room_change_and_ignores_stale_timeout():
    walk, sent, announced, sched = _walk(["n", "e"])
    walk.start()  # sends n, schedules timeout (token 1)
    walk.on_room_change()  # confirmed move -> sends e, schedules timeout (later token)
    assert sent == ["n", "e"]
    sched.calls[0]()  # fire the STALE timeout from step 1 -> no-op
    assert sent == ["n", "e"]
    sched.calls[1]()  # fire the live timeout -> arrived
    assert "arrived" in announced


def test_safe_walk_halts_on_blocked_line():
    walk, sent, announced, _sched = _walk(["n", "e", "s"])
    walk.start()
    assert sent == ["n"]
    walk.on_line("You can't go that way.")
    assert not walk.active
    assert sent == ["n"]  # e and s abandoned
    assert any("blocked" in message for message in announced)


def _app():
    backend = RecordingBackend()
    voice = VoiceRouter(backend, clock=lambda: 0.0)
    sent: list[str] = []
    sched = _Scheduler()
    app = EngineApp(
        voice, send=sent.append, post=[].append, schedule=sched, keymap=load_keymap("vipmud")
    )
    return app, sent, sched, backend


def test_app_double_dot_starts_a_safe_walk():
    app, sent, sched, _backend = _app()
    app.on_ws_message({"type": "input", "text": "..2n"})
    assert sent == ["n"]  # one step at a time, not both
    assert app.nav.trail == ["n"]  # recorded for breadcrumb too
    sched.fire_last()
    assert sent == ["n", "n"]


def test_app_safe_walk_advances_on_gmcp_room_change():
    app, sent, _sched, _backend = _app()
    app.on_ws_message({"type": "input", "text": "..2e"})
    assert sent == ["e"]
    app.on_telnet_event(Subnegotiation(OPT_GMCP, b'room.info {"num": 2}'))  # moved
    assert sent == ["e", "e"]


def test_app_safe_walk_halts_on_blocked_then_stop_key_is_idle():
    app, sent, _sched, backend = _app()
    app.on_ws_message({"type": "input", "text": "..3n"})
    app.on_telnet_event(DataReceived(b"You can't go that way.\r\n"))
    assert not app._walk.active
    assert sent == ["n"]
    app._handle_key("alt+s")  # nav:stop with nothing walking
    assert any("not walking" in spoken for spoken in backend.spoken)


class _Clock:
    """A scheduler with time in it, for lag: callbacks fire in order as time advances."""

    def __init__(self) -> None:
        self.now = 0.0
        self.pending: list[tuple[float, object]] = []

    def __call__(self, delay, callback) -> None:
        self.pending.append((self.now + delay, callback))

    def advance_to(self, when: float) -> None:
        while True:
            due = [entry for entry in self.pending if entry[0] <= when]
            if not due:
                break
            entry = min(due, key=lambda pair: pair[0])
            self.pending.remove(entry)
            self.now = entry[0]
            entry[1]()
        self.now = when


def test_mapped_walk_waits_out_a_slow_room_report():
    # Lag of 0.7 s: the half-second timeout sent step two, then step one's report arrived
    # naming the room step one was heading for, and the walk took that as being moved off
    # the route and gave up.
    clock = _Clock()
    announced: list[str] = []
    here = {"room": "A"}
    walk = SafeWalk(
        ["n", "n", "n"],
        send=lambda _direction: None,
        schedule=clock,
        announce=announced.append,
        waypoints=["B", "C", "D"],
        locate=lambda: here["room"],
    )
    walk.start()
    clock.advance_to(0.7)
    here["room"] = "B"
    walk.on_room_change()
    assert walk.active, announced
    here["room"] = "C"
    walk.on_room_change()
    here["room"] = "D"
    walk.on_room_change()
    assert announced == ["arrived"]


def test_typed_walk_does_not_say_arrived_before_the_last_step_lands():
    clock = _Clock()
    sent: list[str] = []
    announced: list[str] = []
    walk = SafeWalk(["n", "n", "n"], send=sent.append, schedule=clock, announce=announced.append)
    walk.start()
    clock.advance_to(0.7)
    walk.on_room_change()  # step one's report, late: the MUD does report rooms after all
    clock.advance_to(1.25)
    assert "arrived" not in announced, (sent, announced)


def test_walk_waits_longer_for_each_step_on_a_mud_that_reports_rooms():
    app, sent, sched, _backend = _app()
    app.on_telnet_event(Subnegotiation(OPT_GMCP, b'room.info {"num": 1}'))
    app.on_ws_message({"type": "input", "text": "..2n"})
    assert sent == ["n"]
    assert sched.delays[-1] > 0.5  # the report is the signal; the timeout only rides out lag

