from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_resource_path_uses_pyinstaller_meipass(monkeypatch, tmp_path):
    from core.resources import app_root, resource_path

    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert app_root() == tmp_path
    assert resource_path("web", "index.html") == tmp_path / "web" / "index.html"
    assert resource_path("assets", "bgi_native_runtime", "x86") == tmp_path / "assets" / "bgi_native_runtime" / "x86"
    assert resource_path("assets", "bgi_native_runtime", "x64") == tmp_path / "assets" / "bgi_native_runtime" / "x64"
    assert resource_path("assets", "kirikiri_native_runtime", "x86") == tmp_path / "assets" / "kirikiri_native_runtime" / "x86"


def test_release_spec_bundles_gui_runtime_and_engine_assets():
    spec = (ROOT / "game-translator.spec").read_text(encoding="utf-8")

    for required in [
        "('web', 'web')",
        "('assets', 'assets')",
        "('frida', 'frida')",
        "('frida_probe.js', '.')",
        "('engines/assets', 'engines/assets')",
        "'frida'",
        "'translators.deepseek'",
        "'translators.domestic'",
        "'translators.openai'",
        "'translators.anthropic'",
        "'core.usage_statistics'",
    ]:
        assert required in spec

    assert "excludes=['tests']" in spec
    assert "edition.txt" not in spec
    assert "edition.json" not in spec


def test_required_release_assets_exist():
    required_paths = [
        ROOT / "web" / "index.html",
        ROOT / "web" / "app.js",
        ROOT / "web" / "style.css",
        ROOT / "assets" / "SourceHanSansCN-Regular.otf",
        ROOT / "assets" / "source_han_sans_cn_cjk.fontdata",
        ROOT / "assets" / "SOURCE_HAN_SANS_LICENSE.txt",
        ROOT / "assets" / "bgi_native_runtime" / "x86" / "bgi_native_launcher.exe",
        ROOT / "assets" / "bgi_native_runtime" / "x86" / "bgi_native_hook.dll",
        ROOT / "assets" / "bgi_native_runtime" / "x64" / "bgi_native_launcher.exe",
        ROOT / "assets" / "bgi_native_runtime" / "x64" / "bgi_native_hook.dll",
        ROOT / "assets" / "kirikiri_native_runtime" / "x86" / "kirikiri_native_launcher.exe",
        ROOT / "assets" / "kirikiri_native_runtime" / "x86" / "kirikiri_native_hook.dll",
        ROOT / "frida" / "run_bgi_realtime.py",
        ROOT / "frida" / "bgi_realtime_hook.js",
        ROOT / "engines" / "assets" / "rpgmaker_hook.js",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required_paths if not path.exists()]
    assert not missing, f"missing release assets: {missing!r}"
    assert not list((ROOT / "assets").rglob("*private*.pem"))


def test_release_assets_do_not_include_known_proprietary_fonts():
    prohibited_fragments = [
        "simhei",
        "fangzheng",
        "fzlanting",
        "lanting",
        "方正",
        "兰亭",
    ]
    roots = [ROOT / "assets"]
    optional_roots = [
        ROOT / "build_data" / "assets",
        ROOT / "dist" / "game-translator" / "_internal" / "assets",
        ROOT / "dist" / "game-translator" / "_internal" / "data" / "assets",
    ]
    roots.extend(path for path in optional_roots if path.exists())

    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            lowered = path.name.lower()
            if any(fragment in lowered for fragment in prohibited_fragments):
                offenders.append(str(path.relative_to(ROOT)))

    assert not offenders, f"release contains non-open font assets: {offenders!r}"


def test_built_distribution_contains_release_assets_if_present():
    dist = ROOT / "dist" / "game-translator" / "_internal"
    if not dist.exists():
        return

    required_paths = [
        dist / "web" / "index.html",
        dist / "web" / "app.js",
        dist / "web" / "style.css",
        dist / "assets" / "bgi_native_runtime" / "x86" / "bgi_native_launcher.exe",
        dist / "assets" / "bgi_native_runtime" / "x86" / "bgi_native_hook.dll",
        dist / "assets" / "bgi_native_runtime" / "x64" / "bgi_native_launcher.exe",
        dist / "assets" / "bgi_native_runtime" / "x64" / "bgi_native_hook.dll",
        dist / "assets" / "kirikiri_native_runtime" / "x86" / "kirikiri_native_launcher.exe",
        dist / "assets" / "kirikiri_native_runtime" / "x86" / "kirikiri_native_hook.dll",
        dist / "frida" / "run_bgi_realtime.py",
        dist / "engines" / "assets" / "rpgmaker_hook.js",
    ]
    missing = [str(path.relative_to(dist)) for path in required_paths if not path.exists()]
    assert not missing, f"built distribution missing assets: {missing!r}"
