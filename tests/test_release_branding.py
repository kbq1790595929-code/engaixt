from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tools.release.verify_branding import _ico_frames, verify_web_assets


ROOT = Path(__file__).parent.parent
BRANDING = json.loads((ROOT / "tools" / "release" / "branding.json").read_text(encoding="utf-8"))


def _copy_branding(root):
    (root / "web").mkdir()
    for name in ("app.ico", "logo.png", "index.html", "style.css"):
        shutil.copy2(ROOT / "web" / name, root / "web" / name)


def test_source_branding_matches_approved_115_release():
    verify_web_assets(ROOT, BRANDING)
    assert _ico_frames(ROOT / "web" / "app.ico")


@pytest.mark.parametrize("name", ["app.ico", "logo.png"])
def test_branding_gate_rejects_replaced_or_missing_assets(tmp_path, name):
    _copy_branding(tmp_path)
    target = tmp_path / "web" / name
    target.write_bytes(b"stale asset")
    with pytest.raises(ValueError, match="Branding differs"):
        verify_web_assets(tmp_path, BRANDING)
    target.unlink()
    with pytest.raises(ValueError, match="Branding differs"):
        verify_web_assets(tmp_path, BRANDING)


def test_branding_gate_rejects_gui_fallback_svg(tmp_path):
    _copy_branding(tmp_path)
    html = tmp_path / "web" / "index.html"
    html.write_text(
        html.read_text(encoding="utf-8").replace(
            '<img src="logo.png" alt="EngAixt">',
            '<svg class="ic"><use href="#i-spark"/></svg>',
        ), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="fallback SVG"):
        verify_web_assets(tmp_path, BRANDING)


def test_release_script_checks_source_and_built_executable_branding():
    source = (ROOT / "tools" / "release" / "release.ps1").read_text(encoding="utf-8")
    assert "verify_branding.py" in source
    assert "& python $BrandingCheck --source-root $SourceRoot" in source
    assert "--dist $Dist" in source
    assert "icon='web/app.ico'" in (ROOT / "game-translator.spec").read_text(encoding="utf-8")
