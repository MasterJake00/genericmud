"""Keep an installed soundpack in step with the git repository it comes from.

Several packs live in public git repositories (Erion on gitlab.com, Chatpack on ChatMUD's own
GitLab, Miriani-Next, Mystavaria and Toastush on GitHub). Updating one used to mean downloading
the whole repository again, about a gigabyte for Erion. This module works out what actually
changed and fetches only that (the fetching is :mod:`genericmud.packs.git_remote`).

It compares files by their git blob hash, the same hash the repository's file list carries, so
a file that is already right is never downloaded, and a download is checked against its hash
before it replaces anything. A local copy that differs only by Windows line endings counts as
matching, the way git's autocrlf checkout makes it: Mystavaria's install differs from its
repository in 5,173 files that way and in no other.

Files the player changed are theirs, as package managers treat changed config files (Debian
keeps yours and saves the new one as .dpkg-dist): the player's copy stays, the pack's new
version is written next to it with ".new" on the end, and the update reports it. Files the pack
never had (its databases, the player's settings) are never touched, and only files the updater
wrote itself are ever deleted.

What the updater wrote is kept in a baseline file in the pack folder, with each file's size and
modification time, so the next update can tell a file the player changed from one the pack
changed, and needn't hash a gigabyte to see that nothing moved. The first update of a pack has
no baseline, so it can't tell those apart; it keeps the files players commonly change (world
files, MUSHclient's settings, plugin state, logs) and refreshes the rest, as Miriani-Next's own
updater does. A baseline that's there but damaged stops the update rather than being taken for
a first one.

A download can take a while, and the player may change a file meanwhile: every file is looked
at again just before it's replaced or deleted, and one that changed since the plan was made is
the player's.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from genericmud import safepath

BASELINE_NAME = ".gm_git.lst"  # what the updater wrote, in the pack folder
NEW_SUFFIX = ".new"  # the pack's new version of a file the player changed
_BASELINE_HEADER = "# genericMud git sync baseline v1"
_READ_CHUNK = 1 << 20  # hash files a megabyte at a time
_WRITTEN = "w"  # the updater wrote or confirmed this file
_UPDATER_FILES = (".gm_git", ".gm-update-")  # its baseline, check marker and scratch folder
_KEPT = "k"  # the player's own copy, kept over the pack's
_SCRATCH_PREFIX = ".gm-update-"  # a file being written, before it's moved into place
# Characters that end a line somewhere (str.splitlines splits on all of them): a path with one
# can't be a line of the baseline.
_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")

# Kept on a pack's first update even when they differ from the repository: they're what a
# player or MUSHclient itself changes. Miriani-Next's updater never overwrites these either.
_PROTECTED_PATTERNS = (
    "*.mcl", "*.ini", "*.cfg", "*.conf", "*.config", "*.db", "*.sqlite", "*.sqlite3",
    "*/state/*", "*/logs/*", "*/settings/*", "logs/*", "settings/*",
)


class BaselineDamaged(Exception):
    """The baseline file is there but isn't one genericMud wrote, or not all of it."""


@dataclass(frozen=True)
class BaselineEntry:
    """One file as the last update left it: the pack's blob hash for it, the size and
    modification time the file had then, and whose copy it is."""

    sha: str
    size: int
    mtime_ns: int
    owner: str = _WRITTEN

    @property
    def kept(self) -> bool:
        return self.owner == _KEPT


@dataclass
class Baseline:
    """The pack's state after its last update."""

    repo: str = ""
    commit: str = ""
    prefix: str = ""  # where the repository's root sits inside the pack folder
    entries: dict[str, BaselineEntry] = field(default_factory=dict)  # by repository path


