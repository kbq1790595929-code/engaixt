from __future__ import annotations

import os
import re
from pathlib import Path


_APP_ID_RE = re.compile(
    r"(?:steam://(?:rungameid|run|launch|nav/games/details)/|"
    r"(?:-applaunch|appid[=/])\s*)(\d+)",
    re.IGNORECASE,
)
_MANIFEST_RE = re.compile(r"appmanifest_(\d+)\.acf$", re.IGNORECASE)


def resolve_steam_entry(value: str | Path) -> Path | None:
    """Resolve a Steam URI, shortcut, or appmanifest to its install directory."""
    text = str(value).strip().strip('"')
    if not text:
        return None

    path = Path(text)
    if path.is_file():
        suffix = path.suffix.lower()
        if suffix == ".url":
            uri = read_steam_url_shortcut(path)
            if uri:
                return resolve_steam_app_id(extract_app_id(uri) or "")
        if suffix == ".acf" and _MANIFEST_RE.search(path.name):
            return resolve_appmanifest(path)
        if path.name.lower() == "steam_appid.txt":
            return path.parent

    app_id = extract_app_id(text)
    if app_id:
        return resolve_steam_app_id(app_id)

    return None


def extract_app_id(value: str | Path) -> str | None:
    """Extract a Steam app id from common URI, shortcut arg, or manifest forms."""
    text = str(value).strip().replace("\\", "/")
    manifest_match = _MANIFEST_RE.search(Path(text).name)
    if manifest_match:
        return manifest_match.group(1)
    match = _APP_ID_RE.search(text)
    if match:
        return match.group(1)
    return None


def read_steam_url_shortcut(path: Path) -> str:
    """Read the URL= line from a Windows InternetShortcut file."""
    try:
        content = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        try:
            content = path.read_text(encoding="mbcs", errors="replace")
        except Exception:
            return ""
    for line in content.splitlines():
        if line.lower().startswith("url="):
            return line.split("=", 1)[1].strip()
    return ""


def resolve_steam_app_id(app_id: str) -> Path | None:
    """Locate the installed game directory for a Steam app id."""
    if not app_id:
        return None
    manifest = find_appmanifest(app_id)
    if not manifest:
        return None
    return resolve_appmanifest(manifest)


def find_appmanifest(app_id: str) -> Path | None:
    name = f"appmanifest_{app_id}.acf"
    for library in iter_steam_libraries():
        manifest = library / "steamapps" / name
        if manifest.is_file():
            return manifest
    return None


def resolve_appmanifest(manifest_path: Path) -> Path | None:
    """Resolve appmanifest_*.acf to steamapps/common/<installdir>."""
    data = parse_vdf_file(manifest_path)
    app_state = _get_case_insensitive(data, "AppState")
    if not isinstance(app_state, dict):
        return None
    install_dir = _get_case_insensitive(app_state, "installdir")
    if not isinstance(install_dir, str) or not install_dir:
        return None
    steamapps = manifest_path.parent
    game_dir = steamapps / "common" / install_dir
    return game_dir if game_dir.exists() else game_dir


def iter_steam_libraries(extra_roots: list[Path] | None = None) -> list[Path]:
    """Return Steam library roots, not the steamapps folders."""
    roots: list[Path] = []
    for raw in os.environ.get("GT_STEAM_ROOTS", "").split(os.pathsep):
        if raw.strip():
            roots.append(Path(raw.strip()))
    if extra_roots:
        roots.extend(extra_roots)
    roots.extend(_default_steam_roots())

    libraries: list[Path] = []
    seen: set[str] = set()

    def add_library(path: Path):
        library = _as_library_root(path)
        key = str(library.resolve()) if library.exists() else str(library)
        if key not in seen:
            seen.add(key)
            libraries.append(library)

    for root in roots:
        add_library(root)
        steamapps = _as_library_root(root) / "steamapps"
        libraryfolders = steamapps / "libraryfolders.vdf"
        for library in parse_libraryfolders(libraryfolders):
            add_library(library)

    return libraries


def parse_libraryfolders(path: Path) -> list[Path]:
    if not path.is_file():
        return []
    data = parse_vdf_file(path)
    folders = _get_case_insensitive(data, "libraryfolders")
    if not isinstance(folders, dict):
        return []

    result: list[Path] = []
    for key, value in folders.items():
        if not str(key).isdigit():
            continue
        if isinstance(value, str):
            result.append(Path(value))
        elif isinstance(value, dict):
            library_path = _get_case_insensitive(value, "path")
            if isinstance(library_path, str) and library_path:
                result.append(Path(library_path))
    return result


def parse_vdf_file(path: Path) -> dict:
    try:
        return parse_vdf(path.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return {}


def parse_vdf(text: str) -> dict:
    """Parse enough Valve KeyValues/VDF for Steam manifests and library lists."""
    tokens = _tokenize_vdf(_strip_vdf_comments(text))
    pos = 0

    def parse_object() -> dict:
        nonlocal pos
        obj: dict = {}
        while pos < len(tokens):
            token = tokens[pos]
            if token == "}":
                pos += 1
                break
            if token == "{":
                pos += 1
                continue
            key = token
            pos += 1
            if pos < len(tokens) and tokens[pos] == "{":
                pos += 1
                obj[key] = parse_object()
            elif pos < len(tokens):
                obj[key] = tokens[pos]
                pos += 1
            else:
                obj[key] = ""
        return obj

    return parse_object()


def _default_steam_roots() -> list[Path]:
    roots: list[Path] = []
    roots.extend(_steam_roots_from_registry())
    for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env_name)
        if base:
            roots.append(Path(base) / "Steam")
    return roots


def _steam_roots_from_registry() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except Exception:
        return []

    probes = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamExe"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ]
    roots: list[Path] = []
    for hive, key_name, value_name in probes:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        p = Path(str(value).replace("/", "\\"))
        if p.name.lower() == "steam.exe":
            p = p.parent
        roots.append(p)
    return roots


def _as_library_root(path: Path) -> Path:
    if path.name.lower() == "steamapps":
        return path.parent
    return path


def _get_case_insensitive(data: dict, wanted: str):
    wanted_lower = wanted.lower()
    for key, value in data.items():
        if str(key).lower() == wanted_lower:
            return value
    return None


def _strip_vdf_comments(text: str) -> str:
    result: list[str] = []
    in_quote = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_quote:
            result.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_quote = False
            i += 1
            continue
        if ch == '"':
            in_quote = True
            result.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def _tokenize_vdf(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "{}":
            tokens.append(ch)
            i += 1
            continue
        if ch == '"':
            i += 1
            buf: list[str] = []
            escaped = False
            while i < len(text):
                ch = text[i]
                if escaped:
                    if ch in ('"', "\\"):
                        buf.append(ch)
                    else:
                        buf.append("\\" + ch)
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    i += 1
                    break
                else:
                    buf.append(ch)
                i += 1
            tokens.append("".join(buf))
            continue
        start = i
        while i < len(text) and not text[i].isspace() and text[i] not in "{}":
            i += 1
        tokens.append(text[start:i])
    return tokens
