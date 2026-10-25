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
