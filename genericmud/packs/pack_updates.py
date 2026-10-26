"""Checking a world's soundpacks for updates when it connects, and installing them on request.

When a world connects, each of its packs that comes from a git repository is checked at most
once a day, in the background. A pack with something new says so, and the player installs it
with /update, the way Miriani-Next's own updater asks them to "type update". Nothing installs
by itself: genericMud's own updates work the same way, check and then ask. A pack that runs
its own update command (Miriani-Next's "update quietly", when its player turned automatic
updates on) installs without the announcement first.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from genericmud.packs import git_sources
from genericmud.packs.setup import (
    GitUpdateResult,
    PendingUpdate,
    check_pack_update,
    describe_update,
    update_pack_from_git,
)
from genericmud.packs.store import PackError, PackStore

CHECK_INTERVAL_SECONDS = 24 * 60 * 60  # each pack's repository is asked at most once a day
CHECKED_NAME = ".gm_git.checked"  # in the pack folder: when it was last checked

# How a job runs off the app's loop: run_in_background(job, done) calls job() somewhere else and
# then done(result or the exception it raised) back on the loop.
Runner = Callable[[Callable[[], object], Callable[[object], None]], None]
# Told what an install did, pack by pack, once it's been announced: (results, quiet).
Installed = Callable[[dict[str, GitUpdateResult], bool], None]


def run_in_thread(job: Callable[[], object], done: Callable[[object], None]) -> None:
    """Run ``job`` on a daemon thread and hand its outcome to ``done`` on the calling loop.

    Without a running loop (a headless test or tool driving the app by hand) there's nowhere
    to report to, so nothing runs: update checks belong to a session that's actually up.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    def run() -> None:
        try:
            outcome = job()
        except Exception as error:  # noqa: BLE001 - handed over like a result
            outcome = error
        loop.call_soon_threadsafe(done, outcome)

    threading.Thread(target=run, name="pack-update", daemon=True).start()


