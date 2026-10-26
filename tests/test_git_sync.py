"""Working out what a soundpack update from its git repository has to change."""

from __future__ import annotations

import os
from pathlib import Path

from genericmud.packs import git_sync
from genericmud.packs.git_sync import Baseline, blob_sha, make_plan


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_blob_sha_is_gits_own_hash():
    # `printf 'hello\n' | git hash-object --stdin`
    assert blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_a_windows_checkout_matches_the_repositorys_blob(tmp_path):
    # Mystavaria's install differs from its repository only by CRLF line endings.
    path = _write(tmp_path / "script.lua", b"Note('a')\r\nNote('b')\r\n")
    assert git_sync.matches_blob(path, blob_sha(b"Note('a')\nNote('b')\n"))
    assert not git_sync.matches_blob(path, blob_sha(b"Note('a')\nNote('c')\n"))


def test_the_baseline_round_trips_and_ignores_files_it_did_not_write(tmp_path):
    baseline = Baseline(repo="https://example.com/pack", commit="abc", prefix="wrapped/")
    baseline.entries["sounds/a b.ogg"] = git_sync.BaselineEntry("1" * 40, 12, 345)
    git_sync.write_baseline(tmp_path, baseline)
    assert git_sync.read_baseline(tmp_path) == baseline
    (tmp_path / git_sync.BASELINE_NAME).write_text("something else\n", encoding="utf-8")
    assert git_sync.read_baseline(tmp_path) is None


def test_a_first_update_downloads_what_differs_and_leaves_the_rest(tmp_path):
    _write(tmp_path / "same.lua", b"same\n")
    _write(tmp_path / "crlf.lua", b"one\r\ntwo\r\n")
    _write(tmp_path / "old.lua", b"old\n")
    _write(tmp_path / "player.db", b"the pack's own data")
    remote = {
        "same.lua": blob_sha(b"same\n"),
        "crlf.lua": blob_sha(b"one\ntwo\n"),
        "old.lua": blob_sha(b"new\n"),
        "added.ogg": blob_sha(b"sound"),
        "../outside.lua": blob_sha(b"evil"),
    }
    plan = make_plan(remote, tmp_path, "", None)
    assert sorted(plan.unchanged) == ["crlf.lua", "same.lua"]
    assert plan.download == {"old.lua": remote["old.lua"], "added.ogg": remote["added.ogg"]}
    assert plan.modified == {}
    assert plan.remove == []
    assert plan.local_only == 1  # player.db, never touched


def _edit(path: Path, data: bytes) -> None:
    """The player changes a file after the updater wrote it: new bytes, later mtime."""
    before = path.stat().st_mtime_ns
    path.write_bytes(data)
    os.utime(path, ns=(before + 10**9, before + 10**9))


def test_with_a_baseline_a_players_change_is_told_apart_from_the_packs(tmp_path):
    baseline = Baseline(prefix="pack/")
    for name, data in (
        ("ours.lua", b"v1\n"), ("edited.lua", b"original\n"),
        ("dropped.lua", b"gone upstream\n"), ("kept.lua", b"original\n"),
    ):
        path = _write(tmp_path / "pack" / name, data)
        baseline.entries[name] = git_sync.entry_for(path, blob_sha(data))
    _edit(tmp_path / "pack" / "edited.lua", b"edited by the player\n")
    _edit(tmp_path / "pack" / "kept.lua", b"changed here, gone upstream\n")
    remote = {"ours.lua": blob_sha(b"v2\n"), "edited.lua": blob_sha(b"upstream edit\n")}
    plan = make_plan(remote, tmp_path, "pack/", baseline)
    assert plan.download == {"ours.lua": remote["ours.lua"]}
    assert plan.modified == {"edited.lua": remote["edited.lua"]}
    assert plan.remove == ["dropped.lua"]
    assert plan.kept == ["kept.lua"]
    assert not plan.up_to_date


def test_a_file_untouched_since_the_last_update_is_not_hashed_again(tmp_path, monkeypatch):
    # Erion is about a gigabyte; an update that changed nothing mustn't read all of it.
    path = _write(tmp_path / "big.ogg", b"x" * 1000)
    sha = blob_sha(b"x" * 1000)
    baseline = Baseline(entries={"big.ogg": git_sync.entry_for(path, sha)})

    def hashed(_path):
        raise AssertionError("an unchanged file was hashed")

    monkeypatch.setattr(git_sync, "file_blob_sha", hashed)
    plan = make_plan({"big.ogg": sha}, tmp_path, "", baseline)
    assert plan.unchanged == ["big.ogg"]
    assert plan.up_to_date


def test_a_first_update_keeps_a_changed_world_file_and_refreshes_scripts(tmp_path):
    # With no baseline, a differing world file is most likely the player's settings.
    _write(tmp_path / "worlds" / "Miriani.MCL", b"<world player='settings'/>")
    _write(tmp_path / "scripts" / "old.lua", b"old")
    remote = {
        "worlds/Miriani.MCL": blob_sha(b"<world/>"),
        "scripts/old.lua": blob_sha(b"new"),
    }
    plan = make_plan(remote, tmp_path, "", None)
    assert plan.modified == {"worlds/Miriani.MCL": remote["worlds/Miriani.MCL"]}
    assert plan.download == {"scripts/old.lua": remote["scripts/old.lua"]}


