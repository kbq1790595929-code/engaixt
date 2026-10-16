from __future__ import annotations

import sys
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

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
        dist / "assets" / "SourceHanSansCN-Regular.otf",
        dist / "assets" / "SOURCE_HAN_SANS_LICENSE.txt",
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


@pytest.mark.parametrize("clean_old_parts", [False, True])
def test_release_preserves_metadata_unless_cleanup_is_requested(tmp_path, clean_old_parts):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is required for the release script contract")
    script = (ROOT / "tools" / "release" / "release.ps1").read_text(encoding="utf-8")
    section = '$dlDir = Join-Path $DeployDir "downloads"' + script.split(
        '$dlDir = Join-Path $DeployDir "downloads"', 1
    )[1].split('$common =', 1)[0]
    deploy = tmp_path / "site"
    downloads = deploy / "downloads"
    downloads.mkdir(parents=True)
    old_files = [downloads / f"EngAixt-1.1.17-Windows-x64.zip{suffix}"
                 for suffix in (".manifest.json", ".sha256")]
    for path in old_files:
        path.write_text("old-release-metadata", encoding="utf-8")
    preamble = """
$ErrorActionPreference = 'Stop'
$DeployDir = $env:ENGAIXT_TEST_DEPLOY
$Version = '1.1.18'
$pkgName = 'EngAixt-1.1.18-Windows-x64'
$GitHubRepo = 'example/project'
$zipItem = [pscustomobject]@{Length = 64}
$zipSha = 'a' * 64
$utf8 = New-Object System.Text.UTF8Encoding($false)
$CleanOldParts = $env:ENGAIXT_TEST_CLEAN -eq 'true'
function Say([string]$Text, [string]$Color) {}
function Assert-ChildPath([string]$PathText, [string]$RootText) {
    $target = [System.IO.Path]::GetFullPath($PathText)
    $root = [System.IO.Path]::GetFullPath($RootText).TrimEnd('\\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $target.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Release operation escaped the isolated test directory'
    }
}
"""
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", "-"],
        input=preamble + section, text=True, encoding="utf-8", capture_output=True, timeout=30,
        env={**os.environ, "ENGAIXT_TEST_DEPLOY": str(deploy),
             "ENGAIXT_TEST_CLEAN": str(clean_old_parts).lower()},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for path in old_files:
        assert path.exists() is not clean_old_parts
        if path.exists():
            assert path.read_text("utf-8") == "old-release-metadata"
    current = downloads / "EngAixt-1.1.18-Windows-x64.zip.manifest.json"
    manifest = json.loads(current.read_text("utf-8-sig"))
    assert manifest["version"] == "1.1.18"
    assert manifest["sha256"] == "a" * 64
    assert manifest["parts"][0]["path"].startswith("https://github.com/example/project/")
