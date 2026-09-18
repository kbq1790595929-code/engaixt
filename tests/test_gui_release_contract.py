from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


ROOT = Path(__file__).parent.parent
INDEX = ROOT / "web" / "index.html"
APP_JS = ROOT / "web" / "app.js"
STYLE = ROOT / "web" / "style.css"
LOCAL_MODEL_JS = ROOT / "web" / "local_model_settings.js"
LOCAL_MODEL_CSS = ROOT / "web" / "local_model_settings.css"
USAGE_JS = ROOT / "web" / "usage_statistics.js"
USAGE_CSS = ROOT / "web" / "usage_statistics.css"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_help_button_and_engine_help_navigation_exist():
    html = _text(INDEX)

    assert 'class="help-trigger"' in html
    assert "openHelp()" in html
    assert 'id="help-overlay"' in html
    assert 'class="help-nav"' in html

    expected_ids = [
        "help-engine-bgi",
        "help-engine-rpgmaker",
        "help-engine-renpy",
        "help-engine-xunity",
        "help-engine-unity-arch",
        "help-engine-unity-assets",
        "help-engine-godot-pck",
        "help-engine-godot-frida",
        "help-engine-godot-plain",
        "help-engine-gamemaker",
        "help-engine-tyrano",
        "help-engine-kirikiri",
        "help-engine-unreal",
        "help-engine-wolf",
        "help-engine-generic",
    ]
    for engine_id in expected_ids:
        assert f"id=\"{engine_id}\"" in html
        assert f"jumpHelpEngine('{engine_id}'" in html


def test_tyrano_help_explains_experimental_static_pipeline():
    html = _text(INDEX)
    i18n = _text(ROOT / "web" / "i18n.js")

    card = html.split('id="help-engine-tyrano"', 1)[1].split('id="help-engine-kirikiri"', 1)[0]
    assert "TyranoScript / TyranoBuilder" in card
    assert "实验性" in card
    assert "开始翻译" in card
    assert "exe 内嵌 ZIP" in card
    assert "卸载汉化" in card
    assert "Only one real game" in i18n
    assert "実ゲームでの完了例はまだ 1 本のみ" in i18n


def test_gui_branding_uses_engaixt_consistently():
    html = _text(INDEX)
    main_py = _text(ROOT / "main.py")
    app_py = _text(ROOT / "app.py")

    assert "<title>EngAixt</title>" in html
    assert '<div class="brand-name">EngAixt</div>' in html
    assert '<div class="brand-sub">游戏翻译工具</div>' in html
    assert "<h1>EngAixt</h1>" in html
    assert '"游戏一键汉化工具"' not in html
    assert '"Game Translator"' not in html
    assert '"EngAixt"' in main_py
    assert '"EngAixt"' in app_py


def test_engine_help_has_left_nav_and_selected_state_styles():
    css = _text(STYLE)

    assert ".help-layout" in css
    assert ".help-nav" in css
    assert ".help-nav button.active" in css
    assert ".engine-help-card.selected" in css
    assert "@media (max-width: 760px)" in css


def test_engine_banner_only_shows_engine_category():
    html = _text(INDEX)
    js = _text(APP_JS)

    assert 'id="eng-caps"' in html
    assert 'id="eng-limits"' in html
    assert "document.getElementById(\"eng-name\").textContent = info.label" in js
    assert "badge.textContent = _supportLevelLabel(level)" in js
    assert "function _engineCapabilityLabels(capabilities)" not in js
    assert "info.capabilities" not in js


def test_help_navigation_selects_cards_without_reloading_page():
    js = _text(APP_JS)

    body = re.search(r"function jumpHelpEngine\(id, button, instant\) \{(?P<body>.*?)\n\}", js, re.S)
    assert body, "jumpHelpEngine should remain a local modal navigation helper"
    source = body.group("body")
    assert "help-scroll" in source
    assert "button.classList.add(\"active\")" in source
    assert "target.classList.add(\"selected\")" in source
    assert "scroll.scrollTo" in source






def test_no_paid_tier_or_sponsorship_ui_remains():
    """EngAixt is free software: no paid tier, licence UI or donation entry."""
    html = _text(INDEX)
    js = _text(APP_JS)
    css = _text(STYLE)
    app_py = _text(ROOT / "app.py")
    i18n = _text(ROOT / "web" / "i18n.js")

    for item in [
        # licence / quota UI
        'id="license-pill"',
        'id="license-activation-overlay"',
        'id="upgrade-link"',
        'id="renew-link"',
        "function refreshLicenseStatus",
        "function activateMonthlyMember",
        "def activate_monthly_member",
        ".license-activation-content",
        # sponsorship entry
        'id="sponsor-link"',
        "openSupportLink('sponsor')",
        "ifdian.net",
        "\u8d5e\u52a9\u4f5c\u8005",
        "Sponsor the author",
    ]:
        assert item not in html, item
        assert item not in js, item
        assert item not in css, item
        assert item not in app_py, item
        assert item not in i18n, item

    # Bug reports go to GitHub Issues; no personal address is shipped.
    assert "mailto:" not in app_py
    assert "mailto:" not in js




