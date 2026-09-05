"""WebView bridge for the optional selection-time extraction preflight."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import get_config
from core.gui_preflight import preflight_extract


def start_preflight_extract(api: Any, path: str, *, engine_name: str = "") -> dict:
    """Submit a no-AI preflight task when the user explicitly enabled it."""
    if not get_config().selection_preflight_enabled:
        return {"started": False, "reason": "disabled"}

    resolved = api.resolve_path(path)
    key = str(Path(resolved).resolve()).casefold()
    with api._preflight_lock:
        if key in api._preflight_paths:
            return {"started": False, "reason": "already_running"}
        api._preflight_paths.add(key)

    def _run(_task):
        def progress_cb(step: str, pct: float):
            detail = json.dumps({"preflight": True})
            api._js(f"on_progress({json.dumps(step)}, {float(pct):.2f}, {detail})")

        def meta_cb(meta_key: str, value):
            payload = value
            if meta_key in {"extraction_stats", "preflight_result"}:
                payload = dict(value) if isinstance(value, dict) else {"value": value}
                payload.setdefault("path", resolved)
            api._js(f"on_meta({json.dumps(meta_key)}, {json.dumps(payload, ensure_ascii=False)})")

        try:
            api._js("on_status('运行中')")
            api._js(f"on_log(20, {json.dumps('开始预检：解包并提取文本，不会开始翻译')})")
            success = preflight_extract(
                resolved,
                engine_name=str(engine_name or ""),
                progress_callback=progress_cb,
                meta_callback=meta_cb,
            )
            if success:
                api._js("on_log(20, '预检完成：已计算预估费用，等待用户开始翻译')")
                api._js("on_status('空闲')")
            else:
                api._js("on_log(50, '预检未通过：未开始 AI 翻译，原始游戏文件保持不变')")
                api._js("on_status('失败')")
            return bool(success)
        except Exception as exc:
            api._js(f"on_log(50, {json.dumps(f'预检提取失败: {exc}', ensure_ascii=False)})")
            api._js("on_status('失败')")
            return False
        finally:
            with api._preflight_lock:
                api._preflight_paths.discard(key)

    api._tasks.submit("预检提取", _run, kind="preflight", counted=True)
    return {"started": True, "path": resolved}
