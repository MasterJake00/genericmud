"""Working out what a soundpack update from its git repository has to change."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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


def test_the_baseline_round_trips_and_a_damaged_one_is_not_taken_for_none(tmp_path):
    # No baseline means a first update, which overwrites files it can't tell apart; a damaged
    # one mustn't be taken for that, or a later update overwrites the player's changes.
    assert git_sync.read_baseline(tmp_path) is None
    baseline = Baseline(repo="https://example.com/pack", commit="abc", prefix="wrapped/")
    baseline.entries["sounds/a b.ogg"] = git_sync.BaselineEntry("1" * 40, 12, 345)
    git_sync.write_baseline(tmp_path, baseline)
    assert git_sync.read_baseline(tmp_path) == baseline
    path = tmp_path / git_sync.BASELINE_NAME
    for damage in (
        "something else\n",
        path.read_text(encoding="utf-8") + "not an entry\n",
        path.read_text(encoding="utf-8").replace(" w ", " x "),
    ):
        path.write_text(damage, encoding="utf-8")
        with pytest.raises(git_sync.BaselineDamaged):
            git_sync.read_baseline(tmp_path)
    path.write_bytes(b"\xff\xfe not text")
    with pytest.raises(git_sync.BaselineDamaged):
        git_sync.read_baseline(tmp_path)


def test_a_path_with_a_line_break_cannot_add_lines_to_the_baseline(tmp_path):
    baseline = Baseline()
    baseline.entries["a.lua"] = git_sync.BaselineEntry("1" * 40, 1, 2)
    forged = "x\n" + "2" * 40 + " 3 4 w settings.ini"
    baseline.entries[forged] = git_sync.BaselineEntry("3" * 40, 5, 6)
    baseline.entries["y\u2028z"] = git_sync.BaselineEntry("4" * 40, 7, 8)
    git_sync.write_baseline(tmp_path, baseline)
    assert set(git_sync.read_baseline(tmp_path).entries) == {"a.lua"}


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



def test_a_link_named_like_a_scratch_file_cannot_redirect_a_write(tmp_path):
    # The scratch file used to be "<name>.part", opened without a care for what was there: a
    # link of that name sent the pack's bytes out of the pack.
    pack = tmp_path / "pack"
    pack.mkdir()
    outside = _write(tmp_path / "outside", b"player data")
    try:
        (pack / "a.lua.part").symlink_to(outside)
        (pack / git_sync.BASELINE_NAME).with_name(git_sync.BASELINE_NAME + ".part").symlink_to(
            outside
        )
    except OSError:
        pytest.skip("this system can't make symlinks")
    _write(pack / "notes.txt.part", b"a file of the player's")
    remote = {"a.lua": blob_sha(b"new"), "notes.txt": blob_sha(b"notes")}
    plan = make_plan(remote, pack, "", None)
    applied = git_sync.apply_plan(
        pack, "", plan, _blobs(tmp_path, b"new", b"notes"), repo="r", commit="c", remote=remote,
    )
    assert applied.complete
    assert outside.read_bytes() == b"player data"
    assert (pack / "a.lua").read_bytes() == b"new"
    assert (pack / "notes.txt.part").read_bytes() == b"a file of the player's"
    assert set(git_sync.read_baseline(pack).entries) == {"a.lua", "notes.txt"}


def _planned(tmp_path: Path, files: dict[str, bytes], remote_files: dict[str, bytes]):
    """A pack the updater wrote (``files``, with a baseline) and a plan to bring it up to
    ``remote_files``."""
    pack = tmp_path / "pack"
    baseline = Baseline()
    for name, data in files.items():
        baseline.entries[name] = git_sync.entry_for(_write(pack / name, data), blob_sha(data))
    remote = {name: blob_sha(data) for name, data in remote_files.items()}
    return pack, remote, make_plan(remote, pack, "", baseline)


def test_an_edit_made_while_the_update_downloads_is_kept(tmp_path):
    pack, remote, plan = _planned(tmp_path, {"a.lua": b"old"}, {"a.lua": b"new"})
    assert plan.download == {"a.lua": remote["a.lua"]}
    _edit(pack / "a.lua", b"the player's edit, made during the download")
    applied = git_sync.apply_plan(
        pack, "", plan, _blobs(tmp_path, b"new"), repo="r", commit="c", remote=remote,
    )
    assert (pack / "a.lua").read_bytes() == b"the player's edit, made during the download"
    assert (pack / "a.lua.new").read_bytes() == b"new"
    assert applied.new_versions == ["a.lua"] and applied.written == []
    assert git_sync.read_baseline(pack).entries["a.lua"].kept


def test_a_file_edited_while_the_update_downloads_is_not_recorded_as_the_packs(tmp_path):
    # It was right when the plan was made; recording its new size and time against the pack's
    # hash would let the next update overwrite the player's edit.
    pack, remote, plan = _planned(
        tmp_path, {"same.lua": b"same", "b.lua": b"b"}, {"same.lua": b"same", "b.lua": b"b2"},
    )
    assert plan.unchanged == ["same.lua"]
    _edit(pack / "same.lua", b"edited meanwhile")
    git_sync.apply_plan(
        pack, "", plan, _blobs(tmp_path, b"b2"), repo="r", commit="c", remote=remote,
    )
    remote["same.lua"] = blob_sha(b"same, v2")
    later = make_plan(remote, pack, "", git_sync.read_baseline(pack))
    assert later.modified == {"same.lua": remote["same.lua"]}
    assert later.download == {}


def test_a_dropped_file_edited_while_the_update_downloads_is_not_deleted(tmp_path):
    pack, remote, plan = _planned(tmp_path, {"gone.lua": b"x", "a.lua": b"a"}, {"a.lua": b"a"})
    assert plan.remove == ["gone.lua"]
    _edit(pack / "gone.lua", b"the player's now")
    applied = git_sync.apply_plan(pack, "", plan, {}, repo="r", commit="c", remote=remote)
    assert (pack / "gone.lua").read_bytes() == b"the player's now"
    assert applied.kept == ["gone.lua"] and applied.removed == []


def test_something_in_the_way_is_reported_and_the_rest_still_lands(tmp_path):
    # The repository has "maps/town.lua" where the pack has a file "maps", and "music" where
    # the pack has a folder: neither can go in, and they mustn't stop everything else.
    pack = tmp_path / "pack"
    _write(pack / "maps", b"a file, not a folder")
    (pack / "music").mkdir(parents=True)
    remote = {
        "maps/town.lua": blob_sha(b"town"), "music": blob_sha(b"tune"), "a.lua": blob_sha(b"a"),
    }
    plan = make_plan(remote, pack, "", None)
    applied = git_sync.apply_plan(
        pack, "", plan, _blobs(tmp_path, b"town", b"tune", b"a"),
        repo="r", commit="c", remote=remote,
    )
    assert sorted(applied.blocked) == ["maps/town.lua", "music"]
    assert applied.written == ["a.lua"] and applied.complete
    assert set(git_sync.read_baseline(pack).entries) == {"a.lua"}
    assert (pack / "maps").read_bytes() == b"a file, not a folder"


def test_a_link_in_the_pack_is_replaced_or_removed_itself_not_what_it_points_at(tmp_path):
    pack = tmp_path / "pack"
    _write(pack / "real.ogg", b"real")
    try:
        (pack / "link.ogg").symlink_to(pack / "real.ogg")
        (pack / "old.ogg").symlink_to(pack / "real.ogg")
    except OSError:
        pytest.skip("this system can't make symlinks")
    # Written: the repository has a file where the pack has a link.
    remote = {"real.ogg": blob_sha(b"real"), "link.ogg": blob_sha(b"l")}
    plan = make_plan(remote, pack, "", None)
    assert plan.download == {"link.ogg": remote["link.ogg"]}
    git_sync.apply_plan(
        pack, "", plan, _blobs(tmp_path, b"l"), repo="r", commit="c", remote=remote,
    )
    assert not (pack / "link.ogg").is_symlink()
    assert (pack / "link.ogg").read_bytes() == b"l"
    assert (pack / "real.ogg").read_bytes() == b"real"
    # Removed: the repository dropped a file the updater recorded, which here is a link.
    baseline = git_sync.read_baseline(pack)
    baseline.entries["old.ogg"] = git_sync.entry_for(pack / "old.ogg", blob_sha(b"real"))
    plan = make_plan(remote, pack, "", baseline)
    assert plan.remove == ["old.ogg"]
    git_sync.apply_plan(pack, "", plan, {}, repo="r", commit="c", remote=remote)
    assert not (pack / "old.ogg").is_symlink() and not (pack / "old.ogg").exists()
    assert (pack / "real.ogg").read_bytes() == b"real"
