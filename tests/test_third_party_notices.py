"""The licence notices a build carries: Python's, and every library the app runs on, from the
environment the build runs in. Test and build tools aren't in the app, so not in the notices."""

from __future__ import annotations

import sys
from pathlib import Path

from packaging.requirements import Requirement

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import third_party_notices as notices  # noqa: E402


def _names(distributions) -> set[str]:
    return {dist.metadata["Name"].lower() for dist in distributions}


def test_the_apps_libraries_are_in_and_the_tools_are_out():
    names = _names(notices.runtime_distributions(notices.runtime_requirements()))
    assert {"lupa", "regex", "dulwich"} <= names
    assert "urllib3" in names  # what dulwich needs in turn
    assert not names & {"pytest", "ruff", "pyinstaller"}


def test_a_requirement_for_another_platform_is_left_out():
    requirement = Requirement('dulwich>=1; platform_system == "NoSuchSystem"')
    assert notices.runtime_distributions([requirement]) == []


def test_each_library_comes_with_its_licence_text_and_python_with_its_own():
    text = notices.render(notices.runtime_distributions([Requirement("dulwich")]))
    assert "dulwich" in text
    assert "Apache License" in text  # dulwich's own licence file, not just its name
    assert "Python Software Foundation" in text or "PYTHON SOFTWARE FOUNDATION" in text


def test_the_notices_are_written_where_asked(tmp_path):
    written = notices.write(tmp_path / "notices" / "THIRD-PARTY-NOTICES.txt")
    assert written.is_file()
    assert "lupa" in written.read_text(encoding="utf-8")
