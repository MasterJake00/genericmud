"""Transactional upgrade: diff-based verify set, backup, and best-effort rollback.

The overlay itself is ZipExtractor's job on Windows; here we fake it by writing files onto the
"install" directory, so the diff/backup/verify/rollback logic is exercised on any platform.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from genericmud.update import upgrade_manager as um

_OLD = {
    "genericMud.exe": b"old-exe",
    "_internal/base_library.zip": b"old-base",
    "_internal/python313.dll": b"old-dll",
    "_internal/unchanged.pyd": b"same-bytes",  # identical in NEW -> skipped
}
_NEW = {
    "genericMud.exe": b"new-exe",
    "_internal/base_library.zip": b"new-base",
    "_internal/python313.dll": b"new-dll",
    "_internal/unchanged.pyd": b"same-bytes",  # identical -> not verified/backed up
    "_internal/added.pyd": b"brand-new-file",  # a file the upgrade adds
}
_CHANGED = {"genericMud.exe", "_internal/base_library.zip", "_internal/python313.dll"}


def _write_tree(root: Path, contents: dict[str, bytes]) -> None:
    for rel, data in contents.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _make_zip(path: Path, contents: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for rel, data in contents.items():
            archive.writestr(rel, data)
    return path


def _prepared_install(tmp_path: Path) -> tuple[Path, Path]:
    install = tmp_path / "app"
    install.mkdir()
    _write_tree(install, _OLD)
    upgrade_zip = _make_zip(tmp_path / "upgrade.zip", _NEW)
    um.prepare_for_upgrade(install, upgrade_zip, "v9.9.9", exe_name="genericMud.exe")
    return install, upgrade_zip


def test_files_to_verify_is_the_changed_set(tmp_path):
    install = tmp_path / "app"
    _write_tree(install, _OLD)
    upgrade_zip = _make_zip(tmp_path / "u.zip", _NEW)
    changed = um.files_to_verify(install, upgrade_zip)
    assert set(changed) == _CHANGED | {"_internal/added.pyd"}
    assert "_internal/unchanged.pyd" not in changed  # identical -> skipped


def test_prepare_backs_up_only_changed_existing_files(tmp_path):
    install, _ = _prepared_install(tmp_path)
    backup = install / um.STATE_DIR_NAME / um.BACKUP_DIR_NAME
    assert (install / um.STATE_DIR_NAME / um.PENDING_FILE_NAME).is_file()
    for rel in _CHANGED:
        assert (backup / rel).is_file()
    assert not (backup / "_internal/unchanged.pyd").exists()  # unchanged -> not backed up
    assert not (backup / "_internal/added.pyd").exists()  # new file -> nothing to back up


def test_prepare_raises_when_nothing_changes(tmp_path):
    install = tmp_path / "app"
    _write_tree(install, _OLD)
    same_zip = _make_zip(tmp_path / "same.zip", _OLD)  # identical content
    with pytest.raises(um.UpgradeIntegrityError):
        um.prepare_for_upgrade(install, same_zip, "v1", exe_name="")  # no exe anchor


def test_recover_clean_when_overlay_succeeds(tmp_path):
    install, _ = _prepared_install(tmp_path)
    _write_tree(install, _NEW)  # a complete overlay: every changed file now matches the zip
    assert um.recover_pending_upgrade(install) is None
    assert not (install / um.STATE_DIR_NAME).exists()  # state cleared on success


def test_recover_rolls_back_partial_overlay(tmp_path):
    install, _ = _prepared_install(tmp_path)
    # Partial overlay: exe + dll replaced, but base_library.zip left stale (e.g. it was locked).
    (install / "genericMud.exe").write_bytes(_NEW["genericMud.exe"])
    (install / "_internal/python313.dll").write_bytes(_NEW["_internal/python313.dll"])

    result = um.recover_pending_upgrade(install)

    assert result is not None and result.rolled_back
    assert any("base_library" in item for item in result.failed_files)
    # Everything restored to the pre-upgrade snapshot, not left half-new.
    for rel in _CHANGED:
        assert (install / rel).read_bytes() == _OLD[rel]
    assert not (install / um.STATE_DIR_NAME).exists()


def test_recover_emergency_rollback_on_corrupt_marker(tmp_path):
    install, _ = _prepared_install(tmp_path)
    (install / "genericMud.exe").write_bytes(b"half-written")
    (install / um.STATE_DIR_NAME / um.PENDING_FILE_NAME).write_text("{ not valid json")

    result = um.recover_pending_upgrade(install)

    assert result is not None and result.rolled_back
    assert (install / "genericMud.exe").read_bytes() == _OLD["genericMud.exe"]
    assert not (install / um.STATE_DIR_NAME).exists()


def test_recover_noop_without_pending(tmp_path):
    install = tmp_path / "app"
    _write_tree(install, _OLD)
    assert um.recover_pending_upgrade(install) is None


# --- files an earlier version left behind ---


def _built(root: Path, names: list[str]) -> Path:
    """A contents folder as a build leaves it: these files plus the build's manifest."""
    contents = root / "app" / "_internal"
    _write_tree(contents, {name: b"x" for name in names})
    um.write_manifest(contents, "9.9.9")
    return contents


_SHIPPED = [f"lib/mod{i}.pyd" for i in range(60)] + ["genericmud-1.0.dist-info/METADATA"]


