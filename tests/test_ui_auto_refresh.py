"""设置页打开时必须自动刷新云端模型列表与价格，空提示不占位。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _function_body(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    depth = 0
    index = source.index("{", start)
    for pos in range(index, len(source)):
        char = source[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[index:pos + 1]
    raise AssertionError(f"unterminated function: {name}")


def test_open_settings_refreshes_cloud_model_state():
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    body = _function_body(source, "openSettings")
    assert "refreshCloudModelState()" in body


def test_pywebview_ready_refreshes_cloud_model_state():
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    ready = source.split('window.addEventListener("pywebviewready"', 1)[1]
    assert "refreshCloudModelState()" in ready


def test_empty_settings_note_is_hidden():
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    assert ".settings-note:empty" in css
