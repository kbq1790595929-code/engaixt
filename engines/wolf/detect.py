from __future__ import annotations

from pathlib import Path


ARCHIVE_EXTENSIONS = {
    ".wolf", ".data", ".pak", ".bin", ".assets",
    ".content", ".res", ".resource",
}


def game_dir_for(path: Path) -> Path:
    return path if path.is_dir() else path.parent


def wolf_evidence(path: Path) -> list[str]:
    """Return WOLF evidence using root-level metadata only."""
    root = game_dir_for(path)
    evidence: list[str] = []

    exe_names = {p.name.casefold() for p in _root_files(root, ".exe")}
    has_wolf_exe = "game.exe" in exe_names or "gamepro.exe" in exe_names
    if has_wolf_exe:
        evidence.append("找到 WOLF 标准启动程序 Game.exe/GamePro.exe")

    data = root / "Data"
    if _has_loose_wolf_data(data):
        evidence.append("找到 Data/BasicData 与 WOLF 结构化数据")
    elif _has_loose_wolf_data(root):
        evidence.append("找到根目录 BasicData 与 WOLF 结构化数据")

    archives = archive_files(root, max_depth=1)
    strong_archives = [
        p for p in archives
        if p.suffix.casefold() == ".wolf"
        or p.stem.casefold() in {"data", "basicdata", "mapdata"}
    ]
    if strong_archives:
        evidence.append(f"找到 {len(strong_archives)} 个 WOLF RPG 归档")

    has_loose_data = _has_loose_wolf_data(data) or _has_loose_wolf_data(root)
    has_wolf_archive = any(p.suffix.casefold() == ".wolf" for p in strong_archives)
    has_named_archive = bool(strong_archives) and has_wolf_exe
    # Game.exe is far too common to identify WOLF on its own. Non-.wolf
    # archive extensions also require the standard WOLF executable name.
    if not (has_loose_data or has_wolf_archive or has_named_archive):
        return []
    return evidence


def confidence(path: Path) -> tuple[int, list[str]]:
    evidence = wolf_evidence(path)
    if not evidence:
        return 0, []
    root = game_dir_for(path)
    data = root / "Data"
    score = 88
    if _has_loose_wolf_data(data) or _has_loose_wolf_data(root):
        score = 98
    elif any(p.suffix.casefold() == ".wolf" for p in archive_files(root, max_depth=1)):
        score = 97
    elif any("Game.exe" in item or "GamePro.exe" in item for item in evidence):
        score = 92
    return score, evidence


def archive_files(root: Path, max_depth: int = 2) -> list[Path]:
    results: list[Path] = []
    pending = [(root, 0)]
    while pending:
        current, depth = pending.pop(0)
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and entry.suffix.casefold() in ARCHIVE_EXTENSIONS:
                results.append(entry)
            elif entry.is_dir() and depth < max_depth and entry.name not in {
                "_translation_meta", ".game_translator", "save", "saves",
            }:
                pending.append((entry, depth + 1))
    return sorted(results, key=lambda p: str(p).casefold())


def find_wolf_exe(path: Path) -> Path | None:
    root = game_dir_for(path)
    if (
        path.is_file()
        and path.suffix.casefold() == ".exe"
        and path.stem.casefold() not in {"config", "editor", "uninstall", "unins000"}
    ):
        return path
    for name in ("GamePro.exe", "Game.exe"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    candidates = _root_files(root, ".exe")
    return candidates[0] if candidates else None


def find_loose_data_root(root: Path) -> Path | None:
    for candidate in (root / "Data", root):
        if _has_loose_wolf_data(candidate):
            return candidate
    return None


def _root_files(root: Path, suffix: str) -> list[Path]:
    try:
        return sorted(
            (p for p in root.iterdir() if p.is_file() and p.suffix.casefold() == suffix),
            key=lambda p: p.name.casefold(),
        )
    except OSError:
        return []


def _has_loose_wolf_data(root: Path) -> bool:
    basic = root / "BasicData"
    if not basic.is_dir():
        return False
    has_common = (basic / "CommonEvent.dat").is_file()
    has_game = (basic / "Game.dat").is_file()
    has_maps = (root / "MapData").is_dir()
    return has_common and has_game and has_maps