def test_a_build_lists_every_file_in_its_folder_and_itself(tmp_path):
    contents = _built(tmp_path, ["a.dll", "sub/b.pyd"])
    listed = (contents / um.MANIFEST_NAME).read_text(encoding="utf-8").splitlines()
    assert listed[0].startswith("# genericMud 9.9.9")
    assert listed[1:] == ["a.dll", "sub/b.pyd", um.MANIFEST_NAME]


def test_files_an_older_version_left_are_removed(tmp_path):
    contents = _built(tmp_path, _SHIPPED)
    _write_tree(contents, {
        "genericmud-0.6.5.dist-info/METADATA": b"old",  # an older version's metadata
        "wx/wxmsw333u_core_vc140_x64.dll": b"old wx",  # beside the current wx's files
        "accessible_output2/__init__.pyc": b"dropped library",
    })
    removed = um.prune_leftovers(contents)
    assert sorted(removed) == [
        "accessible_output2/__init__.pyc",
        "genericmud-0.6.5.dist-info/METADATA",
        "wx/wxmsw333u_core_vc140_x64.dll",
    ]
    assert not (contents / "accessible_output2").exists()  # emptied folders go too
    assert not (contents / "genericmud-0.6.5.dist-info").exists()
    assert all((contents / name).is_file() for name in _SHIPPED)


def test_the_players_files_beside_the_contents_folder_are_never_touched(tmp_path):
    contents = _built(tmp_path, _SHIPPED)
    _write_tree(contents.parent, {
        "genericmud-data/worlds.toml": b"[[world]]",
        "genericMud.exe": b"exe",
        "ZipExtractor.exe": b"helper",
    })
    um.prune_leftovers(contents)
    assert (contents.parent / "genericmud-data" / "worlds.toml").read_bytes() == b"[[world]]"
    assert (contents.parent / "genericMud.exe").exists()


def test_a_name_in_another_case_is_the_same_file(tmp_path):
    # Windows keeps a file's old spelling when an update overwrites it.
    contents = _built(tmp_path, _SHIPPED)
    shipped = contents / "lib" / "mod7.pyd"
    shipped.rename(contents / "lib" / "MOD7.PYD")
    assert um.prune_leftovers(contents) == []


@pytest.mark.parametrize("damage", ["no manifest", "short list", "not ours", "file missing"])
def test_nothing_is_removed_unless_the_list_matches_what_is_here(tmp_path, damage):
    contents = _built(tmp_path, _SHIPPED)
    manifest = contents / um.MANIFEST_NAME
    lines = manifest.read_text(encoding="utf-8").splitlines()
    if damage == "no manifest":
        manifest.unlink()
    elif damage == "short list":
        manifest.write_text("\n".join(lines[:10] + [um.MANIFEST_NAME]), encoding="utf-8")
    elif damage == "not ours":  # doesn't list itself
        manifest.write_text("\n".join(lines[:-1]), encoding="utf-8")
    else:  # another version's files are here: a rollback, or an update only partly applied
        (contents / _SHIPPED[3]).unlink()
    stale = contents / "stale.dll"
    stale.write_bytes(b"old")
    assert um.prune_leftovers(contents) == []
    assert stale.exists()


def test_a_linked_folder_inside_is_not_followed(tmp_path):
    contents = _built(tmp_path, _SHIPPED)
    elsewhere = tmp_path / "elsewhere"
    _write_tree(elsewhere, {"precious.txt": b"keep me"})
    try:
        (contents / "linked").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("this system can't make symlinks")
    um.prune_leftovers(contents)
    assert (elsewhere / "precious.txt").read_bytes() == b"keep me"


def test_startup_tidies_the_contents_folder_unless_it_rolled_back(tmp_path, monkeypatch):
    import sys
    import types

    import genericmud.__main__ as entry

    contents = _built(tmp_path, _SHIPPED)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(contents), raising=False)
    monkeypatch.setattr(entry, "_sweep_update_leftovers", lambda: None)
    monkeypatch.setattr("genericmud.session.crashlog.install_crash_handlers", lambda: None)
    monkeypatch.setitem(
        sys.modules, "genericmud.ui.wx_app", types.SimpleNamespace(run=lambda *_a, **_k: None)
    )
    stale = contents / "stale.dll"

    stale.write_bytes(b"old")
    rolled_back = um.RecoveryResult(rolled_back=True, title="t", message="m")
    monkeypatch.setattr(entry, "_recover_pending_upgrade", lambda: rolled_back)
    entry.main([])
    assert stale.exists()  # the files here may be the previous version's now

    monkeypatch.setattr(entry, "_recover_pending_upgrade", lambda: None)
    entry.main([])
    assert not stale.exists()


def test_a_junction_inside_is_not_followed(tmp_path):
    # Windows' own kind of folder link, which needs no special rights to make.
    winapi = pytest.importorskip("_winapi")
    contents = _built(tmp_path, _SHIPPED)
    elsewhere = tmp_path / "elsewhere"
    _write_tree(elsewhere, {"precious.txt": b"keep me"})
    winapi.CreateJunction(str(elsewhere), str(contents / "linked"))
    um.prune_leftovers(contents)
    assert (elsewhere / "precious.txt").read_bytes() == b"keep me"
