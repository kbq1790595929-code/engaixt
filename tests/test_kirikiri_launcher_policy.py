from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from core.kirikiri_launcher_policy import (
    KIRIKIRI_REALTIME_LAUNCHER_NAME,
    KIRIKIRI_STATIC_LAUNCHER_NAME,
    find_kirikiri_static_launcher,
    has_active_kirikiri_root_patch,
    kirikiri_launcher_route,
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


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("marker", [
    "core.translation_overlay_window",
    "KIRIKIRI_NATIVE_HOOK_PROFILE",
    "kirikiri_native_launcher.exe",
    "run_kirikiri_realtime.py",
    "KrkrPatchLoader.exe",
])
def test_legacy_realtime_launcher_is_never_static(tmp_path, marker, encoded):
    launcher = tmp_path / KIRIKIRI_STATIC_LAUNCHER_NAME
    script = f"Write-Host '{marker}'"
    if encoded:
        payload = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        script = f'powershell -EncodedCommand "{payload}"'
    launcher.write_text("@echo off\n" + script, encoding="utf-8")

    assert kirikiri_launcher_route(launcher) == "realtime"
    assert find_kirikiri_static_launcher(tmp_path) is None


def test_static_launcher_is_selected_even_with_realtime_beside_it(tmp_path):
    static = tmp_path / KIRIKIRI_STATIC_LAUNCHER_NAME
    static.write_text('@echo off\nstart "" Game.exe\n', encoding="ascii")
    realtime = tmp_path / KIRIKIRI_REALTIME_LAUNCHER_NAME
    realtime.write_text("@echo off\n", encoding="ascii")

    assert kirikiri_launcher_route(static) == "static"
    assert kirikiri_launcher_route(realtime) == "realtime"
    assert find_kirikiri_static_launcher(tmp_path) == static


@pytest.mark.parametrize("content", ["", "powershell -EncodedCommand !!!", "powershell -EncodedCommand QQ=="])
def test_invalid_launcher_fails_closed(tmp_path, content):
    launcher = tmp_path / KIRIKIRI_STATIC_LAUNCHER_NAME
    launcher.write_text(content, encoding="ascii")

    assert kirikiri_launcher_route(launcher) == "unknown"
    assert find_kirikiri_static_launcher(tmp_path) is None


def test_missing_or_unreadable_launcher_fails_closed(tmp_path, monkeypatch):
    assert find_kirikiri_static_launcher(tmp_path) is None
    launcher = tmp_path / KIRIKIRI_STATIC_LAUNCHER_NAME
    launcher.write_text("@echo off\n", encoding="ascii")

    def deny_read(*_args, **_kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "read_text", deny_read)
    assert kirikiri_launcher_route(launcher) == "unknown"
    assert find_kirikiri_static_launcher(tmp_path) is None
