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
import os as _os
# 与历史发布包保持一致：部分原生工具需要 VS2010 运行时
if _os.path.exists('_internal/MSVCR100.dll'):
    binaries.append(('_internal/MSVCR100.dll', '.'))
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


# Icon and version resource both come from files in the repo root.
# tools\release\release.ps1 regenerates version_info.txt for each build so the
# packaged EngAixt.exe reports a real product name and version instead of blanks.
import os as _os
_icon = 'web/app.ico' if _os.path.exists('web/app.ico') else None
_version = 'version_info.txt' if _os.path.exists('version_info.txt') else None


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
    icon=_icon,
    version=_version,
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
