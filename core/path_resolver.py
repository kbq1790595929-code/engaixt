from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from core.steam import resolve_steam_entry


@dataclass
class ShortcutTarget:
    target_path: str = ""
    arguments: str = ""
    working_directory: str = ""


def resolve_game_path(value: str | Path) -> str:
    """Resolve shortcuts and launch URIs to a filesystem path usable by engines."""
    path_text = _normalize_input(value)
    if not path_text:
        return ""

    path = Path(path_text)

    if path.exists() and not _is_indirect_launch_entry(path):
        return _normalize_output(path)

    steam_path = resolve_steam_entry(path_text)
    if steam_path:
        return _normalize_output(steam_path)

    if path.suffix.lower() == ".lnk" and path.is_file():
        shortcut = resolve_windows_shortcut(path)
        if shortcut:
            steam_text = f"{shortcut.target_path} {shortcut.arguments}"
            steam_path = resolve_steam_entry(steam_text)
            if steam_path:
                return _normalize_output(steam_path)
            if shortcut.target_path:
                return resolve_game_path(shortcut.target_path)

    if path.suffix.lower() == ".url" and path.is_file():
        return _normalize_output(path)

    if path.suffix.lower() == ".exe" and path.is_file():
        return _normalize_output(path)

    if path.name.lower() == "steam_appid.txt" and path.is_file():
        return _normalize_output(path.parent)

    return _normalize_output(path_text)


def _is_indirect_launch_entry(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in {".lnk", ".url", ".acf"} or path.name.lower() == "steam_appid.txt"


def resolve_windows_shortcut(path: Path) -> ShortcutTarget | None:
    """Resolve a .lnk file using the Windows Script Host COM object."""
    if os.name != "nt":
        return None
    ps_path = str(path).replace("'", "''")
    script = (
        f"$p='{ps_path}';"
        "$w=New-Object -ComObject WScript.Shell;"
        "$s=$w.CreateShortcut($p);"
        "[pscustomobject]@{"
        "TargetPath=$s.TargetPath;"
        "Arguments=$s.Arguments;"
        "WorkingDirectory=$s.WorkingDirectory"
        "} | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except Exception:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return ShortcutTarget(
        target_path=str(data.get("TargetPath") or ""),
        arguments=str(data.get("Arguments") or ""),
        working_directory=str(data.get("WorkingDirectory") or ""),
    )


def _normalize_input(value: str | Path) -> str:
    text = str(value).strip().strip('"')
    if not text:
        return ""
    if text.lower().startswith("file:"):
        parsed = urlparse(text)
        if parsed.scheme.lower() == "file":
            text = unquote(parsed.path or "")
            text = _windows_uri_drive(text)
    else:
        text = unquote(text)
    return text.replace("/", "\\") if _looks_like_windows_path(text) else text


def _normalize_output(value: str | Path) -> str:
    text = str(value)
    return text.replace("/", "\\") if _looks_like_windows_path(text) else text


def _looks_like_windows_path(value: str) -> bool:
    return len(value) >= 2 and value[1] == ":"


def _windows_uri_drive(value: str) -> str:
    if len(value) >= 4 and value[0] == "/" and value[2] == ":":
        return value[1:]
    return value
