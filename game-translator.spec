# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [
    ('build_data', 'data'),
    ('web', 'web'),
    ('assets', 'assets'),
    ('frida', 'frida'),
    ('frida_probe.js', '.'),
    ('engines/assets', 'engines/assets'),
    # Optional KRKR static sidecars are copied when present. The source tree
    # intentionally does not vendor third-party binaries by default.
    ('_internal/tools', '_internal/tools'),
    ('_internal/licenses', '_internal/licenses'),
]
binaries = []
hiddenimports = [
    'openai', 'numpy', 'requests', 'anthropic', 'lz4', 'py7zr',
    'tkinterdnd2', 'tkinter', 'Crypto', 'frida',
    'core.app_update',
    'core.usage_statistics',
    'translators.deepseek', 'translators.domestic',
    'translators.openai', 'translators.anthropic',
    'translators.hy_mt2', 'translators.hy_mt2_component',
    'translators.hy_mt2_runtime',
]
tmp_ret = collect_all('UnityPy')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tests'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='game-translator',
    icon='web/app.ico',
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='game-translator',
)