@dataclass
class Plan:
    """What an update would do, path by path (repository paths)."""

    unchanged: list[str] = field(default_factory=list)
    download: dict[str, str] = field(default_factory=dict)  # path -> blob hash to write
    modified: dict[str, str] = field(default_factory=dict)  # player's copy stays, write .new
    held: dict[str, str] = field(default_factory=dict)  # player's copy stays, pack unchanged
    remove: list[str] = field(default_factory=list)  # gone upstream, unchanged here
    kept: list[str] = field(default_factory=list)  # gone upstream, but changed here
    local_only: int = 0  # files the repository doesn't have and the updater never wrote
    # How each file the plan writes over, deletes or vouches for looked when it was planned:
    # (size, modification time), or None when it wasn't there. Checked again just before.
    seen: dict[str, tuple[int, int] | None] = field(default_factory=dict)
    previous: dict[str, BaselineEntry] = field(default_factory=dict)  # the baseline it began from

    @property
    def up_to_date(self) -> bool:
        return not self.download and not self.modified and not self.remove

    @property
    def blobs(self) -> set[str]:
        """Every blob the update needs to fetch."""
        return set(self.download.values()) | set(self.modified.values())


def blob_sha(data: bytes) -> str:
    """Git's hash of a file's contents: SHA-1 of "blob <size>\\0" and the bytes."""
    digest = hashlib.sha1()
    digest.update(b"blob %d\0" % len(data))
    digest.update(data)
    return digest.hexdigest()


def file_blob_sha(path: Path) -> str:
    """Git's hash of a file on disk, read a megabyte at a time."""
    size = path.stat().st_size
    digest = hashlib.sha1()
    digest.update(b"blob %d\0" % size)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def matches_blob(path: Path, sha: str) -> bool:
    """Whether the file on disk is the blob ``sha``, as checked out on any system.

    A Windows checkout with autocrlf turns the repository's LF line endings into CRLF, so a
    file that misses is hashed once more with them turned back. Only text with a CRLF in it
    gets the second pass.
    """
    if file_blob_sha(path) == sha:
        return True
    data = path.read_bytes()
    if b"\r\n" not in data:
        return False
    return blob_sha(data.replace(b"\r\n", b"\n")) == sha


def read_baseline(pack_dir: Path) -> Baseline | None:
    """The baseline the last update left, or None when the pack has never been updated this
    way. One that's there but unreadable raises BaselineDamaged: without it the player's
    changes can't be told from the pack's, so an update mustn't guess."""
    path = Path(pack_dir) / BASELINE_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise BaselineDamaged(f"it can't be read ({error})") from error
    lines = text.split("\n")  # only "\n": a path may hold any other character
    if lines[-1] == "":
        lines.pop()
    if not lines or lines[0] != _BASELINE_HEADER:
        raise BaselineDamaged("it doesn't begin the way genericMud writes it")
    baseline = Baseline()
    for number, line in enumerate(lines[1:], start=2):
        if line.startswith("# "):
            key, _, value = line[2:].partition(" ")
            if key in ("repo", "commit", "prefix"):
                setattr(baseline, key, value)
            continue
        # "<sha> <size> <mtime_ns> <owner> <path>": the path is the rest, spaces and all.
        parts = line.split(" ", 4)
        if (
            len(parts) != 5 or not parts[1].isdigit() or not parts[2].isdigit()
            or parts[3] not in (_WRITTEN, _KEPT)
        ):
            raise BaselineDamaged(f"line {number} isn't one genericMud writes")
        sha, size, mtime_ns, owner, repo_path = parts
        baseline.entries[repo_path] = BaselineEntry(sha, int(size), int(mtime_ns), owner)
    return baseline


def write_baseline(pack_dir: Path, baseline: Baseline) -> None:
    """Save the baseline, replacing the old one in one step."""
    lines = [
        _BASELINE_HEADER,
        f"# repo {baseline.repo}",
        f"# commit {baseline.commit}",
        f"# prefix {baseline.prefix}",
    ]
    for repo_path in sorted(baseline.entries):
        if _LINE_BREAKS.intersection(repo_path):
            continue  # it would add lines of its own; the file list never lets one through
        entry = baseline.entries[repo_path]
        lines.append(f"{entry.sha} {entry.size} {entry.mtime_ns} {entry.owner} {repo_path}")
    _write_new_file(Path(pack_dir) / BASELINE_NAME, ("\n".join(lines) + "\n").encode("utf-8"))