def test_api_keys_are_hidden_by_default_with_visibility_toggle():
    html = _text(INDEX)
    js = _text(APP_JS)
    css = _text(STYLE)

    for name in [
        "openai_api_key",
        "deepseek_api_key",
        "qwen_api_key",
        "zhipu_api_key",
        "moonshot_api_key",
        "doubao_api_key",
        "anthropic_api_key",
    ]:
        assert f'type="password" class="mono" name="{name}"' in html
        assert f"toggleSecretInput('{name}')" in html

    assert 'id="i-eye"' in html
    assert 'id="i-eye-off"' in html
    assert 'class="secret-toggle"' in html
    assert "function toggleSecretInput(name)" in js
    assert "function resetSecretInputs()" in js
    assert "setSecretInputVisible(input, false)" in js
    assert ".secret-input" in css
    assert '.field input[type="password"]' in css


def test_editable_inputs_have_custom_context_menu_for_paste():
    js = _text(APP_JS)
    css = _text(STYLE)
    app_py = _text(ROOT / "app.py")

    assert "function installEditableContextMenu()" in js
    assert 'document.addEventListener("contextmenu"' in js
    assert "pywebview.api.get_clipboard_text" in js
    assert "pywebview.api.set_clipboard_text" in js
    assert 'data-action="paste"' in js
    assert "_replaceEditableSelection(input, text)" in js
    assert "_editableContextSelection" in js
    assert ".input-context-menu" in css
    assert "from core.clipboard_service import get_clipboard_text, set_clipboard_text" in app_py


def test_api_model_fields_are_version_selects_with_current_choices():
    html = _text(INDEX)

    expected_selects = [
        "openai_model",
        "deepseek_model",
        "qwen_model",
        "zhipu_model",
        "moonshot_model",
        "doubao_model",
        "anthropic_model",
    ]
    for name in expected_selects:
        assert f'<select name="{name}">' in html
        assert f'<input type="text" class="mono" name="{name}"' not in html

    assert html.count('value="__provider_default__"') == 7
    for default_label in ["ChatGPT", "DeepSeek", "通义千问", "智谱 GLM", "Kimi", "豆包", "Claude"]:
        assert f">{default_label}</option>" in html

    assert "cloudDisplayModelValue" in _text(ROOT / "web" / "cloud_model_settings.js")


def test_model_selects_preserve_unknown_saved_values():
    js = _text(APP_JS)

    assert "function setFormControlValue(el, val)" in js
    assert "function _selectHasValue(select, value)" in js
    assert "opt.textContent = text + \"（当前配置）\";" in js
    assert "setFormControlValue(el, val)" in js


def test_active_translator_select_uses_user_facing_labels():
    html = _text(INDEX)

    assert '<option value="deepseek">DeepSeek</option>' in html
    assert '<option value="qwen">通义千问</option>' in html
    assert '<option value="zhipu">智谱 GLM</option>' in html
    assert '<option value="moonshot">Kimi</option>' in html
    assert '<option value="doubao">豆包 / 火山方舟</option>' in html
    assert '<option value="hy_mt2">腾讯 Hy-MT2（离线）</option>' in html
    assert "<option>qwen</option>" not in html
    assert "<option>zhipu</option>" not in html
    assert "<option>moonshot</option>" not in html


def test_local_cost_estimate_cannot_keep_stale_cloud_amount():
    cloud_js = _text(ROOT / "web" / "cloud_model_settings.js")
    local_js = _text(LOCAL_MODEL_JS)

    assert "function handleTranslatorCostEstimateChanged(value)" in cloud_js
    assert "function _renderLocalCostEstimateImmediately(textCount)" in cloud_js
    assert "estimated_cost_cny: 0" in cloud_js
    assert "function _invalidateMainCostEstimate()" in cloud_js
    assert 'if (selected.provider === "hy_mt2") {' in cloud_js
    assert "_renderLocalCostEstimateImmediately(count);" in cloud_js
    assert "targetKey === currentKey" in cloud_js
    assert "if (requestSeq === _mainEstimateRequestSeq) _hideMainCostEstimate();" in cloud_js
    assert "handleTranslatorCostEstimateChanged(value)" in local_js


