"""Updater core: version parsing, release selection, asset choice, Zip-Slip-safe extract.

The Windows file-swap itself can't run here; these cover the pure-Python decision logic that
determines whether an update is offered and that a malicious zip is refused.
"""

from __future__ import annotations

import zipfile

import pytest

from genericmud.update import self_update


@pytest.fixture(autouse=True)
def _fixed_platform(monkeypatch):
    # The historical asset tests assert Windows behavior; pin the platform so they pass on
    # the (Linux) CI/dev host. The per-platform selection is covered explicitly below.
    monkeypatch.setattr(self_update.sys, "platform", "win32")


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("v1.2.3", (1, 2, 3)),
        ("1.2.3", (1, 2, 3)),
        ("v0.5.4", (0, 5, 4)),
        ("v1.2.3-beta", None),  # prerelease-shaped -> not offered as stable
        ("v1.2", None),
        ("nightly", None),
        ("", None),
    ],
)
def test_parse_version(tag, expected):
    assert self_update._parse_version(tag) == expected


def test_select_asset_prefers_windows_zip():
    assets = [
        {"name": "source.tar.gz", "browser_download_url": "x"},
        {"name": "genericMud-windows.zip", "browser_download_url": "y"},
    ]
    assert self_update._select_asset(assets)["name"] == "genericMud-windows.zip"


def test_select_asset_none_when_absent():
    assert self_update._select_asset([{"name": "notes.txt"}]) is None


def test_select_asset_is_platform_specific(monkeypatch):
    assets = [
        {"name": "genericMud-windows.zip", "browser_download_url": "w"},
        {"name": "genericMud-macos.zip", "browser_download_url": "m"},
        {"name": "genericMud-linux.tar.gz", "browser_download_url": "l"},
    ]
    monkeypatch.setattr(self_update.sys, "platform", "darwin")
    assert self_update._select_asset(assets)["name"] == "genericMud-macos.zip"
    monkeypatch.setattr(self_update.sys, "platform", "linux")
    assert self_update._select_asset(assets)["name"] == "genericMud-linux.tar.gz"
    monkeypatch.setattr(self_update.sys, "platform", "win32")
    assert self_update._select_asset(assets)["name"] == "genericMud-windows.zip"


def _release(tag, *, draft=False, with_asset=True):
    assets = (
        [{
            "name": "genericMud-windows.zip",
            "browser_download_url": f"http://x/{tag}.zip",
            "size": 42,
        }]
        if with_asset else []
    )
    return {
        "tag_name": tag, "draft": draft, "html_url": f"http://x/{tag}",
        "body": "notes", "assets": assets,
    }


def test_check_for_update_picks_newest_skipping_drafts(monkeypatch):
    releases = [
        _release("v0.5.1"),
        _release("v0.6.0"),  # newest non-draft, all-prerelease repo -> still selected
        _release("v0.7.0", draft=True),  # draft skipped despite higher version
        _release("nightly"),  # unparseable skipped
    ]
    monkeypatch.setattr(self_update, "_get_json", lambda _url: releases)
    monkeypatch.setattr(self_update, "current_version", lambda: "0.5.4")

    info = self_update.check_for_update()
    assert info is not None
    assert info["tag"] == "v0.6.0"
    assert info["download_url"] == "http://x/v0.6.0.zip"
    assert info["size"] == 42


_NOTES = """**Updating**

* The update window tells you what's new in every
  version since yours.

## 0.20.3 — 2026-10-27

**Soundpacks**

* Toastush's index command no longer goes to
  the MUD.

## 0.20.2 — 2026-10-27

**Soundpacks**

* Miriani-Next's ambience plays.

## 0.20.1 — 2026-10-27

* Find carries on from the bottom.
"""


def test_the_notes_cover_every_version_since_yours_and_none_before(monkeypatch):
    release = _release("v0.20.4")
    release["body"] = _NOTES
    monkeypatch.setattr(self_update, "_get_json", lambda _url: [release])
    monkeypatch.setattr(self_update, "current_version", lambda: "0.20.2")
    notes = self_update.check_for_update()["notes"]
    assert notes.index("New in 0.20.4") < notes.index("New in 0.20.3")
    assert "Toastush's index command no longer goes to the MUD." in notes  # one line each
    assert "ambience" not in notes and "0.20.2" not in notes  # what you have already


def test_the_notes_read_without_markdown_marks():
    notes = self_update.notes_since(_NOTES, "v0.20.4", (0, 0, 0))
    assert "*" not in notes and "#" not in notes and "—" not in notes
    assert "\nSoundpacks\n" in notes
    assert "New in 0.20.1, 2026-10-27" in notes


def test_notes_without_version_headings_are_kept_whole():
    notes = self_update.notes_since("**Fixes**\n\n* It works now.", "v1.0.0", (0, 9, 0))
    assert notes == "New in 1.0.0\n\nFixes\n\nIt works now."


def test_a_version_heading_written_another_way_still_ends_whats_new():
    body = "* Newest.\n\n## 0.20.3 (27 October)\n\n* Middle.\n\n## 0.20.2\n\n* Installed."
    notes = self_update.notes_since(body, "v0.20.4", (0, 20, 2))
    assert notes == "New in 0.20.4\n\nNewest.\n\nNew in 0.20.3, (27 October)\n\nMiddle."


