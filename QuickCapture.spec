# -*- mode: python ; coding: utf-8 -*-
import glob
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('gphoto2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Bundle libgphoto2 camera drivers (camlibs) and I/O drivers (iolibs)
for so in glob.glob('/opt/homebrew/lib/libgphoto2/2.5.33/*.so'):
    binaries.append((so, 'libgphoto2/2.5.33'))
for so in glob.glob('/opt/homebrew/lib/libgphoto2_port/0.12.2/*.so'):
    binaries.append((so, 'libgphoto2_port/0.12.2'))


a = Analysis(
    ['quickcapture.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['rthook_gphoto2.py'],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='QuickCapture',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name='QuickCapture',
)
app = BUNDLE(
    coll,
    name='QuickCapture.app',
    icon=None,
    bundle_identifier=None,
)
