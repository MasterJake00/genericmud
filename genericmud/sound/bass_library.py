"""The BASS audio library, loaded through ctypes, so genericMud plays sound the way packs expect.

BASS is the library soundpacks are made for: nearly every MUSHclient pack in the Soundpack
Vault ships bass.dll, and the ones that drive it directly (Miriani-Next, Toastush, Chatpack,
the Yu-Gi-Oh pack) use its pitch, slides, effects and timing. genericMud plays through BASS
when it can, and through pygame when it can't (no BASS in this build, or no audio device).

genericMud's builds carry the library files in a "bass" folder; a development checkout finds
them in the folder the GENERICMUD_BASS_DIR environment variable names (see
tools/fetch_bass.py). BASS is free for non-commercial use; un4seen.com has its licence.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

ENVIRONMENT_FOLDER = "GENERICMUD_BASS_DIR"  # a development checkout's BASS files
BUNDLED_FOLDER = "bass"  # where the builds put them, inside the frozen app
_VERSION_2_4 = 0x0204  # BASS_GetVersion's high word: every 2.4 release is binary compatible

# Flags, attributes and states, from bass.h.
SAMPLE_LOOP = 4
SAMPLE_FLOAT = 0x100
STREAM_AUTOFREE = 0x40000
STREAM_DECODE = 0x200000
UNICODE = 0x80000000
ATTRIB_FREQ = 1
ATTRIB_VOL = 2
ATTRIB_PAN = 3
ACTIVE_STOPPED = 0
ACTIVE_PLAYING = 1
ACTIVE_STALLED = 2
ACTIVE_PAUSED = 3
SYNC_POS = 0
SYNC_END = 2
SYNC_MIXTIME = 0x40000000
POS_BYTE = 0
POS_END = 0x10  # where a stream ends (each time round, when it loops)
POS_LOOP = 0x11  # where a looping stream starts again
CONFIG_DEV_DEFAULT = 36
ERROR_ALREADY = 14
DEVICE_DEFAULT = -1
DEVICE_NO_SOUND = 0

if sys.platform == "win32":
    _LIBRARY_NAMES = ("bass.dll", "bass_fx.dll")
    # BASS calls back with the Windows calling convention there.
    SyncProc = ctypes.WINFUNCTYPE(
        None, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p
    )
elif sys.platform == "darwin":
    _LIBRARY_NAMES = ("libbass.dylib", "libbass_fx.dylib")
    SyncProc = ctypes.CFUNCTYPE(
        None, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p
    )
else:
    _LIBRARY_NAMES = ("libbass.so", "libbass_fx.so")
    SyncProc = ctypes.CFUNCTYPE(
        None, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p
    )

_DWORD = ctypes.c_uint32
_QWORD = ctypes.c_uint64
_BOOL = ctypes.c_int
# Each function genericMud calls: (name, result type, argument types).
_SIGNATURES = (
    ("BASS_GetVersion", _DWORD, ()),
    ("BASS_ErrorGetCode", ctypes.c_int, ()),
    ("BASS_SetConfig", _BOOL, (_DWORD, _DWORD)),
    ("BASS_Init", _BOOL, (ctypes.c_int, _DWORD, _DWORD, ctypes.c_void_p, ctypes.c_void_p)),
    ("BASS_StreamCreateFile", _DWORD, (_BOOL, ctypes.c_void_p, _QWORD, _QWORD, _DWORD)),
    ("BASS_StreamFree", _BOOL, (_DWORD,)),
    ("BASS_ChannelPlay", _BOOL, (_DWORD, _BOOL)),
    ("BASS_ChannelStop", _BOOL, (_DWORD,)),
    ("BASS_ChannelIsActive", _DWORD, (_DWORD,)),
    ("BASS_ChannelSetAttribute", _BOOL, (_DWORD, _DWORD, ctypes.c_float)),
    ("BASS_ChannelGetAttribute", _BOOL, (_DWORD, _DWORD, ctypes.POINTER(ctypes.c_float))),
    ("BASS_ChannelFlags", _DWORD, (_DWORD, _DWORD, _DWORD)),
    ("BASS_ChannelSetSync", _DWORD, (_DWORD, _DWORD, _QWORD, SyncProc, ctypes.c_void_p)),
    ("BASS_ChannelGetData", _DWORD, (_DWORD, ctypes.c_void_p, _DWORD)),
    ("BASS_ChannelGetLength", _QWORD, (_DWORD, _DWORD)),
    ("BASS_ChannelGetPosition", _QWORD, (_DWORD, _DWORD)),
    ("BASS_ChannelSetPosition", _BOOL, (_DWORD, _QWORD, _DWORD)),
    ("BASS_ChannelSeconds2Bytes", _QWORD, (_DWORD, ctypes.c_double)),
    ("BASS_ChannelBytes2Seconds", ctypes.c_double, (_DWORD, _QWORD)),
    ("BASS_ChannelSlideAttribute", _BOOL, (_DWORD, _DWORD, ctypes.c_float, _DWORD)),
)


class BassUnavailable(Exception):
    """BASS couldn't be loaded or started; the message says why."""


