# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['start.py'],
    pathex=[],
    binaries=[],
    datas=[('src', 'src')],
    hiddenimports=['truststore'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
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
    name='easyauth-emulator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='easyauth-emulator',
)

import shutil
shutil.copy2(
    'config.toml.example',
    str(DISTPATH) + '/easyauth-emulator/config.toml.example',
)
