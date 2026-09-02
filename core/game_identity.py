from __future__ import annotations

import configparser
import ctypes
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_GODOT_NAME_RE = re.compile(r"(?m)^\s*config/name\s*=\s*(.+?)\s*$")
_RENPY_NAME_RE = re.compile(
    r"(?m)^\s*(?:define\s+)?config\.(?:name|window_title)\s*=\s*(?:_\s*\(\s*)?"
    r"(?P<quote>['\"])(?P<title>.+?)(?P=quote)\s*\)?\s*$"
)
_GENERIC_TITLES = {
    "game",
    "game.exe",
    "nw.js",
    "nwjs",
    "rpg maker",
    "kirikiri",
    "kirikiri z",
    "kirikiri z core / scripting platform",
    "ren'py",
    "unity",
}


@dataclass(frozen=True)
class GameIdentity:
    title: str
    source: str
    game_dir: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "source": self.source,
            "game_dir": str(self.game_dir),
        }


def resolve_game_identity(
    game_path: str | Path,
    *,
    manifest_data: dict[str, Any] | None = None,
) -> GameIdentity:
    path = Path(game_path).expanduser()
    game_dir = path if path.is_dir() else path.parent
    game_dir = game_dir.resolve()

    manifest_title = _clean_title((manifest_data or {}).get("game_title"))
    manifest_source = str((manifest_data or {}).get("title_source") or "manifest")
    if manifest_title and manifest_source != "directory_fallback":
        return GameIdentity(manifest_title, manifest_source, game_dir)

    steam = _steam_title(path, game_dir)
    if steam:
        return GameIdentity(steam, "steam_manifest", game_dir)

    metadata_readers = (
        _rpgmaker_json_title,
        _rpgmaker_ini_title,
        _godot_title,
        _renpy_title,
        _package_json_title,
    )
    for reader in metadata_readers:
        title = reader(game_dir)
        if title:
            return GameIdentity(title, reader.__name__.removeprefix("_").removesuffix("_title"), game_dir)

    exe_title = _executable_title(path, game_dir)
    if exe_title:
        return GameIdentity(exe_title, "exe_version_info", game_dir)

    return GameIdentity(_clean_title(game_dir.name) or "未命名作品", "directory_fallback", game_dir)


def _steam_title(path: Path, game_dir: Path) -> str:
    try:
        from core.steam import parse_vdf_file
    except Exception:
        return ""

    manifests: list[Path] = []
    if path.is_file() and path.name.lower().startswith("appmanifest_") and path.suffix.lower() == ".acf":
        manifests.append(path)
    for parent in (game_dir, *game_dir.parents):
        if parent.name.lower() == "common" and parent.parent.name.lower() == "steamapps":
            manifests.extend(parent.parent.glob("appmanifest_*.acf"))
            break

    expected_install = game_dir.name.casefold()
    for manifest in manifests:
        data = parse_vdf_file(manifest)
        state = _case_get(data, "AppState")
        if not isinstance(state, dict):
            continue
        install_dir = _clean_title(_case_get(state, "installdir"))
        if path != manifest and install_dir.casefold() != expected_install:
            continue
        title = _clean_title(_case_get(state, "name"))
        if _usable_title(title):
            return title
    return ""


def _rpgmaker_json_title(game_dir: Path) -> str:
    for rel in ("data/System.json", "www/data/System.json"):
        data = _read_json(game_dir / rel)
        title = _clean_title(data.get("gameTitle")) if isinstance(data, dict) else ""
        if _usable_title(title):
            return title
    return ""


def _rpgmaker_ini_title(game_dir: Path) -> str:
    path = game_dir / "Game.ini"
    if not path.is_file():
        return ""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    for encoding in ("utf-8-sig", "cp932", "mbcs"):
        try:
            parser.read_string(path.read_text(encoding=encoding))
            title = _clean_title(parser.get("Game", "Title", fallback=""))
            if _usable_title(title):
                return title
        except (OSError, UnicodeError, configparser.Error, LookupError):
            continue
    return ""


def _godot_title(game_dir: Path) -> str:
    path = game_dir / "project.godot"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""
    match = _GODOT_NAME_RE.search(text)
    if not match:
        return ""
    raw = match.group(1).strip()
    if raw.startswith(('"', "'")):
        try:
            raw = json.loads(raw) if raw.startswith('"') else raw[1:-1]
        except (ValueError, TypeError):
            raw = raw[1:-1]
    title = _clean_title(raw)
    return title if _usable_title(title) else ""


def _renpy_title(game_dir: Path) -> str:
    for rel in ("game/options.rpy", "game/options.rpyc"):
        path = game_dir / rel
        if path.suffix == ".rpyc" or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        match = _RENPY_NAME_RE.search(text)
        if match:
            title = _clean_title(match.group("title"))
            if _usable_title(title):
                return title
    return ""


def _package_json_title(game_dir: Path) -> str:
    for rel in ("package.json", "www/package.json"):
        data = _read_json(game_dir / rel)
        if not isinstance(data, dict):
            continue
        for key in ("productName", "window.title", "name"):
            value: Any = data
            for part in key.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            title = _clean_title(value)
            if _usable_title(title):
                return title
    return ""


def _executable_title(path: Path, game_dir: Path) -> str:
    candidates: list[Path] = []
    if path.is_file() and path.suffix.lower() == ".exe":
        candidates.append(path)
    if not candidates:
        try:
            from core.exe_selector import find_main_exe

            selected = find_main_exe(game_dir)
            if selected:
                candidates.append(Path(selected))
        except Exception:
            pass
    for exe in candidates:
        for field in ("ProductName", "FileDescription"):
            title = _clean_title(_windows_version_string(exe, field))
            if _usable_title(title):
                return title
    return ""


def _windows_version_string(path: Path, field: str) -> str:
    if not hasattr(ctypes, "windll") or not path.is_file():
        return ""
    try:
        version = ctypes.windll.version
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return ""

        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        translations: list[tuple[int, int]] = []
        if version.VerQueryValueW(buffer, r"\VarFileInfo\Translation", ctypes.byref(pointer), ctypes.byref(length)):
            raw = ctypes.string_at(pointer.value, length.value)
            translations.extend(
                (int.from_bytes(raw[i : i + 2], "little"), int.from_bytes(raw[i + 2 : i + 4], "little"))
                for i in range(0, len(raw) - 3, 4)
            )
        translations.extend(((0x0411, 0x04B0), (0x0804, 0x04B0), (0x0409, 0x04B0), (0x0409, 0x04E4)))
        seen: set[tuple[int, int]] = set()
        for language, codepage in translations:
            if (language, codepage) in seen:
                continue
            seen.add((language, codepage))
            query = fr"\StringFileInfo\{language:04x}{codepage:04x}\{field}"
            if version.VerQueryValueW(buffer, query, ctypes.byref(pointer), ctypes.byref(length)) and pointer.value:
                value = ctypes.wstring_at(pointer.value, max(0, length.value - 1))
                if value:
                    return value
    except (OSError, ValueError, TypeError, AttributeError):
        return ""
    return ""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        return None


def _case_get(data: dict[str, Any], key: str) -> Any:
    expected = key.casefold()
    for current, value in data.items():
        if str(current).casefold() == expected:
            return value
    return None


def _clean_title(value: Any) -> str:
    title = " ".join(str(value or "").replace("\x00", "").split()).strip()
    return title[:300]


def _usable_title(title: str) -> bool:
    return bool(title and title.casefold() not in _GENERIC_TITLES)
