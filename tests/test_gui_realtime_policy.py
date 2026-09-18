from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import Api


ROOT = Path(__file__).parent.parent


def test_start_translation_allows_kirikiri_static_route():
    api = Api()
    calls = []
    api._do_run = lambda *args, **kwargs: calls.append((args, kwargs))

    with patch("app._detect_engine_policy", return_value={"name": "kirikiri", "label": "吉里吉里"}):
        assert api.run_translation("game", injector="", coverage=100) is True

    with patch("app._detect_engine_policy", return_value={"name": "xunity_realtime", "label": "Unity 运行时注入"}):
        assert api.run_translation("game", injector="", coverage=100) is False

    assert calls == [(('game',), {'injector': '', 'coverage': 100})]


def test_start_translation_allows_non_realtime_only_engines():
    api = Api()
    calls = []
    api._do_run = lambda *args, **kwargs: calls.append((args, kwargs))

    with patch("app._detect_engine_policy", return_value={"name": "bgi", "label": "BGI"}):
        assert api.run_translation("game", injector="", coverage=80) is True

    assert calls == [(("game",), {"injector": "", "coverage": 80})]


def test_active_task_end_does_not_refresh_removed_license_status():
    api = Api()
    calls = []
    api._js = lambda code: calls.append(code)

    api._begin_active_task()
    api._end_active_task()

    assert not any("refreshLicenseStatus" in code for code in calls)


def test_web_start_translation_prompts_for_unity_but_allows_krkr():
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "function _isRealtimeOnlyEngine" in source
    assert '"kirikiri"' not in source.split('function _isRealtimeOnlyEngine', 1)[1].split('function _realtimeOnlyMessage', 1)[0]
    assert '"unity"' in source
    assert '"xunity_realtime"' in source
    assert '"unity_arch000_lua"' in source
    assert "alert(msg)" in source
    assert "pywebview.api.run_translation" in source


def test_help_describes_krkr_static_first_and_unity_realtime_only():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    i18n = (ROOT / "web" / "i18n.js").read_text(encoding="utf-8")

    assert "KRKR 点击“开始翻译”会先走静态预检" in html
    assert "运行时后备入口" in html
    assert "GUI 实时翻译会直接拉起 native runtime 和字幕窗口，不再经过 bat / PowerShell" in html
    assert "只能点击“实时翻译”。工具会部署 BepInEx、XUnity.AutoTranslator" in html
    assert "KiriKiri first runs static preflight" in i18n
