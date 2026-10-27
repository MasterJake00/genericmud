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


def test_a_licence_file_named_for_its_licence_is_found_wherever_it_is():
    # pygame's wheel carries its LGPL as pygame/docs/generated/LGPL.txt and nothing else.
    try:
        pygame = notices.metadata.distribution("pygame")
    except notices.metadata.PackageNotFoundError:
        import pytest

        pytest.skip("pygame isn't installed here")
    names = [name for name, _text in notices.licence_texts(pygame)]
    assert "LGPL.txt" in names


class _BareDistribution:
    """A library that installed no licence file, declaring MIT, as proxy_tools does."""

    version = "0.1.0"
    files: list = []

    class metadata:  # noqa: N801 - the attribute importlib.metadata gives a distribution
        _fields = {"Name": "bare_tools", "License": "MIT", "Author": "A. Author"}

        @classmethod
        def get(cls, key, default=None):
            return cls._fields.get(key, default)

        @classmethod
        def get_all(cls, _key):
            return []

        def __class_getitem__(cls, key):
            return cls._fields[key]


def test_an_mit_library_without_its_own_text_gets_mits_with_its_author():
    text = notices.render([_BareDistribution()])
    assert "bare_tools 0.1.0" in text
    assert "Copyright (c) A. Author" in text
    assert "Permission is hereby granted, free of charge" in text
