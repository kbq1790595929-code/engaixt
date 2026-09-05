"""Bounded GUI progress messages for KiriKiri static extraction."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any


_EXTRACT_PROGRESS_PERCENT = 20.0


def emit_extract_progress(engine: Any, message: str) -> None:
    """Publish an extraction detail without changing the pipeline stage."""
    callback = getattr(engine, "_progress", None)
    if not callable(callback):
        return
    try:
        callback(message, _EXTRACT_PROGRESS_PERCENT)
    except Exception:
        # GUI reporting must never affect extraction.
        return


def should_report_item(current: int, total: int, *, max_updates: int = 30) -> bool:
    """Keep large archives responsive without flooding the GUI bridge."""
    if total <= 1 or current in {1, total}:
        return True
    interval = max(1, math.ceil(total / max(1, max_updates)))
    return current % interval == 0


def archive_index_message(archive: Path, current: int, total: int) -> str:
    return f"KRKR：读取 {archive.name} 索引（{current}/{total}）"


def archive_script_message(archive: Path, current: int, total: int) -> str:
    return f"KRKR：提取 {archive.name} 脚本（{current}/{total}）"


def script_parse_message(relative_path: str, current: int, total: int) -> str:
    return f"KRKR：解析脚本文本（{current}/{total}）：{relative_path}"


def external_tool_message(tool: str, archive: Path, current: int, total: int) -> str:
    return f"KRKR：{tool} 尝试解包 {archive.name}（{current}/{total}）"