def _blobs(tmp_path: Path, *contents: bytes) -> dict[str, Path]:
    """Fetched blobs, as git_remote.fetch_blobs leaves them: one file per hash."""
    folder = tmp_path / "blobs"
    folder.mkdir(exist_ok=True)
    landed = {}
    for data in contents:
        sha = blob_sha(data)
        landed[sha] = _write(folder / sha, data)
    return landed


def test_an_update_writes_the_packs_files_and_leaves_the_players_beside_them(tmp_path):
    pack = tmp_path / "pack"
    _write(pack / "sounds" / "hit.ogg", b"old hit")
    _write(pack / "worlds" / "w.mcl", b"<world mine/>")
    _write(pack / "gone.lua", b"dropped upstream")
    baseline = Baseline(entries={
        "sounds/hit.ogg": git_sync.entry_for(pack / "sounds" / "hit.ogg", blob_sha(b"old hit")),
        "gone.lua": git_sync.entry_for(pack / "gone.lua", blob_sha(b"dropped upstream")),
        "worlds/w.mcl": git_sync.entry_for(pack / "worlds" / "w.mcl", blob_sha(b"<world/>")),
    })
    _edit(pack / "worlds" / "w.mcl", b"<world mine, edited/>")
    remote = {
        "sounds/hit.ogg": blob_sha(b"new hit"),
        "sounds/miss.ogg": blob_sha(b"miss"),
        "worlds/w.mcl": blob_sha(b"<world v2/>"),
    }
    plan = make_plan(remote, pack, "", baseline)
    blobs = _blobs(tmp_path, b"new hit", b"miss", b"<world v2/>")
    applied = git_sync.apply_plan(
        pack, "", plan, blobs, repo="https://forge.example/pack.git", commit="c1", remote=remote,
    )
    assert applied.complete
    assert (pack / "sounds" / "hit.ogg").read_bytes() == b"new hit"
    assert (pack / "sounds" / "miss.ogg").read_bytes() == b"miss"
    assert (pack / "worlds" / "w.mcl").read_bytes() == b"<world mine, edited/>"
    assert (pack / "worlds" / "w.mcl.new").read_bytes() == b"<world v2/>"
    assert not (pack / "gone.lua").exists()
    assert applied.new_versions == ["worlds/w.mcl"]
    assert not list(pack.rglob("*.part"))

    # Run again with nothing new upstream: nothing to do, and the player's copy is held.
    again = make_plan(remote, pack, "", git_sync.read_baseline(pack))
    assert again.up_to_date
    assert again.held == {"worlds/w.mcl": remote["worlds/w.mcl"]}

    # The pack changes that file again: a fresh .new, the player's copy still stays.
    remote["worlds/w.mcl"] = blob_sha(b"<world v3/>")
    later = make_plan(remote, pack, "", git_sync.read_baseline(pack))
    assert later.modified == {"worlds/w.mcl": remote["worlds/w.mcl"]}


def test_an_update_that_stops_short_leaves_the_old_baseline(tmp_path):
    pack = tmp_path / "pack"
    _write(pack / "a.lua", b"a")
    remote = {"a.lua": blob_sha(b"a2"), "b.lua": blob_sha(b"b")}
    plan = make_plan(remote, pack, "", None)
    blobs = _blobs(tmp_path, b"a2")  # b.lua's blob never arrived
    applied = git_sync.apply_plan(pack, "", plan, blobs, repo="r", commit="c", remote=remote)
    assert applied.missing == ["b.lua"]
    assert git_sync.read_baseline(pack) is None  # so the next update runs the whole way again


def test_a_file_in_use_is_left_for_next_time(tmp_path, monkeypatch):
    # On Windows the music playing from a file stops it being replaced.
    pack = tmp_path / "pack"
    _write(pack / "music.ogg", b"old tune")
    _write(pack / "a.lua", b"a")
    remote = {"music.ogg": blob_sha(b"new tune"), "a.lua": blob_sha(b"a2")}
    plan = make_plan(remote, pack, "", None)
    blobs = _blobs(tmp_path, b"new tune", b"a2")
    real_replace = os.replace

    def locked(source, target):
        if Path(target).name == "music.ogg":
            raise PermissionError("in use by another process")
        return real_replace(source, target)

    monkeypatch.setattr(git_sync.os, "replace", locked)
    applied = git_sync.apply_plan(pack, "", plan, blobs, repo="r", commit="c", remote=remote)
    assert applied.in_use == ["music.ogg"] and applied.written == ["a.lua"]
    assert (pack / "music.ogg").read_bytes() == b"old tune"
    assert not list(pack.glob("*.part"))
    assert git_sync.read_baseline(pack) is None  # the next update tries it again
