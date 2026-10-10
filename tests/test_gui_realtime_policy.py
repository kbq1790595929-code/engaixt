from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import re
import shutil
import subprocess
import sys

import pytest

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


def test_krkr_gui_relays_specific_runtime_diagnostics(tmp_path, monkeypatch):
    api = Api()
    messages = []
    api._js = messages.append
    api.resolve_path = lambda path: str(tmp_path)
    monkeypatch.setattr("app._detect_engine_policy", lambda path: {
        "name": "kirikiri", "path": tmp_path, "engine": None,
    })
    monkeypatch.setattr("core.launcher.create_kirikiri_native_launcher", lambda *args, **kwargs: None)

    def fake_launch(path, *, engine, wait, on_log):
        assert wait is True
        on_log(40, "injection_failed pid=42 win32=5")
        return False

    monkeypatch.setattr("core.launcher.launch_kirikiri_native_runtime", fake_launch)
    monkeypatch.setattr(api._tasks, "submit", lambda name, run, **kwargs: run(None))
    assert api.run_realtime(str(tmp_path)) is True
    assert any("injection_failed pid=42 win32=5" in message for message in messages)
    assert not any("KRKR 实时翻译已结束" in message for message in messages)


def test_web_start_translation_prompts_for_unity_but_allows_krkr():
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "function _isRealtimeOnlyEngine" in source
    unity_only = re.search(r"function _isRealtimeOnlyEngine\(info\) \{.*?\n\}", source, re.S)
    assert unity_only
    assert '"kirikiri"' not in unity_only.group(0)
    assert '"unity"' in source
    assert '"xunity_realtime"' in source
    assert '"unity_arch000_lua"' in source
    assert "alert(msg)" in source
    assert "pywebview.api.run_translation" in source


def test_help_describes_krkr_static_first_and_unity_realtime_only():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    i18n = (ROOT / "web" / "i18n.js").read_text(encoding="utf-8")

    assert "KRKR 点击“开始翻译”只走静态预检" in html
    assert "独立实时入口" in html
    assert "游戏库使用静态入口，不启动字幕窗口" in html
    assert "启动实时汉化版.bat" in html
    assert "改用实时翻译" not in html
    assert "改用实时翻译" not in i18n
    assert "只能点击“实时翻译”。工具会部署 BepInEx、XUnity.AutoTranslator" in html
    assert "KiriKiri uses static preflight" in i18n


def test_realtime_button_is_hidden_initially_and_fallback_removed():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    assert re.search(r'<button[^>]+id="realtime-btn"[^>]+hidden', html)
    assert "krkr-fallback-btn" not in html + source
    assert html.count('onclick="startRealtime()"') == 1
    assert ".actions-btn[hidden] { display: none; }" in css
    for name in ("resetUI", "detectEngine", "_showEngine"):
        body = re.search(rf"(?:async )?function {name}\([^)]*\) \{{.*?\n\}}", source, re.S)
        assert body and "_updateRealtimeButton(" in body.group(0)


def test_realtime_button_behavior_for_supported_and_unsupported_engines():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for GUI JavaScript behavior tests")
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    names = ["_isRealtimeOnlyEngine", "_supportsRealtimeTranslation", "_updateRealtimeButton"]
    functions = []
    for name in names:
        match = re.search(rf"function {name}\(info\) \{{.*?\n\}}", source, re.S)
        assert match
        functions.append(match.group(0))
    script = """
const assert = require("node:assert/strict");
const button = {hidden: true, disabled: true};
global.document = {getElementById: id => id === "realtime-btn" ? button : null};
""" + "\n".join(functions) + """
for (const name of ["kirikiri", "unity", "xunity_realtime", "unity_arch000_lua"]) {
    _updateRealtimeButton({name});
    assert.equal(button.hidden, false, name);
    assert.equal(button.disabled, true, "detection must not enable a busy task button");
}
assert.equal(_isRealtimeOnlyEngine({name: "kirikiri"}), false);
for (const name of ["bgi", "rpgmaker", "gamemaker", "godot", "godot_pck", "wolf",
                    "renpy", "tyrano", "unreal", "generic", "unknown", "error", ""]) {
    _updateRealtimeButton({name: "kirikiri"});
    _updateRealtimeButton({name});
    assert.equal(button.hidden, true, name);
}
_updateRealtimeButton({name: "unity"});
_updateRealtimeButton(null);
assert.equal(button.hidden, true, "reset/detection pending must hide the button");
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_start_realtime_does_not_call_backend_for_unsupported_engine():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for GUI JavaScript behavior tests")
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    functions = []
    for name in ("_isRealtimeOnlyEngine", "_supportsRealtimeTranslation", "startRealtime"):
        match = re.search(rf"(?:async )?function {name}\([^)]*\) \{{.*?\n\}}", source, re.S)
        assert match
        functions.append(match.group(0))
    script = """
const assert = require("node:assert/strict");
let engine = "bgi";
const calls = [];
global.getPath = () => "game";
global.getEngineInfo = async () => ({name: engine});
global.document = {getElementById: () => ({value: "ja"})};
global.pywebview = {api: {run_realtime: (...args) => calls.push(args)}};
global._runAction = action => action();
global.t = s => s;
""" + "\n".join(functions) + """
(async () => {
    await startRealtime();
    assert.equal(calls.length, 0);
    engine = "kirikiri";
    await startRealtime();
    assert.equal(calls.length, 1);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
