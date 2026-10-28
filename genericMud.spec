# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata

datas = [('frontend', 'frontend'), ('genericmud/config/keymaps', 'genericmud/config/keymaps')]
binaries = []
hiddenimports = ['websockets', 'win32com.client', 'pythoncom']
datas += copy_metadata('genericmud')
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# collect_all gets prism's Python modules and libprism, but not the cffi bridge beside it --
# hooks/hook-prism.py picks that up, for this spec and for tools/build_app.py alike.
tmp_ret = collect_all('prism')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('lupa')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# BASS, the audio library the app plays through, into the app's "bass" folder where
# genericmud/sound/bass_library.py looks. tools/fetch_bass.py build/bass puts it here.
bass_folder = os.path.join(SPECPATH, 'build', 'bass')
if os.path.isdir(bass_folder):
    for name in sorted(os.listdir(bass_folder)):
        path = os.path.join(bass_folder, name)
        if name.endswith(('.dll', '.so', '.dylib')):
            binaries.append((path, 'bass'))
        else:
            datas.append((path, 'bass'))


a = Analysis(
    ['run_genericmud.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[os.path.join(SPECPATH, 'hooks')],
    hooksconfig={},
    runtime_hooks=[],
    # pygame comes in through its own hook (its DLLs, font and icon), not whole: its docs,
    # tests and examples were 12 MB, and the tests pulled in Python's test package and Tcl/Tk.
    # Pythonwin's MFC windows only arrive through win32com's wrapper writer, which SAPI never
    # runs. Kept the same as tools/build_app.py.
    excludes=['pygame.tests', 'pygame.examples', 'pygame.docs', 'test', 'tkinter', 'pywin',
              'win32ui'],
    noarchive=False,
    optimize=0,
)
# upx=False deliberately: UPX compression is a known antivirus false-positive amplifier,
# and PyInstaller only uses it when upx happens to be on PATH -- so leaving it on made a
# dev box's exe differ from CI's in the one dimension a blind user cannot debug. Nothing
# here needs the couple of MB.
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='genericMud',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='genericMud',
)
