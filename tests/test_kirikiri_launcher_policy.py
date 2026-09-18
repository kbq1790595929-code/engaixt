from __future__ import annotations

import json
from pathlib import Path

from core.kirikiri_launcher_policy import (
    has_active_kirikiri_root_patch,
    kirikiri_needs_patch_bridge,
)


def _write_diagnosis(game: Path, data: dict[str, object]) -> None:
    meta = game / "_translation_meta"
    meta.mkdir()
    (meta / "kirikiri_patch_diagnostics.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


def test_active_root_patch_requires_diagnosis_and_real_xp3_payload(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    _write_diagnosis(game, {"root_patch": True, "needs_patch_bridge": False})

    assert has_active_kirikiri_root_patch(game) is False
    assert kirikiri_needs_patch_bridge(game) is False

    (game / "patch.xp3").write_bytes(b"XP3\r\n \n\x1a\x8bg\x01")
    assert has_active_kirikiri_root_patch(game) is True


def test_bridge_or_invalid_patch_keeps_realtime_fallback_available(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    _write_diagnosis(game, {"root_patch": False, "needs_patch_bridge": True})
    (game / "patch.xp3").write_bytes(b"not an xp3")

    assert kirikiri_needs_patch_bridge(game) is True
    assert has_active_kirikiri_root_patch(game) is False
