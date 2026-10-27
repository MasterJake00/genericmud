"""A world's soundpacks: checked for updates on connect, installed with /update."""

from __future__ import annotations

import json

from genericmud.app import EngineApp
from genericmud.packs import PackStore
from genericmud.packs.git_sources import UpdateSource
from genericmud.packs.pack_updates import CHECK_INTERVAL_SECONDS, PackUpdates
from genericmud.packs.setup import GitUpdateResult, PendingUpdate
from genericmud.packs.store import PackError
from genericmud.voice.router import VoiceRouter
from tests.helpers import RecordingBackend

SOURCE = UpdateSource("toastush", "Toastush", "https://example.test/t.git", "worlds/Miriani.MCL")


def _right_away(job, done):
    done(job())


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _updates(tmp_path, *, check=None, update=None, runner=_right_away, installed=None):
    store = PackStore(tmp_path / "store")
    store.pack_dir("toastush").mkdir(parents=True)
    said: list[str] = []
    clock = _Clock()
    calls: list[str] = []

    def default_check(_store, pack_id, _source):
        calls.append(pack_id)
        return PendingUpdate(pack_id, "Toastush", 34)

    updates = PackUpdates(
        store, announce=said.append, run_in_background=runner,
        check=check or default_check,
        update=update or (lambda *_args, **_kw: GitUpdateResult("Toastush", False, written=34)),
        source_for=lambda pack_id: SOURCE if pack_id == "toastush" else None,
        clock=clock,
        installed=installed,
    )
    return updates, said, clock, calls


class _Held:
    """A background runner that keeps each job until the test finishes it."""

    def __init__(self) -> None:
        self.jobs: list[tuple] = []

    def __call__(self, job, done) -> None:
        self.jobs.append((job, done))

    def finish(self, index: int = 0) -> None:
        job, done = self.jobs.pop(index)
        done(job())


def test_a_pack_with_an_update_says_so_once_a_day(tmp_path):
    updates, said, clock, calls = _updates(tmp_path)
    updates.check_due(["toastush", "local-pack"])
    assert said == ["Toastush has an update: 34 files. Type /update to install it."]
    updates.check_due(["toastush"])  # later the same day: not asked again
    assert calls == ["toastush"]
    clock.now += CHECK_INTERVAL_SECONDS
    updates.check_due(["toastush"])
    assert calls == ["toastush", "toastush"]


def test_a_host_thats_down_is_asked_again_an_hour_later_without_a_word(tmp_path):
    # Not on every reconnect, which could be minutes apart, and not a whole day later.
    asked = []

    def unreachable(_store, pack_id, _source):
        asked.append(pack_id)
        raise PackError("couldn't check Toastush for updates: the host is busy")

    updates, said, clock, _calls = _updates(tmp_path, check=unreachable)
    updates.check_due(["toastush"])
    clock.now += 10 * 60
    updates.check_due(["toastush"])  # ten minutes later: not yet
    assert asked == ["toastush"]
    clock.now += 50 * 60
    updates.check_due(["toastush"])  # an hour on: again
    assert asked == ["toastush", "toastush"]
    assert said == []


def test_an_update_that_cant_start_doesnt_leave_updates_stuck(tmp_path):
    # Without a running loop nothing can run in the background; "already being installed"
    # from then on would be a lie.
    updates, said, _clock, _calls = _updates(tmp_path, runner=lambda job, done: False)
    updates.install(["toastush"])
    updates.install(["toastush"])
    assert "Soundpack updates are already being installed." not in said


def test_update_installs_and_says_what_it_did(tmp_path):
    updates, said, _clock, _calls = _updates(tmp_path)
    updates.install(["toastush"])
    assert said == [
        "Checking for soundpack updates.",
        "Updated Toastush: 34 files updated. Reconnect to apply.",
    ]


def test_a_quiet_update_with_nothing_new_says_nothing(tmp_path):
    # Miriani-Next asks for "update quietly" at every login when its player turned that on.
    updates, said, _clock, _calls = _updates(
        tmp_path, update=lambda *_args, **_kw: GitUpdateResult("Toastush", True),
    )
    updates.install(["toastush"], quiet=True)
    assert said == []


def test_update_explains_a_failure_and_packs_it_cant_update(tmp_path):
    def broken(*_args, **_kw):
        raise PackError("couldn't download Toastush's update: the host is busy")

    updates, said, _clock, _calls = _updates(tmp_path, update=broken)
    updates.install(["toastush"])
    updates.install(["local-pack"])
    assert said == [
        "Checking for soundpack updates.",
        "couldn't download Toastush's update: the host is busy",
        "None of this world's soundpacks can be updated from here.",
    ]


def test_a_second_update_while_one_runs_waits(tmp_path):
    held = _Held()
    updates, said, _clock, _calls = _updates(tmp_path, runner=held)
    updates.install(["toastush"])
    updates.install(["toastush"])
    updates.install(["toastush"], quiet=True)  # a pack's idle update: not a word
    assert said == [
        "Checking for soundpack updates.",
        "Soundpack updates are already being installed.",
    ]
    held.finish()
    updates.install(["toastush"])  # finished: a new one can start
    assert said[-1] == "Checking for soundpack updates."