def _write_new_file(target: Path, data: bytes) -> None:
    """Write ``data`` to a freshly created file beside ``target`` and move it into place.

    The scratch file gets a new, unguessable name and is created exclusively, so nothing that
    was already there (a link out of the pack, a file of the player's) is ever written through.
    """
    handle, scratch = tempfile.mkstemp(prefix=_SCRATCH_PREFIX, suffix=".part", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(scratch, target)
    except BaseException:
        Path(scratch).unlink(missing_ok=True)
        raise


def entry_for(path: Path, sha: str, owner: str = _WRITTEN) -> BaselineEntry:
    """A baseline entry for a file just written or checked to be blob ``sha``."""
    status = path.stat()
    return BaselineEntry(sha, status.st_size, status.st_mtime_ns, owner)


def _snapshot(path: Path) -> tuple[int, int] | None:
    """A file's size and modification time, or None when there's no file there."""
    try:
        status = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return status.st_size, status.st_mtime_ns


def _inside(pack_dir: Path, relative: str) -> Path | None:
    """``relative`` in the pack folder, or None when it would land outside it.

    The path goes through the folder rather than to wherever its links resolve, so a link in
    the pack is itself replaced or deleted, never the file it points at.
    """
    if safepath.confine(pack_dir, relative) is None:
        return None
    return Path(pack_dir) / relative.replace("\\", "/")


def _still_as_recorded(path: Path, entry: BaselineEntry) -> bool:
    """Whether the file looks untouched since the baseline recorded it: same size and same
    modification time. Cheap, so an update needn't hash every file; a file that fails it is
    hashed before anything is decided."""
    try:
        status = path.stat()
    except OSError:
        return False
    return status.st_size == entry.size and status.st_mtime_ns == entry.mtime_ns


def _local_matches(path: Path, sha: str, recorded: BaselineEntry | None) -> bool:
    """Whether the file is blob ``sha``, trusting an untouched file the updater recorded."""
    trusted = recorded is not None and not recorded.kept and recorded.sha == sha
    if trusted and _still_as_recorded(path, recorded):
        return True
    return matches_blob(path, sha)


def _protected(repo_path: str) -> bool:
    lowered = repo_path.lower()
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in _PROTECTED_PATTERNS)


def make_plan(
    remote: dict[str, str], pack_dir: Path, prefix: str, baseline: Baseline | None
) -> Plan:
    """Compare the repository's files (``remote``: path -> blob hash) with the pack folder.

    ``prefix`` is where the repository's root sits in the folder (Toastush was installed from
    an archive that kept its "PsudoDeSudo-Toastush-e8cdb7d/" folder).
    """
    pack_dir = Path(pack_dir)
    entries = baseline.entries if baseline is not None else {}
    plan = Plan(previous=dict(entries))

    # Every file the repository has: right already, missing, out of date, or the player's.
    for repo_path, sha in remote.items():
        local = safepath.confine(pack_dir, prefix + repo_path)
        if local is None:
            continue  # a path that would land outside the pack is never written or counted
        plan.seen[repo_path] = _snapshot(_inside(pack_dir, prefix + repo_path))
        if not local.is_file():
            plan.download[repo_path] = sha
            continue
        recorded = entries.get(repo_path)
        if _local_matches(local, sha, recorded):
            plan.unchanged.append(repo_path)
        elif recorded is not None and recorded.kept:
            # The player's own copy: a new .new only when the pack's version moved on.
            if recorded.sha == sha:
                plan.held[repo_path] = sha
            else:
                plan.modified[repo_path] = sha
        elif recorded is not None and _local_matches(local, recorded.sha, recorded):
            plan.download[repo_path] = sha  # the updater's own copy, now out of date
        elif recorded is None and baseline is None and not _protected(repo_path):
            plan.download[repo_path] = sha  # first update: an old copy, as far as we can tell
        else:
            plan.modified[repo_path] = sha  # changed here: theirs stays

    # Files the updater looked after that the repository no longer has.
    for repo_path, recorded in entries.items():
        if repo_path in remote:
            continue
        local = safepath.confine(pack_dir, prefix + repo_path)
        if local is None or not local.is_file():
            continue
        if not recorded.kept and _local_matches(local, recorded.sha, recorded):
            plan.remove.append(repo_path)
            plan.seen[repo_path] = _snapshot(_inside(pack_dir, prefix + repo_path))
        else:
            plan.kept.append(repo_path)

    # And everything else in the folder, which an update never touches.
    root = safepath.confine(pack_dir, prefix) if prefix else pack_dir
    if root is not None and root.is_dir():
        known = set(remote) | set(entries)
        for path in root.rglob("*"):
            if not path.is_file() or path.name.startswith(_UPDATER_FILES):
                continue
            repo_path = path.relative_to(root).as_posix()
            if repo_path not in known and not repo_path.endswith(NEW_SUFFIX):
                plan.local_only += 1
    return plan