def test_hy_mt2_optional_component_controls_are_available():
    html = _text(INDEX)
    js = _text(LOCAL_MODEL_JS)
    css = _text(STYLE)
    app_py = _text(ROOT / "app.py")

    assert 'id="hy-mt2-component"' in html
    assert 'id="hy-mt2-install-btn"' in html
    assert 'id="hy-mt2-remove-btn"' in html
    assert "function refreshHyMt2Status()" in js
    assert "function installHyMt2()" in js
    assert "function removeHyMt2()" in js
    assert "pywebview.api.install_hy_mt2_component" in js
    assert "pywebview.api.remove_hy_mt2_component" in js
    assert ".hy-mt2-component" in css
    assert "def get_hy_mt2_status" in app_py
    assert "def install_hy_mt2_component" in app_py
    assert "def remove_hy_mt2_component" in app_py


def test_local_model_settings_are_separate_but_cloud_controls_remain_visible():
    html = _text(INDEX)
    js = _text(LOCAL_MODEL_JS)
    css = _text(LOCAL_MODEL_CSS)

    assert 'data-tab="api"' in html and ">云端模型</button>" in html
    assert 'data-tab="local-model"' in html and ">本地模型</button>" in html
    assert 'id="tab-local-model"' in html
    assert 'name="hy_mt2_batch_size"' in html
    assert 'name="hy_mt2_context_size"' in html
    assert 'name="hy_mt2_idle_timeout_seconds"' in html
    assert html.count('inputmode="numeric"') >= 2
    assert 'id="cloud-translation-settings" data-cloud-only' in html
    assert "cloudSettings.hidden = localSelected" not in js
    assert "control.disabled = localSelected" not in js
    assert "Local inference simply ignores these values" in js
    assert "#cloud-translation-settings[hidden]" in css


def test_concurrency_setting_has_clickable_warning():
    html = _text(INDEX)
    js = _text(APP_JS)
    css = _text(STYLE)

    assert 'name="max_concurrency"' in html
    assert 'onclick="showConcurrencyWarning()"' in html
    assert 'class="field-alert-btn"' in html
    assert "function showConcurrencyWarning()" in js
    assert "部分模型或代理线路可能卡死" in js
    assert ".field-alert-btn" in css


def test_translation_cache_settings_are_user_visible():
    html = _text(INDEX)
    css = _text(STYLE)
    app_py = _text(ROOT / "app.py")

    assert 'name="translation_cache_enabled"' in html
    assert 'name="translation_cache_auto_cleanup"' in html
    assert 'name="translation_cache_max_size_gb"' in html
    assert "开启缓存会更便宜" in html
    assert "翻译的游戏越多" in html
    assert "长期越省钱" in html
    assert "缓存上限（GB，默认 1）" in html
    assert "不会删除游戏目录、汉化文件或每个游戏的元数据" in html
    assert ".cache-setting-field" in css
    assert '"translation_cache_enabled": c.translation_cache_enabled' in app_py
    assert '"translation_cache_max_size_gb": c.translation_cache_max_size_gb' in app_py


def test_usage_statistics_navigation_api_and_view_exist():
    html = _text(INDEX)
    js = _text(USAGE_JS)
    app_py = _text(ROOT / "app.py")
    css = _text(USAGE_CSS)
    backend = _text(ROOT / "core" / "usage_statistics.py")

    assert 'id="nav-statistics"' in html
    assert "navTo('statistics')" in html
    assert 'id="statistics-page"' in html
    assert 'data-range="latest"' in html
    assert 'data-range="7d"' in html
    assert 'data-range="30d"' in html
    assert 'data-range="90d"' in html
    assert 'data-range="all"' in html
    assert 'id="usage-total-tokens"' in html
    assert 'id="usage-display-cost"' in html
    assert 'id="usage-trend"' in html
    assert 'id="usage-runs"' in html
    assert "作品名" in html
    assert "只记录完整翻译成功的作品" in html
    assert 'src="usage_statistics.js"' in html
    assert 'href="usage_statistics.css"' in html
    assert "get_usage_statistics" in js
    assert "function renderUsageStatistics" in js
    assert "function selectUsageRange" in js
    assert "def get_usage_statistics" in app_py
    assert ".statistics-page-inner" in css
    assert ".usage-kpis" in css
    assert ".usage-trend" in css
    assert "statistics-loading-indicator" in html
    assert "_usageLoading" in js
    assert "if (!_isStatisticsPageActive()) return;" in js
    assert "#statistics-page.active .statistics-page-inner" in css
    assert 'class="statistics-empty-mark"' in html
    assert "@keyframes usage-bar-grow" in css
    assert "@keyframes usage-column-grow" in css
    assert "prefers-reduced-motion" in css
    assert "successful_runs" in backend
    assert "usage_statistics_v2.db" in backend
    assert "usage_statistics.db" in backend
    assert "status === \"failed\"" not in js
    assert "status === \"running\"" not in js
