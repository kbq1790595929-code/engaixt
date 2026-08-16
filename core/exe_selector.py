from __future__ import annotations

import re
from pathlib import Path


_BAD_NAME_PARTS = (
    "crash", "unitycrash", "unins", "uninstall", "setup", "install",
    "config", "setting", "settings", "option", "options", "launcher_patch",
    "patcher", "updater", "update", "notification", "translator", "translate",
    "tool", "tools", "helper", "server", "dedicated", "debug", "diagnostic",
    "checker", "check", "verify", "verification", "integrity", "repair",
    "netlog", "log", "redistributable", "redist", "vcredist", "dotnet",
    "汉化", "一键", "工具",
)
_BAD_EXACT_NAMES = {
    "steam", "steamerrorreporter", "unitycrashhandler",
    "unitycrashhandler32", "unitycrashhandler64",
    "krkrpatchloader", "krkrdumploader", "krkrextractlite",
    "mtool", "mtoolgame",
    "kirikiri_native_launcher", "bgi_native_launcher",
}
_GOOD_NAME_PARTS = ("game", "start", "play", "run", "win64", "win32", "shipping")


def find_main_exe(path: Path, recursive: bool = False) -> Path | None:
    """Pick the most likely game executable using general scoring rules."""
    game_dir = path if path.is_dir() else path.parent
    candidates = _iter_exes(game_dir, recursive=recursive)
    if not candidates:
        return None
    return sorted(candidates, key=lambda exe: _score_exe(exe, game_dir), reverse=True)[0]


def find_main_exes(path: Path, recursive: bool = False) -> list[Path]:
    game_dir = path if path.is_dir() else path.parent
    return sorted(_iter_exes(game_dir, recursive=recursive),
                  key=lambda exe: _score_exe(exe, game_dir),
                  reverse=True)


def _iter_exes(game_dir: Path, recursive: bool) -> list[Path]:
    if not game_dir.exists():
        return []
    if not recursive:
        return [p for p in game_dir.glob("*.exe") if _is_candidate(p)]
    candidates: list[Path] = []
    skip_dirs = {
        "__pycache__", ".git", "node_modules", "BepInEx", "MonoBleedingEdge",
        "redist", "_CommonRedist", "tools", "tool", "crashpad",
        "_translation_meta", ".game_translator", "bgi_hook", "kirikiri_patch",
    }
    skip_dirs_norm = {item.lower() for item in skip_dirs}
    for root, dirs, files in game_dir.walk():
        dirs[:] = [d for d in dirs if d.lower() not in skip_dirs_norm]
        for name in files:
            if name.lower().endswith(".exe"):
                exe = Path(root) / name
                if _is_candidate(exe):
                    candidates.append(exe)
    return candidates


def _is_candidate(exe: Path) -> bool:
    stem = _norm(exe.stem)
    if stem in _BAD_EXACT_NAMES:
        return False
    if any(part in stem for part in _BAD_NAME_PARTS):
        return False
    if _looks_like_integrity_checker(exe):
        return False
    return exe.is_file()


def _score_exe(exe: Path, game_dir: Path) -> tuple[int, int, int, str]:
    stem_raw = exe.stem
    stem = _norm(stem_raw)
    dir_name = _norm(game_dir.name)
    rel_depth = len(exe.relative_to(game_dir).parts) if _is_relative_to(exe, game_dir) else 99
    score = 0

    if exe.parent == game_dir:
        score += 120
    else:
        score -= rel_depth * 20

    if stem == dir_name:
        score += 220
    elif stem and (stem in dir_name or dir_name in stem):
        score += 150
    elif _token_overlap_score(stem, dir_name) >= 2:
        score += 80

    if _steam_appid_matches(exe, game_dir):
        score += 180
    if _matches_unity_data_dir(exe, game_dir):
        score += 160
    if (exe.with_name(exe.stem + ".pck")).exists():
        score += 120
    if stem in ("game", "start", "run"):
        score += 70
    if any(part in stem for part in _GOOD_NAME_PARTS):
        score += 25
    if any(part in stem for part in _BAD_NAME_PARTS):
        score -= 260
    if stem in _BAD_EXACT_NAMES:
        score -= 1000

    try:
        size = exe.stat().st_size
    except OSError:
        size = 0
    if size >= 5 * 1024 * 1024:
        score += 60
    elif size < 256 * 1024:
        score -= 80

    # Packed stub / loader: a tiny .text section means the real engine is compressed
    # or loaded later. Prefer the full engine exe when both exist (e.g. KRKRZ games
    # shipped with a packed original next to an unpacked engine).
    text_size = _read_text_section_size(exe)
    if text_size is not None and text_size < 64 * 1024:
        score -= 300

    return (score, -rel_depth, size, exe.name.lower())


def _read_text_section_size(path: Path) -> int | None:
    """Return the file-relative size of the PE .text section in bytes, or None on failure.

    A packed stub / loader usually has a tiny .text section because the real engine
    code is compressed or loaded later; the true engine exe has a large .text.
    Returns None whenever the PE cannot be parsed so callers stay conservative.
    """
    try:
        data = path.read_bytes()[:4096]
        if len(data) < 0x40 or data[:2] != b"MZ":
            return None
        pe_off = int.from_bytes(data[0x3C:0x40], "little")
        if pe_off + 24 > len(data) or data[pe_off:pe_off + 4] != b"PE\0\0":
            return None
        num_sections = int.from_bytes(data[pe_off + 6:pe_off + 8], "little")
        opt_size = int.from_bytes(data[pe_off + 20:pe_off + 22], "little")
        sec_start = pe_off + 24 + opt_size
        for i in range(num_sections):
            off = sec_start + i * 40
            if off + 40 > len(data):
                break
            name = data[off:off + 8].rstrip(b"\0")
            if name == b".text":
                return int.from_bytes(data[off + 16:off + 20], "little")
        return None
    except (OSError, ValueError):
        return None


def _looks_like_integrity_checker(exe: Path) -> bool:
    name = exe.name.casefold()
    stem = _norm(exe.stem)
    if "破損" in name or "チェック" in name:
        return True
    return bool(stem and any(
        part in stem
        for part in ("filecheck", "filechecker", "integritycheck", "repairtool")
    ))


def _matches_unity_data_dir(exe: Path, game_dir: Path) -> bool:
    stem = exe.stem
    if (game_dir / f"{stem}_Data").is_dir():
        return True
    return any(d.is_dir() and d.name.lower().startswith(stem.lower())
               and d.name.lower().endswith("_data")
               for d in game_dir.glob("*_Data"))


def _steam_appid_matches(exe: Path, game_dir: Path) -> bool:
    appid_file = game_dir / "steam_appid.txt"
    if not appid_file.is_file():
        return False
    try:
        appid = appid_file.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return False
    if not appid or not appid.isdigit():
        return False
    # If a Steam game has exactly one strong game-named exe, favor it globally.
    return _norm(exe.stem) == _norm(game_dir.name)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _token_overlap_score(left: str, right: str) -> int:
    left_tokens = set(re.findall(r"[a-z0-9]+", left.lower()))
    right_tokens = set(re.findall(r"[a-z0-9]+", right.lower()))
    return len(left_tokens & right_tokens)


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False