@dataclass
class Applied:
    """What an update did."""

    written: list[str] = field(default_factory=list)
    new_versions: list[str] = field(default_factory=list)  # player's files with a .new beside
    removed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # blobs that never arrived
    in_use: list[str] = field(default_factory=list)  # open elsewhere (a sound playing), left as is
    failed: list[str] = field(default_factory=list)  # couldn't be written; tried again next time
    # A folder where the file goes, or a file where one of its folders goes: no retry fixes it.
    blocked: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # dropped upstream, but changed meanwhile

    @property
    def complete(self) -> bool:
        return not self.missing and not self.in_use and not self.failed


def apply_plan(
    pack_dir: Path,
    prefix: str,
    plan: Plan,
    blobs: dict[str, Path],
    *,
    repo: str,
    commit: str,
    remote: dict[str, str],
) -> Applied:
    """Carry out ``plan`` with the fetched ``blobs`` (hash -> file), then save the baseline.

    Each file is written to a temporary name and moved into place, so an interrupted update
    leaves every file either old or new, never half-written. A file that changed since the plan
    was made is the player's: it stays, and the pack's version goes beside it as .new.

    The baseline is saved even when the update stops short (a blob that never arrived, a file
    in use), so the retry still knows which files are the player's. A first update has nothing
    else to go on: without it, the retry would take a file kept as the player's for an old copy
    and replace it. Until everything is written the baseline is saved without the commit, so
    it's never taken for a finished update.
    """
    pack_dir = Path(pack_dir)
    applied = Applied()
    entries: dict[str, BaselineEntry] = {}  # the new baseline, filled in as files land
    players: set[str] = set(plan.modified) | set(plan.held)

    # The pack's files: written in place, or beside the player's copy as .new.
    for repo_path, sha in plan.download.items():
        target = _inside(pack_dir, prefix + repo_path)
        if target is not None and _snapshot(target) != plan.seen.get(repo_path):
            players.add(repo_path)  # the player changed it while the update downloaded
            outcome = _place(pack_dir, prefix + repo_path + NEW_SUFFIX, blobs.get(sha))
            _record(applied, applied.new_versions, repo_path, outcome)
            continue
        outcome = _place(pack_dir, prefix + repo_path, blobs.get(sha))
        _record(applied, applied.written, repo_path, outcome)
        if outcome == _PLACED:
            entries[repo_path] = entry_for(target, sha)
    for repo_path, sha in plan.modified.items():
        outcome = _place(pack_dir, prefix + repo_path + NEW_SUFFIX, blobs.get(sha))
        _record(applied, applied.new_versions, repo_path, outcome)

    # Files the repository dropped that are still as the updater left them.
    for repo_path in plan.remove:
        target = _inside(pack_dir, prefix + repo_path)
        if target is None or _snapshot(target) is None:
            continue
        if _snapshot(target) != plan.seen.get(repo_path):
            applied.kept.append(repo_path)  # changed while the update ran: the player's now
            continue
        try:
            target.unlink()
        except OSError:
            applied.in_use.append(repo_path)
            continue
        applied.removed.append(repo_path)

    # The rest of the baseline: the player's files, the pack's that were right already, and
    # the pack's old copies still to be replaced.
    unfinished = set(applied.missing) | set(applied.in_use) | set(applied.failed)
    baseline = Baseline(repo=repo, commit=commit if applied.complete else "", prefix=prefix)
    for repo_path, sha in remote.items():
        target = _inside(pack_dir, prefix + repo_path)
        if target is None or _snapshot(target) is None:
            continue
        unchanged_since_plan = _snapshot(target) == plan.seen.get(repo_path)
        previous = plan.previous.get(repo_path)
        if repo_path in entries:
            baseline.entries[repo_path] = entries[repo_path]
        elif repo_path in players:
            baseline.entries[repo_path] = entry_for(target, sha, _KEPT)
        elif repo_path in unfinished and unchanged_since_plan:
            if previous is not None and not previous.kept:
                baseline.entries[repo_path] = previous  # the pack's old copy, as recorded
            else:
                # A first update took it for an old copy of the pack's: still to be replaced.
                baseline.entries[repo_path] = entry_for(target, file_blob_sha(target))
        elif repo_path not in unfinished and (unchanged_since_plan or matches_blob(target, sha)):
            baseline.entries[repo_path] = entry_for(target, sha)
        else:
            # Changed since the plan was made: the player's, not the pack's.
            baseline.entries[repo_path] = entry_for(target, sha, _KEPT)
    # Dropped files that couldn't be deleted this time are deleted next time.
    for repo_path in plan.remove:
        if repo_path in unfinished and repo_path in plan.previous:
            baseline.entries[repo_path] = plan.previous[repo_path]
    write_baseline(pack_dir, baseline)
    return applied


