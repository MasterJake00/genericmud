"""Write the licence notices for what a genericMud build carries: Python, and every library the
app runs on, as their licences ask when they're passed on in a build.

The libraries are genericMud's own runtime dependencies (pyproject.toml's dependencies and its
gui, voice and audio extras) and what those need in turn, on this platform, as installed in the
environment the build runs in. Test and build tools are left out: none of them is in the app.
BASS's and BASS_FX's terms aren't here: their own readmes go in the app's "bass" folder.

    python tools/third_party_notices.py <output file>
"""

from __future__ import annotations

import re
import sys
import sysconfig
import tomllib
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
RUNTIME_EXTRAS = ("gui", "voice", "audio")  # "package" is PyInstaller, which builds the app
# A licence file by name, wherever the library put it (pygame keeps its LGPL as
# pygame/docs/generated/LGPL.txt).
_LICENCE_FILE = re.compile(
    r"^(licen[cs]e|copying|notice|copyright|authors|lgpl|gpl)", re.IGNORECASE
)
# The MIT licence, for a library that declares it but installed no text of its own.
_MIT = (
    "Copyright (c) {holder}\n\n"
    "Permission is hereby granted, free of charge, to any person obtaining a copy of this "
    "software and associated documentation files (the \"Software\"), to deal in the Software "
    "without restriction, including without limitation the rights to use, copy, modify, "
    "merge, publish, distribute, sublicense, and/or sell copies of the Software, and to "
    "permit persons to whom the Software is furnished to do so, subject to the following "
    "conditions:\n\n"
    "The above copyright notice and this permission notice shall be included in all copies "
    "or substantial portions of the Software.\n\n"
    "THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, "
    "INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A "
    "PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT "
    "HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF "
    "CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE "
    "OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.\n"
)
_RULE = "=" * 78


def runtime_requirements(pyproject: Path = PYPROJECT) -> list[Requirement]:
    """genericMud's own requirements that the app runs with."""
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    texts = list(project.get("dependencies", []))
    optional = project.get("optional-dependencies", {})
    for extra in RUNTIME_EXTRAS:
        texts += optional.get(extra, [])
    return [Requirement(text) for text in texts]


def _applies(requirement: Requirement, extras: Iterable[str]) -> bool:
    """Whether a requirement holds on this platform, for the extras asked of its parent."""
    if requirement.marker is None:
        return True
    marker: Marker = requirement.marker
    asked = list(extras) or [""]
    return any(marker.evaluate({"extra": extra}) for extra in asked)


def runtime_distributions(requirements: Iterable[Requirement]) -> list[metadata.Distribution]:
    """The installed distributions the app runs on: the requirements given, and what each
    needs in turn on this platform, each once, by name."""
    found: dict[str, metadata.Distribution] = {}
    pending: list[tuple[Requirement, tuple[str, ...]]] = [
        (requirement, ()) for requirement in requirements
    ]
    while pending:
        requirement, parent_extras = pending.pop()
        if not _applies(requirement, parent_extras):
            continue
        name = canonicalize_name(requirement.name)
        try:
            distribution = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError:
            continue  # not installed here: not in this platform's build either
        extras = tuple(sorted(requirement.extras))
        if name in found and not extras:
            continue
        found[name] = distribution
        for text in distribution.requires or []:
            pending.append((Requirement(text), extras))
    return sorted(found.values(), key=lambda dist: canonicalize_name(dist.metadata["Name"]))


def licence_texts(distribution: metadata.Distribution) -> list[tuple[str, str]]:
    """(file name, text) for each licence or notice file the distribution installed."""
    texts = []
    for path in distribution.files or []:
        in_licences_folder = "licenses" in (part.lower() for part in path.parts[:-1])
        if not (_LICENCE_FILE.match(path.name) or in_licences_folder):
            continue
        if path.suffix.lower() in (".py", ".pyc", ".so", ".dll", ".pyd", ".dylib"):
            continue
        located = Path(distribution.locate_file(path))
        try:
            texts.append((path.name, located.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return texts


def python_licence() -> str:
    """Python's own licence, as the running Python installed it."""
    candidates = (
        Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")
    return (
        "Python is distributed under the Python Software Foundation License; its full text "
        "is at https://docs.python.org/3/license.html\n"
    )


def render(distributions: Iterable[metadata.Distribution]) -> str:
    """The notices file: Python's licence, then each library's name, version, declared
    licence and licence files."""
    sections = [
        "genericMud is built from the software below. Each is passed on under its own licence, "
        "reproduced here as it asks. BASS's and BASS_FX's terms are in the bass folder.\n",
        f"{_RULE}\nPython {sys.version.split()[0]}\n{_RULE}\n\n{python_licence()}",
    ]
    for distribution in distributions:
        info = distribution.metadata
        declared = info.get("License-Expression") or info.get("License") or ""
        if len(declared) > 200 or "\n" in declared:
            declared = ""  # a whole licence pasted into the field: its file says it better
        classifiers = [
            value.split(" :: ")[-1] for value in info.get_all("Classifier") or []
            if value.startswith("License ::")
        ]
        heading = f"{info['Name']} {distribution.version}"
        lines = [f"{_RULE}\n{heading}\n{_RULE}"]
        if declared:
            lines.append(f"Licence: {declared}")
        elif classifiers:
            lines.append(f"Licence: {', '.join(classifiers)}")
        if info.get("Home-page"):
            lines.append(f"Home page: {info['Home-page']}")
        texts = licence_texts(distribution)
        for name, text in texts:
            lines.append(f"\n--- {name} ---\n\n{text.rstrip()}")
        mit = "MIT" in (declared or "").split() or "MIT License" in classifiers
        if not texts and mit:
            holder = info.get("Author") or info.get("Maintainer") or f"the {info['Name']} authors"
            lines.append(
                "\n(It installed no licence text of its own; this is the MIT licence it "
                "declares, with its author from its metadata.)\n\n" + _MIT.format(holder=holder)
            )
        elif not texts:
            lines.append("\n(This library installed no licence file of its own.)")
        sections.append("\n".join(lines) + "\n")
    return "\n\n".join(sections)


def write(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(runtime_distributions(runtime_requirements())), encoding="utf-8")
    return output


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python tools/third_party_notices.py <output file>", file=sys.stderr)
        return 2
    print(write(Path(argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
