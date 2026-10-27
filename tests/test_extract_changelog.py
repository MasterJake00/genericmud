"""Release-notes extraction (tools/extract_changelog.py).

The GitHub release body is filled from CHANGELOG.md at tag time, and
``self_update.check_for_update`` hands that body straight to the update dialog as its
notes. These guard the two ways that silently goes wrong: a section that bleeds into the
next version's, and a tag with no section at all.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MOD_PATH = _REPO_ROOT / "tools" / "extract_changelog.py"
_spec = importlib.util.spec_from_file_location("extract_changelog", _MOD_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
extract_section = _mod.extract_section
extract_notes = _mod.extract_notes
main = _mod.main


_SAMPLE = """# Changelog

Preamble text that belongs to no version.

## 0.8.0 — 2026-08-01

**New features**

* Find in the output.

## 0.7.1 — 2026-07-27

**Bug fixes**

* Channel triggers match again.

## 0.7.0 — 2026-07-17

Menu access keys.
"""


def test_extracts_named_section_without_its_heading():
    out = extract_section(_SAMPLE, "0.8.0")
    assert out.startswith("**New features**")
    assert "Find in the output." in out
    assert "Channel triggers" not in out  # stops at the next heading
    assert "## 0.7.1" not in out


def test_accepts_a_v_prefixed_tag():
    assert extract_section(_SAMPLE, "v0.7.1") == extract_section(_SAMPLE, "0.7.1")


def test_a_middle_section_is_bounded_at_both_ends():
    out = extract_section(_SAMPLE, "0.7.1")
    assert "Channel triggers match again." in out
    assert "New features" not in out
    assert "Menu access keys" not in out


def test_the_last_section_runs_to_end_of_file():
    assert extract_section(_SAMPLE, "0.7.0") == "Menu access keys."


def test_the_preamble_is_never_returned():
    assert "Preamble" not in (extract_section(_SAMPLE, "0.8.0") or "")


def test_an_unknown_version_returns_none():
    assert extract_section(_SAMPLE, "9.9.9") is None


def test_a_partial_version_does_not_falsely_match():
    assert extract_section(_SAMPLE, "0.8") is None


def test_a_longer_version_does_not_match_a_prefix():
    # "0.7.1" must not be found by a lookup for "0.7.10" or vice versa.
    assert extract_section("## 0.7.10 — 2026-09-01\n\nLater.\n", "0.7.1") is None


def test_main_exits_nonzero_when_the_tag_has_no_section(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(_SAMPLE, encoding="utf-8")
    assert main([str(changelog), "9.9.9", str(tmp_path / "out.md")]) == 1
    assert not (tmp_path / "out.md").exists()  # no half-written empty notes


def test_main_writes_the_section_to_the_output_file(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(_SAMPLE, encoding="utf-8")
    out = tmp_path / "release_notes.md"
    assert main([str(changelog), "v0.7.1", str(out)]) == 0
    assert "Channel triggers match again." in out.read_text(encoding="utf-8")


def test_release_notes_carry_the_earlier_versions_too():
    # The update window of genericMud 0.20.3 and older reads only the newest release's notes,
    # so someone several versions behind heard about the newest change alone. Each release's
    # notes now carry the earlier versions under their own headings, newest first.
    out = extract_notes(_SAMPLE, "v0.8.0")
    assert out.startswith("**New features**")  # its own section first, its heading dropped
    assert "## 0.7.1 — 2026-07-27" in out
    assert out.index("Channel triggers match again.") < out.index("Menu access keys.")
    assert "Preamble" not in out


def test_release_notes_stop_at_the_size_limit_and_say_where_the_rest_is():
    out = extract_notes(_SAMPLE, "0.8.0", limit=120)
    assert "Channel triggers match again." in out
    assert "Menu access keys." not in out  # the next whole section won't fit
    assert out.rstrip().endswith(_mod.OLDER_NOTES)
    assert len(extract_notes(_SAMPLE, "0.8.0", limit=1)) > 0  # its own section always goes in


def test_the_oldest_versions_notes_stand_alone():
    assert extract_notes(_SAMPLE, "0.7.0") == "Menu access keys."
    assert extract_notes(_SAMPLE, "9.9.9") is None


def test_the_real_changelog_has_a_section_for_the_current_version():
    # The release workflow will fail the build without this, so catch it here first.
    from genericmud import __version__

    changelog = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = extract_section(changelog, __version__)
    assert section, f"CHANGELOG.md has no section for {__version__}"
