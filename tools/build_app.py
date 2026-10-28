"""Build the frozen genericMud app for the current platform with PyInstaller.

One place for the freeze flags so Windows, macOS and Linux stay consistent: a windowed
(no-console) GUI, the bundled frontend + keymaps, the per-OS ``--add-data`` separator, and
only the hidden imports each platform actually needs. ``--windowed`` matters for
accessibility on every platform -- a stray console window steals screen-reader focus.

macOS ``--windowed`` produces ``dist/genericMud.app`` (a real bundle VoiceOver can drive);
Linux/Windows produce ``dist/genericMud/`` (a portable onedir). Packaging the result into a
release artifact (zip/tarball) is the caller's job -- see the CI workflows.

The Windows release also builds the ZipExtractor self-update helper; that stays in
build-windows.yml because it needs MSBuild. Run: ``python tools/build_app.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

APP_NAME = "genericMud"
ENTRY = "run_genericmud.py"
BASS_FOLDER = Path("build/bass")  # where tools/fetch_bass.py puts BASS for a build
NOTICES_FILE = Path("build/notices/THIRD-PARTY-NOTICES.txt")  # tools/third_party_notices.py
CONTENTS_FOLDER = Path("dist") / APP_NAME / "_internal"  # PyInstaller's, beside the exe
_LIBRARY_SUFFIXES = (".dll", ".so", ".dylib")
_NEVER_RUN_ON_WINDOWS = (
    "pygame.tests", "pygame.examples", "pygame.docs", "test", "tkinter", "pywin", "win32ui",
)


def pyinstaller_args(
    platform: str, bass_folder: Path | None = None, notices: Path | None = None
) -> list[str]:
    """The PyInstaller argv for ``platform`` (``sys.platform`` value).

    Pure so the freeze contract (windowed, bundled data, right separator, right hidden
    imports) can be asserted without actually running a multi-minute build. ``bass_folder``
    holds the BASS audio library the app plays through; its files go in the app's "bass"
    folder, where genericmud/sound/bass_library.py looks. ``notices`` is the licence notices
    file for Python and the libraries the app runs on, which goes in with the app.
    """
    separator = ";" if platform == "win32" else ":"  # PyInstaller --add-data host convention
    args = [
        "--noconfirm",
        "--onedir",
        "--name", APP_NAME,
        "--windowed",  # GUI subsystem, no console: a console window would grab SR focus
        # Bundle the installed dist-info so importlib.metadata.version() resolves in the
        # frozen build (the updater compares it against releases).
        "--copy-metadata", "genericmud",
        "--add-data", f"frontend{separator}frontend",
        "--add-data", f"genericmud/config/keymaps{separator}genericmud/config/keymaps",
        "--collect-all", "lupa",
        "--hidden-import", "websockets",
    ]
    if platform != "win32":
        args += ["--collect-all", "pygame"]
    # pywebview backs the optional --web UI; present on every platform's [gui] extra.
    args += ["--collect-all", "webview"]
    # prism is the self-voice backend on all three platforms. Collect-all because the speech
    # comes out of a native library the wheel ships alongside the Python modules (prism.dll /
    # libprism) -- a module-only scan would freeze an app that imports prism and then cannot
    # speak. It still misses the cffi bridge in prism/_native/, which hooks/hook-prism.py
    # collects; without that the frozen app cannot import prism at all.
    args += ["--collect-all", "prism"]
    args += ["--additional-hooks-dir", "hooks"]
    if platform == "win32":
        args += [  # the SAPI fallback backend talks COM
            "--hidden-import", "win32com.client",
            "--hidden-import", "pythoncom",
        ]
        # pygame (the fallback audio) comes in through its own hook, which brings its DLLs,
        # and not whole: collecting all of it shipped its 12 MB of docs, tests and examples,
        # and its tests pulled in Python's test package and with it Tcl/Tk. Pythonwin's MFC
        # windows only reach the build through a win32com tool that writes COM wrappers,
        # which the SAPI voice never runs.
        for module in _NEVER_RUN_ON_WINDOWS:
            args += ["--exclude-module", module]
    elif platform == "darwin":
        # A stable bundle id keeps preferences/TCC attribution across releases.
        args += ["--osx-bundle-identifier", "space.thealvernaz.genericmud"]
    # The Linux fallback is speech-dispatcher's `spd-say` (a runtime command, not a bundled
    # module), so there's nothing extra to collect there.
    if bass_folder is not None:
        for path in sorted(bass_folder.iterdir()):
            kind = "--add-binary" if path.suffix in _LIBRARY_SUFFIXES else "--add-data"
            args += [kind, f"{path}{separator}bass"]
    if notices is not None:
        args += ["--add-data", f"{notices}{separator}."]
    args.append(ENTRY)
    return args


def main() -> None:
    import PyInstaller.__main__
    import third_party_notices

    bass_folder = BASS_FOLDER if BASS_FOLDER.is_dir() else None
    if bass_folder is None:
        # The app still runs, through pygame, but packs lose what BASS gives them.
        print(f"Building without BASS: run tools/fetch_bass.py {BASS_FOLDER} first.")
    # The libraries' licences, read from this environment: the one the build freezes.
    notices = third_party_notices.write(NOTICES_FILE)
    PyInstaller.__main__.run(pyinstaller_args(sys.platform, bass_folder, notices))
    list_contents(CONTENTS_FOLDER)


def list_contents(contents: Path) -> None:
    """List the files of the built contents folder, so the app's first start after an update
    can delete the ones an earlier version left there (upgrade_manager.prune_leftovers).
    Nothing goes in the folder after this; the Windows workflow adds ZipExtractor.exe beside
    the app, outside it. A macOS .app is replaced whole, so it needs no list."""
    if not contents.is_dir():
        return
    from genericmud import __version__
    from genericmud.update.upgrade_manager import write_manifest

    write_manifest(contents, __version__)


if __name__ == "__main__":
    main()