class PackUpdates:
    """A session's pack update checks and installs."""

    def __init__(
        self,
        store: PackStore,
        *,
        announce: Callable[[str], None],
        run_in_background: Runner = run_in_thread,
        check=check_pack_update,
        update=update_pack_from_git,
        source_for=git_sources.update_source,
        clock: Callable[[], float] = time.time,
        installed: Installed | None = None,
        diag=None,
    ) -> None:
        self._store = store
        self._announce = announce
        self._run = run_in_background
        self._check = check
        self._update = update
        self._source_for = source_for
        self._clock = clock
        self._installed = installed
        self._diag = diag
        self._busy = False  # an install running; a second /update waits for it
        # Counts installs started. A check that began before the latest install keeps quiet:
        # the install says what it found, and "type /update" would be out of date.
        self._installs_started = 0

    def check_due(self, pack_ids: Iterable[str]) -> None:
        """Check the packs whose last check was over a day ago, in the background, and
        announce any that have an update waiting."""
        if self._busy:
            return  # an install is fetching what a check would find
        due = [
            (pack_id, source) for pack_id, source in self._targets(pack_ids) if self._due(pack_id)
        ]
        if not due:
            return
        installs_before = self._installs_started

        def job() -> list[PendingUpdate]:
            waiting = []
            for pack_id, source in due:
                try:
                    pending = self._check(self._store, pack_id, source)
                except PackError as error:
                    self._trace("pack.update_check", id=pack_id, result="failed", error=str(error))
                    continue  # a host that's down is asked again tomorrow, quietly
                self._mark_checked(pack_id)
                self._trace(
                    "pack.update_check", id=pack_id,
                    result="update" if pending else "up to date",
                )
                if pending is not None:
                    waiting.append(pending)
            return waiting

        def report(outcome: object) -> None:
            if self._installs_started == installs_before:
                self._report_waiting(outcome)

        self._run(job, report)

    def check_now(self, pack_ids: Iterable[str]) -> None:
        """Ask the packs' repositories now, whenever they were last asked, and say what each
        has: a pack's own "update check" command."""
        targets = self._targets(pack_ids)
        if not targets:
            self._announce("None of this world's soundpacks can be updated from here.")
            return
        if self._busy:
            self._announce("Soundpack updates are already being installed.")
            return
        self._announce("Checking for soundpack updates.")
        installs_before = self._installs_started

        def job() -> list[str]:
            reports = []
            for pack_id, source in targets:
                try:
                    pending = self._check(self._store, pack_id, source)
                except PackError as error:
                    reports.append(str(error))
                    continue
                self._mark_checked(pack_id)
                if pending is None:
                    reports.append(f"{source.name} is up to date.")
                else:
                    reports.append(_waiting_line(pending))
            return reports

        def report(outcome: object) -> None:
            if self._installs_started != installs_before:
                return  # an install began meanwhile and reports for itself
            if isinstance(outcome, Exception):
                self._announce(f"Checking for soundpack updates failed: {outcome}")
                return
            for line in outcome:
                self._announce(line)

        self._run(job, report)

    def install(self, pack_ids: Iterable[str], *, quiet: bool = False) -> None:
        """Install whatever the packs' repositories have new: the /update command, or a pack's
        own update command. ``quiet`` leaves out the "checking" line."""
        targets = self._targets(pack_ids)
        if not targets:
            if not quiet:
                self._announce("None of this world's soundpacks can be updated from here.")
            return
        if self._busy:
            if not quiet:
                self._announce("Soundpack updates are already being installed.")
            return
        self._busy = True
        self._installs_started += 1
        if not quiet:
            self._announce("Checking for soundpack updates.")

        def job() -> tuple[list[str], dict[str, GitUpdateResult]]:
            reports = []
            results = {}
            for pack_id, source in targets:
                try:
                    result = self._update(self._store, pack_id, source, diag=self._diag)
                except PackError as error:
                    reports.append(str(error))
                    continue
                self._mark_checked(pack_id)
                results[pack_id] = result
                if quiet and result.up_to_date and not result.in_use:
                    continue  # asked for quietly, and nothing changed: nothing to say
                reports.append(describe_update(result))
            return reports, results

        self._run(job, lambda outcome: self._report_installed(outcome, quiet))

    def _targets(self, pack_ids: Iterable[str]) -> list[tuple[str, git_sources.UpdateSource]]:
        """The packs that come from a repository genericMud can update them from."""
        return [
            (pack_id, source) for pack_id in pack_ids
            if (source := self._source_for(pack_id)) is not None
        ]

    def _report_waiting(self, outcome: object) -> None:
        if isinstance(outcome, Exception):
            self._trace("pack.update_check", result="error", error=repr(outcome))
            return
        for pending in outcome:
            self._announce(_waiting_line(pending))

    def _report_installed(self, outcome: object, quiet: bool) -> None:
        self._busy = False
        if isinstance(outcome, Exception):
            self._announce(f"Updating soundpacks failed: {outcome}")
            return
        reports, results = outcome
        for report in reports:
            self._announce(report)
        if self._installed is not None and results:
            self._installed(results, quiet)

    def _due(self, pack_id: str) -> bool:
        marker = self._marker(pack_id)
        try:
            checked = float(marker.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return True
        return self._clock() - checked >= CHECK_INTERVAL_SECONDS

    def _mark_checked(self, pack_id: str) -> None:
        try:
            self._marker(pack_id).write_text(f"{self._clock():.0f}\n", encoding="utf-8")
        except OSError:
            pass  # unwritable: it's checked again next connect, no harm done

    def _marker(self, pack_id: str) -> Path:
        return self._store.pack_dir(pack_id) / CHECKED_NAME

    def _trace(self, event: str, **fields: object) -> None:
        if self._diag is not None:
            self._diag.event(event, **fields)


def _waiting_line(pending: PendingUpdate) -> str:
    files = f"{pending.files} {'file' if pending.files == 1 else 'files'}"
    return f"{pending.name} has an update: {files}. Type /update to install it."