_PLACED = "placed"
_NO_BLOB = "missing"
_IN_USE = "in use"
_FAILED = "failed"
_BLOCKED = "blocked"


def _place(pack_dir: Path, relative: str, blob: Path | None) -> str:
    """Put ``blob`` at ``relative`` inside the pack, replacing what's there in one step.

    On Windows a file something has open (a sound or the music playing from it) can't be
    replaced; it's left as it is and reported, and the next update tries it again.
    """
    if blob is None:
        return _NO_BLOB
    target = _inside(pack_dir, relative)
    if target is None:
        return _BLOCKED
    if target.is_dir() and not target.is_symlink():
        return _BLOCKED  # a folder of the same name is where the file goes
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _BLOCKED  # a file is where one of its folders goes
    try:
        handle, scratch = tempfile.mkstemp(
            prefix=_SCRATCH_PREFIX, suffix=".part", dir=target.parent
        )
    except OSError:
        return _FAILED
    try:
        with os.fdopen(handle, "wb") as stream, blob.open("rb") as source:
            shutil.copyfileobj(source, stream, _READ_CHUNK)
    except OSError:
        Path(scratch).unlink(missing_ok=True)
        return _FAILED
    try:
        os.replace(scratch, target)
    except OSError:
        Path(scratch).unlink(missing_ok=True)
        return _IN_USE
    return _PLACED


def _record(applied: Applied, done: list[str], repo_path: str, outcome: str) -> None:
    lists = {
        _PLACED: done, _IN_USE: applied.in_use, _FAILED: applied.failed,
        _BLOCKED: applied.blocked, _NO_BLOB: applied.missing,
    }
    lists[outcome].append(repo_path)
