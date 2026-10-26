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
updater does.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
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

# Kept on a pack's first update even when they differ from the repository: they're what a
# player or MUSHclient itself changes. Miriani-Next's updater never overwrites these either.
_PROTECTED_PATTERNS = (
    "*.mcl", "*.ini", "*.cfg", "*.conf", "*.config", "*.db", "*.sqlite", "*.sqlite3",
    "*/state/*", "*/logs/*", "*/settings/*", "logs/*", "settings/*",
)


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
    """The baseline the last update left, or None when the pack has never been updated
    this way (or the file isn't one genericMud wrote)."""
    path = Path(pack_dir) / BASELINE_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if not lines or lines[0] != _BASELINE_HEADER:
        return None
    baseline = Baseline()
    for line in lines[1:]:
        if line.startswith("# "):
            key, _, value = line[2:].partition(" ")
            if key in ("repo", "commit", "prefix"):
                setattr(baseline, key, value)
            continue
        # "<sha> <size> <mtime_ns> <owner> <path>": the path is the rest, spaces and all.
        parts = line.split(" ", 4)
        if len(parts) != 5 or not parts[1].isdigit() or not parts[2].isdigit():
            continue
        sha, size, mtime_ns, owner, repo_path = parts
        if owner not in (_WRITTEN, _KEPT):
            continue
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
        entry = baseline.entries[repo_path]
        lines.append(f"{entry.sha} {entry.size} {entry.mtime_ns} {entry.owner} {repo_path}")
    path = Path(pack_dir) / BASELINE_NAME
    temporary = path.with_name(path.name + ".part")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def entry_for(path: Path, sha: str, owner: str = _WRITTEN) -> BaselineEntry:
    """A baseline entry for a file just written or checked to be blob ``sha``."""
    status = path.stat()
    return BaselineEntry(sha, status.st_size, status.st_mtime_ns, owner)


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
    plan = Plan()

    # Every file the repository has: right already, missing, out of date, or the player's.
    for repo_path, sha in remote.items():
        local = safepath.confine(pack_dir, prefix + repo_path)
        if local is None:
            continue  # a path that would land outside the pack is never written or counted
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

    @property
    def complete(self) -> bool:
        return not self.missing and not self.in_use


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
    leaves every file either old or new, never half-written. The baseline is saved only when
    every blob arrived, so an update that stopped short simply runs again.
    """
    pack_dir = Path(pack_dir)
    applied = Applied()

    # The pack's files: written in place, or beside the player's copy as .new.
    for repo_path, sha in plan.download.items():
        outcome = _place(pack_dir, prefix + repo_path, blobs.get(sha))
        _record(applied, applied.written, repo_path, outcome)
    for repo_path, sha in plan.modified.items():
        outcome = _place(pack_dir, prefix + repo_path + NEW_SUFFIX, blobs.get(sha))
        _record(applied, applied.new_versions, repo_path, outcome)

    # Files the repository dropped that are still as the updater left them.
    for repo_path in plan.remove:
        local = safepath.confine(pack_dir, prefix + repo_path)
        if local is None or not local.is_file():
            continue
        try:
            local.unlink()
        except OSError:
            applied.in_use.append(repo_path)
            continue
        applied.removed.append(repo_path)

    if not applied.complete:
        return applied

    # The new baseline: what's now the pack's, and what's the player's.
    baseline = Baseline(repo=repo, commit=commit, prefix=prefix)
    for repo_path, sha in remote.items():
        local = safepath.confine(pack_dir, prefix + repo_path)
        if local is None or not local.is_file():
            continue
        if repo_path in plan.modified or repo_path in plan.held:
            baseline.entries[repo_path] = entry_for(local, sha, _KEPT)
        else:
            baseline.entries[repo_path] = entry_for(local, sha)
    write_baseline(pack_dir, baseline)
    return applied


_PLACED = "placed"
_NO_BLOB = "missing"
_IN_USE = "in use"


def _place(pack_dir: Path, relative: str, blob: Path | None) -> str:
    """Put ``blob`` at ``relative`` inside the pack, replacing what's there in one step.

    On Windows a file something has open (a sound or the music playing from it) can't be
    replaced; it's left as it is and reported, and the next update tries it again.
    """
    if blob is None:
        return _NO_BLOB
    target = safepath.confine(pack_dir, relative)
    if target is None:
        return _NO_BLOB
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    shutil.copyfile(blob, temporary)
    try:
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        return _IN_USE
    return _PLACED


def _record(applied: Applied, done: list[str], repo_path: str, outcome: str) -> None:
    if outcome == _PLACED:
        done.append(repo_path)
    elif outcome == _IN_USE:
        applied.in_use.append(repo_path)
    else:
        applied.missing.append(repo_path)
