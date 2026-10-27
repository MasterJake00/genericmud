"""Fetch the BASS audio library for this platform into a folder, for genericMud's builds and tests.

genericMud plays sound through BASS when a build carries it (genericmud/sound/bass_library.py).
un4seen publishes BASS only as zip files it replaces in place, so each one is pinned here by
its SHA-256: when un4seen releases a new version, the check fails, and someone looks at the
new version and updates the pins on purpose rather than a build quietly changing underneath.

    python tools/fetch_bass.py <folder>

The folder gets BASS and BASS_FX for this machine, and their read-me files, which carry their
licences. Point GENERICMUD_BASS_DIR at it to run the tests against real BASS.
"""

from __future__ import annotations

import hashlib
import io
import platform as platform_module
import sys
import urllib.request
import zipfile
from pathlib import Path

_CORE_URL = "https://www.un4seen.com/files/"
_EFFECTS_URL = "https://www.un4seen.com/files/z/0/"
# Each package's SHA-256 as fetched on 2026-10-27 (BASS 2.4.18.3, BASS_FX 2.4.12.6).
_PINS = {
    "bass24.zip": "3a03ec9a33d0f4f9d167660da51c8bb1432e8977496995455ab137277d69636e",
    "bass24-osx.zip": "dfadd6238896b02b144b2870655fb9ee2445fc84d5c00f7e1c56faf9343ce59c",
    "bass24-linux.zip": "9bf723dbf750d665c14cfc099f96a056483dc35de6850d2df76af8b640435ca9",
    "bass_fx24.zip": "a4baf602865941963127acb15ed12627d108189f99c2757970432ae7da0366cd",
    "bass_fx24-osx.zip": "eb9e496da229cdd73dc51d16c7e3fd17c7400a8125c2be2feb88eee1ac68dc68",
    "bass_fx24-linux.zip": "1ee97610bc2768357c4c344c0d7a058ac95edf51804c391fdcf7644762bd413b",
}
_LINUX_FOLDERS = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


def packages(system: str, machine: str) -> list[tuple[str, str, list[str]]]:
    """What to fetch for a platform: (package, where it comes from, files to take from it)."""
    if system == "win32":
        if machine.lower() not in ("amd64", "x86_64"):
            raise SystemExit(f"BASS's Windows package has no {machine} build; genericMud needs x64")
        return [
            ("bass24.zip", _CORE_URL, ["x64/bass.dll", "bass.txt"]),
            ("bass_fx24.zip", _EFFECTS_URL, ["x64/bass_fx.dll", "bass_fx.txt"]),
        ]
    if system == "darwin":
        return [  # universal: Intel and Apple silicon in one file
            ("bass24-osx.zip", _CORE_URL, ["libbass.dylib", "bass.txt"]),
            ("bass_fx24-osx.zip", _EFFECTS_URL, ["libbass_fx.dylib", "bass_fx.txt"]),
        ]
    folder = _LINUX_FOLDERS.get(machine.lower())
    if folder is None:
        raise SystemExit(f"BASS's Linux package has no {machine} build")
    return [
        ("bass24-linux.zip", _CORE_URL, [f"libs/{folder}/libbass.so", "bass.txt"]),
        ("bass_fx24-linux.zip", _EFFECTS_URL, [f"libs/{folder}/libbass_fx.so", "bass_fx.txt"]),
    ]


def verified(package: str, data: bytes) -> bytes:
    """``data`` if it's the package that was pinned, else stop and say what to do."""
    digest = hashlib.sha256(data).hexdigest()
    if digest != _PINS[package]:
        raise SystemExit(
            f"{package} isn't the version genericMud was tested with (SHA-256 {digest}). "
            "un4seen has probably released a new one: check its changes, then update the pin "
            "in tools/fetch_bass.py."
        )
    return data


def fetch(destination: Path, system: str = sys.platform, machine: str = "") -> list[Path]:
    """Download, check and unpack this platform's BASS files into ``destination``."""
    machine = machine or platform_module.machine()
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    for package, base, members in packages(system, machine):
        with urllib.request.urlopen(base + package, timeout=60) as response:
            data = verified(package, response.read())
        archive = zipfile.ZipFile(io.BytesIO(data))
        for member in members:
            target = destination / Path(member).name
            target.write_bytes(archive.read(member))
            written.append(target)
    return written


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python tools/fetch_bass.py <folder>", file=sys.stderr)
        return 2
    for path in fetch(Path(argv[1])):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
