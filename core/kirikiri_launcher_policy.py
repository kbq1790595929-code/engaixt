"""Launch policy facts for KiriKiri static and realtime translation routes."""

from __future__ import annotations

import base64
import binascii
import json
import re
from pathlib import Path


KIRIKIRI_STATIC_LAUNCHER_NAME = "启动汉化版.bat"
KIRIKIRI_REALTIME_LAUNCHER_NAME = "启动实时汉化版.bat"

_RUNTIME_MARKERS = (
    "core.translation_overlay_window",
    "kirikiri_native_hook_profile",
    "kirikiri_native_launcher.exe",
    "run_kirikiri_realtime.py",
    "krkrpatchloader.exe",
)


def kirikiri_launcher_route(launcher: Path) -> str:
    """Classify portable launchers, including legacy encoded realtime batches.

    Unreadable or malformed scripts fail closed so static callers can safely
    fall back to the game's executable instead of an unknown launch route.
    """
    launcher = Path(launcher)
    if launcher.name.casefold() == KIRIKIRI_REALTIME_LAUNCHER_NAME.casefold():
        return "realtime"
    if launcher.suffix.casefold() not in {".bat", ".cmd", ".ps1"}:
        return "unknown"
    try:
        content = launcher.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return "unknown"
    if not content.strip():
        return "unknown"
    scripts = [content]
    # Generated batches keep Unicode paths in UTF-16LE PowerShell payloads.
    for match in re.finditer(r'-EncodedCommand\s+(?:"([^"]*)"|(\S+))', content, re.I):
        try:
            encoded = match.group(1) if match.group(1) is not None else match.group(2)
            scripts.append(base64.b64decode(encoded, validate=True).decode("utf-16le"))
        except (ValueError, binascii.Error, UnicodeError):
            return "unknown"
    combined = "\n".join(scripts).casefold()
    if any(marker in combined for marker in _RUNTIME_MARKERS):
        return "realtime"
    return "static"


def find_kirikiri_static_launcher(game_dir: Path) -> Path | None:
    launcher = Path(game_dir) / KIRIKIRI_STATIC_LAUNCHER_NAME
    if launcher.is_file() and kirikiri_launcher_route(launcher) == "static":
        return launcher
    return None


def preserve_legacy_kirikiri_realtime_launcher(game_dir: Path) -> None:
    """Preserve a legacy realtime batch before generating the static entry."""
    legacy = Path(game_dir) / KIRIKIRI_STATIC_LAUNCHER_NAME
    realtime = Path(game_dir) / KIRIKIRI_REALTIME_LAUNCHER_NAME
    if legacy.is_file() and not realtime.exists() and kirikiri_launcher_route(legacy) == "realtime":
        legacy.rename(realtime)


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
