"""GUI selection preflight: extract text and cost metadata without translating."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from core.cloud_model_catalog import game_text_stats
from core.path_resolver import resolve_game_path
from core.pipeline import Pipeline


REALTIME_ONLY_ENGINES = frozenset({
    "unity",
    "xunity_realtime",
    "unity_arch000_lua",
})


def is_realtime_only_engine(name: str | None) -> bool:
    return str(name or "").strip().lower() in REALTIME_ONLY_ENGINES


def _emit_meta(callback: Callable[[str, object], None] | None, key: str, value) -> None:
    if callback:
        callback(key, value)


def _emit_progress(callback: Callable[[str, float], None] | None, step: str, pct: float) -> None:
    if callback:
        callback(step, pct)


def preflight_extract(
    input_path: str,
    *,
    engine_name: str = "",
    progress_callback: Callable[[str, float], None] | None = None,
    meta_callback: Callable[[str, object], None] | None = None,
) -> bool:
    """Prepare extraction metadata for the selected game.

    A usable checkpoint is read without touching game assets. Otherwise the
    normal checkpoint pipeline runs in extract-only mode, which never calls a
    translator, spends quota, patches files, or launches the game.
    """
    resolved = Path(resolve_game_path(input_path))
    if is_realtime_only_engine(engine_name):
        _emit_meta(meta_callback, "preflight_result", {
            "status": "skipped",
            "reason": "realtime_only_engine",
        })
        _emit_progress(progress_callback, "preflight_skipped", 100)
        return True

    existing = game_text_stats(resolved)
    if existing.get("available"):
        stats = {
            "text_count": int(existing.get("text_count") or 0),
            "source_chars": int(existing.get("source_chars") or 0),
            "file_count": int(existing.get("file_count") or 0),
            "preflight_reused": True,
            "source": str(existing.get("source") or ""),
        }
        _emit_meta(meta_callback, "extraction_stats", stats)
        _emit_meta(meta_callback, "preflight_result", {
            "status": "success",
            "reused": True,
            "stats": stats,
        })
        _emit_progress(progress_callback, "preflight_complete", 100)
        return True

    captured_stats: dict = {}

    def pipeline_meta_callback(key: str, value) -> None:
        if key == "extraction_stats" and isinstance(value, dict):
            captured_stats.clear()
            captured_stats.update(value)
        _emit_meta(meta_callback, key, value)

    pipeline = Pipeline(
        progress_callback=progress_callback,
        meta_callback=pipeline_meta_callback,
    )
    success = pipeline.run_with_checkpoint(
        str(resolved),
        launch=False,
        extract_only=True,
    )
    if captured_stats:
        stats = {
            "available": True,
            "text_count": int(captured_stats.get("text_count") or 0),
            "source_chars": int(captured_stats.get("source_chars") or 0),
            "file_count": int(captured_stats.get("file_count") or 0),
            "source": "preflight_extraction",
        }
    else:
        stats = game_text_stats(resolved)
    if success and stats.get("available"):
        _emit_meta(meta_callback, "extraction_stats", {
            "text_count": int(stats.get("text_count") or 0),
            "source_chars": int(stats.get("source_chars") or 0),
            "file_count": int(stats.get("file_count") or 0),
            "preflight_reused": False,
            "source": str(stats.get("source") or ""),
        })
    _emit_meta(meta_callback, "preflight_result", {
        "status": "success" if success else "failed",
        "reused": False,
        "stats": stats if stats.get("available") else {},
    })
    return bool(success)