def test_notes_that_cant_be_tidied_still_let_you_update(monkeypatch):
    # A bug in tidying the notes must not block the update that would fix it.
    def broken(*_args):
        raise ValueError("a body the tidying never expected")

    release = _release("v0.20.4")
    release["body"] = _NOTES
    monkeypatch.setattr(self_update, "_get_json", lambda _url: [release])
    monkeypatch.setattr(self_update, "current_version", lambda: "0.20.2")
    monkeypatch.setattr(self_update, "notes_since", broken)
    info = self_update.check_for_update()
    assert info is not None and info["tag"] == "v0.20.4"
    assert info["notes"] == _NOTES  # as the release has them


def test_check_for_update_none_when_up_to_date(monkeypatch):
    monkeypatch.setattr(self_update, "_get_json", lambda _url: [_release("v0.6.0")])
    monkeypatch.setattr(self_update, "current_version", lambda: "0.6.0")
    assert self_update.check_for_update() is None


def test_check_for_update_none_when_newer_has_no_windows_asset(monkeypatch):
    monkeypatch.setattr(
        self_update, "_get_json", lambda _url: [_release("v0.6.0", with_asset=False)]
    )
    monkeypatch.setattr(self_update, "current_version", lambda: "0.5.4")
    assert self_update.check_for_update() is None


def test_check_for_update_none_when_current_version_unknown(monkeypatch):
    # A frozen build missing --copy-metadata resolves to no version: never guess an update.
    monkeypatch.setattr(self_update, "current_version", lambda: None)
    monkeypatch.setattr(self_update, "_get_json", lambda _url: [_release("v9.9.9")])
    assert self_update.check_for_update() is None


def test_safe_extract_normal_zip(tmp_path):
    src = tmp_path / "ok.zip"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr("genericMud/genericMud.exe", b"exe")
        archive.writestr("genericMud/_internal/base_library.zip", b"lib")
    out = tmp_path / "out"
    self_update._safe_extract(src, out)
    assert (out / "genericMud" / "genericMud.exe").read_bytes() == b"exe"


@pytest.mark.parametrize("member", ["../escape.txt", "/abs/escape.txt"])
def test_safe_extract_rejects_traversal(tmp_path, member):
    src = tmp_path / "evil.zip"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr(member, b"pwned")
    with pytest.raises(RuntimeError):
        self_update._safe_extract(src, tmp_path / "out")


def test_current_version_reads_source_version():
    """current_version() returns genericmud.__version__ -- baked into every frozen build,
    unlike importlib.metadata, which needs --copy-metadata and silently returns nothing."""
    import genericmud

    assert self_update.current_version() == genericmud.__version__


def test_current_version_survives_missing_dist_metadata(monkeypatch):
    """The dead-updater mode: importlib.metadata can't resolve the dist. With __version__ in
    source, current_version must still answer (not None), so check_for_update keeps working."""
    import genericmud

    def _raise(_name):
        raise self_update.PackageNotFoundError("genericmud")

    monkeypatch.setattr(self_update, "_pkg_version", _raise)
    assert self_update.current_version() == genericmud.__version__  # source wins, metadata unused


def _release_zip(path, exe_bytes):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("genericMud/genericMud.exe", exe_bytes)
        archive.writestr("genericMud/_internal/lib.pyd", b"new library")


def test_a_helper_that_never_launched_leaves_no_failed_update_behind(tmp_path, monkeypatch):
    # The rollback marker is written just before the helper starts. When antivirus or a
    # declined prompt stops the helper, nothing was installed, yet the next launch read
    # the marker and announced "Update failed, genericMud was restored".
    from genericmud.update import upgrade_manager

    install = tmp_path / "install"
    install.mkdir()
    (install / "genericMud.exe").write_bytes(b"old exe")
    (install / "ZipExtractor.exe").write_bytes(b"helper")
    monkeypatch.setattr(self_update.sys, "executable", str(install / "genericMud.exe"))
    monkeypatch.setattr(self_update, "can_self_replace", lambda: True)

    def fake_download(_url, dest, progress_cb=None, expected_size=0):
        _release_zip(dest, b"new exe")

    def blocked_spawn(*_args):
        raise RuntimeError("ShellExecuteW failed (code 5) launching ZipExtractor.exe")

    monkeypatch.setattr(self_update, "_download", fake_download)
    monkeypatch.setattr(self_update, "_spawn_extractor", blocked_spawn)
    with pytest.raises(RuntimeError):
        self_update.download_and_replace({"download_url": "https://x/y.zip", "tag": "v9.9.9"})

    assert upgrade_manager.recover_pending_upgrade(install) is None
    assert (install / "genericMud.exe").read_bytes() == b"old exe"


def test_startup_clears_out_old_update_folders(tmp_path, monkeypatch):
    # Every in-app update leaves its download (about 50 MB) and a helper copy in a temp
    # folder; the sweep that removes them existed but nothing ever called it.
    import os
    import sys
    import time
    import types

    import genericmud.__main__ as entry

    old = tmp_path / "genericmud-update-old"
    old.mkdir()
    two_days_ago = time.time() - 2 * 24 * 3600
    os.utime(old, (two_days_ago, two_days_ago))
    fresh = tmp_path / "genericmud-update-fresh"  # may still be in use by a running update
    fresh.mkdir()
    monkeypatch.setattr(self_update.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(entry, "_recover_pending_upgrade", lambda: None)
    monkeypatch.setattr("genericmud.session.crashlog.install_crash_handlers", lambda: None)
    opened: list[bool] = []
    window = types.SimpleNamespace(run=lambda _args, recovery=None: opened.append(True))
    monkeypatch.setitem(sys.modules, "genericmud.ui.wx_app", window)

    entry.main([])

    assert opened == [True]
    assert not old.exists()
    assert fresh.exists()