def test_update_check_says_what_each_pack_has(tmp_path):
    answers = [PendingUpdate("toastush", "Toastush", 1), None]

    def check(_store, _pack_id, _source):
        return answers.pop(0)

    updates, said, _clock, _calls = _updates(tmp_path, check=check)
    updates.check_now(["toastush"])
    updates.check_now(["toastush"])  # asked again at once: no once-a-day limit
    assert said == [
        "Checking for soundpack updates.",
        "Toastush has an update: 1 file. Type /update to install it.",
        "Checking for soundpack updates.",
        "Toastush is up to date.",
    ]


def test_update_check_says_when_the_host_is_down(tmp_path):
    def unreachable(_store, _pack_id, _source):
        raise PackError("couldn't check Toastush for updates: the host is busy")

    updates, said, _clock, _calls = _updates(tmp_path, check=unreachable)
    updates.check_now(["toastush"])
    assert said[-1] == "couldn't check Toastush for updates: the host is busy"


def test_an_install_hands_the_packs_what_it_did(tmp_path):
    told = []
    updates, _said, _clock, _calls = _updates(
        tmp_path, installed=lambda results, quiet: told.append((results, quiet)),
    )
    updates.install(["toastush"], quiet=True)
    [(results, quiet)] = told
    assert quiet is True
    assert results["toastush"].written == 34


def test_a_check_that_began_before_an_install_keeps_quiet(tmp_path):
    # At login the daily check starts, then Miriani-Next sends "update quietly": the check's
    # "type /update" would arrive while that update is installing, or after it.
    held = _Held()
    updates, said, _clock, _calls = _updates(tmp_path, runner=held)
    updates.check_due(["toastush"])
    updates.install(["toastush"], quiet=True)
    held.finish(0)  # the check
    assert said == []
    held.finish(0)  # the install
    assert said == ["Updated Toastush: 34 files updated. Reconnect to apply."]


def test_no_check_starts_while_an_install_runs(tmp_path):
    held = _Held()
    updates, _said, _clock, calls = _updates(tmp_path, runner=held)
    updates.install(["toastush"])
    updates.check_due(["toastush"])
    assert len(held.jobs) == 1
    held.finish()
    assert calls == []


class _Recorder:
    def __init__(self) -> None:
        self.checked: list[list[str]] = []
        self.installed: list[list[str]] = []

    def check_due(self, pack_ids):
        self.checked.append(list(pack_ids))

    def install(self, pack_ids, *, quiet=False):
        self.installed.append(list(pack_ids))


def test_connecting_checks_the_worlds_packs_and_slash_update_installs_them(tmp_path):
    store = PackStore(tmp_path / "store")
    source = tmp_path / "toastush.set"
    source.write_text("#trigger {x} {look}", encoding="utf-8")
    manifest = store.install(source, world="mud", trust=True)
    app = EngineApp(VoiceRouter(RecordingBackend(), clock=lambda: 0.0), packs=store, name="mud")
    recorder = _Recorder()
    app.pack_updates = recorder
    app.on_connect("mud")
    assert recorder.checked == [[manifest.id]]
    app.on_ws_message({"type": "input", "text": "/update"})
    assert recorder.installed == [[manifest.id]]


def test_typing_update_in_miriani_next_installs_and_the_pack_hears_it(tmp_path):
    # The whole way round: the pack's own update command, genericMud's updater, then the
    # broadcast Miriani-Next's updater sent when it finished (its theme and changelog).
    source = tmp_path / "miriani-next"
    plugins = source / "worlds" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "updater.xml").write_text(
        '<muclient><plugin id="706cd88f4d441a81439bd421" name="updater" language="Lua"/>'
        "</muclient>",
        encoding="latin-1",
    )
    (plugins / "listener.xml").write_text(
        '<muclient><plugin id="aaaaaaaaaaaaaaaaaaaaaaaa" name="listener" language="Lua"/>'
        "<script><![CDATA[\n"
        "function OnPluginBroadcast(msg, id, name, text)\n"
        '  SetVariable("heard", msg .. "|" .. id .. "|" .. text)\n'
        "end\n"
        "]]></script></muclient>",
        encoding="latin-1",
    )
    (source / "worlds" / "Miriani.MCL").write_text(
        '<muclient><world site="x" port="1" name="Miriani"/>'
        '<include name="updater.xml" plugin="y"/><include name="listener.xml" plugin="y"/>'
        "</muclient>",
        encoding="latin-1",
    )
    store = PackStore(tmp_path / "store")
    store.install(
        source, world="miriani", trust=True, entry="worlds/Miriani.MCL", pack_id="miriani-next",
    )
    app = EngineApp(VoiceRouter(RecordingBackend(), clock=lambda: 0.0), packs=store, name="miriani")
    said: list[str] = []
    app.pack_updates = PackUpdates(
        store, announce=said.append, run_in_background=_right_away,
        check=lambda *_args: None,
        update=lambda *_args, **_kw: GitUpdateResult(
            "Miriani-Next", False, written=1, written_files=["sounds/a.ogg"],
        ),
        source_for=lambda pack_id: SOURCE if pack_id == "miriani-next" else None,
        installed=app._packs_updated,
    )
    app.on_connect("miriani")
    app.on_ws_message({"type": "input", "text": "update"})
    assert said == [
        "Checking for soundpack updates.",
        "Updated Miriani-Next: 1 file updated. Reconnect to apply.",
    ]
    message, plugin_id, text = app._mush_packs[0]._api.get_var("heard").split("|", 2)
    assert (message, plugin_id) == ("777", "706cd88f4d441a81439bd421")
    assert json.loads(text)["files"] == ["sounds/a.ogg"]
