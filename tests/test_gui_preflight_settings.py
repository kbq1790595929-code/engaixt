"""Contracts for optional selection-time extraction and estimate rendering."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parent.parent


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8-sig")


def test_preflight_setting_is_visible_and_extension_is_loaded_after_main_gui():
    html = _text("web/index.html")

    assert 'name="selection_preflight_enabled"' in html
    assert "选择游戏后自动预检并显示预估费用" in html
    assert "预检只解包、提取和计数，不翻译、不扣费" in html
    assert html.index('src="app.js"') < html.index('src="preflight_settings.js"')


def test_preflight_scheduler_is_opt_in_and_progress_does_not_finish_translation():
    source = _text("web/preflight_settings.js")

    assert "selection_preflight_enabled: false" in source
    assert "if (!runtimeConfig.selection_preflight_enabled) return;" in source
    assert 'Boolean(detail?.preflight)' in source
    assert 'String(step || "").startsWith("preflight_")' in source
    assert "if (!isPreflight) return baseOnProgress(step, pct, detail);" in source
    assert "setTimeout(hideStats" not in source


def test_extraction_stats_reject_stale_path_before_replacing_current_estimate():
    source = _text("web/preflight_settings.js")

    assert "function normalizeGamePath" in source
    assert "function matchesSelectedGame" in source
    assert "if (!matchesSelectedGame(stats.path)) return;" in source
    extraction_handler = source.split('if (key === "extraction_stats") {', 1)[1].split(
        "baseOnMeta(key, value);", 1
    )[0]
    assert extraction_handler.index("if (!matchesSelectedGame(stats.path)) return;") < extraction_handler.index(
        "renderExtractionStats(stats);"
    )
    assert "refreshMainCostEstimate(stats.path || result.path" in source
