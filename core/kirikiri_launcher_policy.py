"""Launch policy facts for KiriKiri static and realtime translation routes."""

from __future__ import annotations

import json
from pathlib import Path


def _read_patch_diagnosis(game_dir: Path) -> dict[str, object]:
    path = game_dir / "_translation_meta" / "kirikiri_patch_diagnostics.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def kirikiri_needs_patch_bridge(game_dir: Path) -> bool:
    """Return whether diagnostics require an EXE-specific stream bridge."""
    data = _read_patch_diagnosis(game_dir)
    return not (bool(data.get("root_patch")) and not bool(data.get("needs_patch_bridge")))


def has_active_kirikiri_root_patch(game_dir: Path) -> bool:
    """Require both a verified route decision and a present XP3 payload.

    Diagnostics alone can be stale after uninstall or a failed copy, so they
    are not enough to suppress the realtime fallback window.
    """
    data = _read_patch_diagnosis(game_dir)
    if not bool(data.get("root_patch")) or bool(data.get("needs_patch_bridge")):
        return False
    patch = game_dir / "patch.xp3"
    try:
        with patch.open("rb") as stream:
            return stream.read(3) == b"XP3"
    except OSError:
        return False