@dataclass(frozen=True)
class Bass:
    """The loaded library: BASS itself, BASS_FX when it loaded too, and where they came from."""

    core: object
    effects: object | None
    folder: Path


_loaded: Bass | None = None
_started = False
_lock = threading.Lock()  # sessions start in parallel; BASS loads and starts once


def library_folders() -> list[Path]:
    """Where to look for the BASS files, the most deliberate choice first."""
    folders = []
    named = os.environ.get(ENVIRONMENT_FOLDER)
    if named:
        folders.append(Path(named))
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        folders.append(Path(bundle) / BUNDLED_FOLDER)
    return folders


def load(folders: list[Path] | None = None) -> Bass:
    """BASS, loaded once per process from the first folder that has it.

    Raises :class:`BassUnavailable` when no folder has it or it won't load.
    """
    global _loaded
    with _lock:
        if _loaded is not None:
            return _loaded
        searched = folders if folders is not None else library_folders()
        core_name, effects_name = _LIBRARY_NAMES
        for folder in searched:
            core_path = folder / core_name
            if not core_path.is_file():
                continue
            core = _open_library(core_path)
            version = core.BASS_GetVersion()
            if version >> 16 != _VERSION_2_4:
                raise BassUnavailable(
                    f"{core_path} is BASS {version >> 24}.{(version >> 16) & 0xFF}, but "
                    "genericMud needs 2.4"
                )
            effects = None
            effects_path = folder / effects_name
            if effects_path.is_file():
                try:
                    effects = _open_library(effects_path, typed=False)
                except OSError:
                    effects = None  # the effects add-on is optional; playback works without it
            _loaded = Bass(core=core, effects=effects, folder=folder)
            return _loaded
        places = ", ".join(str(folder) for folder in searched) or "nowhere to look"
        raise BassUnavailable(f"no {core_name} found ({places})")


def start(bass: Bass, device: int = DEVICE_DEFAULT) -> None:
    """Start BASS's output on ``device``, once per process (the default device unless a test
    asks for BASS's silent one). Raises :class:`BassUnavailable` when there's no device."""
    global _started
    with _lock:
        if _started:
            return
        core = bass.core
        if sys.platform == "win32":
            # Follow the system's default device when it changes, as when a headset goes in.
            core.BASS_SetConfig(CONFIG_DEV_DEFAULT, 1)
        if not core.BASS_Init(device, 44100, 0, None, None):
            code = core.BASS_ErrorGetCode()
            if code != ERROR_ALREADY:
                raise BassUnavailable(f"no audio device BASS could open (BASS error {code})")
        _started = True


def _open_library(path: Path, *, typed: bool = True):
    """Load one library file. BASS goes in with its symbols visible to libraries loaded later,
    so BASS_FX finds the BASS it extends."""
    try:
        if sys.platform == "win32":
            library = ctypes.WinDLL(str(path))
        else:
            library = ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
    except OSError as error:
        raise BassUnavailable(f"{path} wouldn't load: {error}") from error
    if typed:
        for name, result, arguments in _SIGNATURES:
            function = getattr(library, name)
            function.restype = result
            function.argtypes = list(arguments)
    return library


def path_argument(file: str) -> tuple[object, int]:
    """A file name as BASS takes it, and the flag that says how: UTF-16 on Windows, so any
    name works there, and UTF-8 bytes everywhere else."""
    if sys.platform == "win32":
        return ctypes.c_wchar_p(file), UNICODE
    return ctypes.c_char_p(os.fsencode(file)), 0
