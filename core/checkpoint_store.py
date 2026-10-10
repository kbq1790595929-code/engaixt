"""用户目录中的持久化翻译检查点。

游戏目录可能位于 Program Files、只读介质或没有写权限的目录。
检查点属于 EngAixt 的工作状态，不应依赖游戏目录可写；游戏目录里的
旧检查点只作为兼容输入，新的权威副本放在应用状态目录。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from core import manifest as manifest_state


def game_dir_of(game_path: str | Path) -> Path:
    path = Path(game_path)
    return path if path.is_dir() else path.parent


def persistent_checkpoint_path(game_path: str | Path) -> Path:
    game_dir = game_dir_of(game_path)
    game_id = manifest_state.game_id_for(game_dir)
    return manifest_state.BASE_DIR / "checkpoints" / game_id / "translation_checkpoint.json"


def legacy_checkpoint_path(game_path: str | Path) -> Path:
    return game_dir_of(game_path) / "_translation_meta" / "translation_checkpoint.json"


def _belongs_to_game(path: Path, game_dir: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return False
    source = str(data.get("source") or "").strip()
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return False
    if not source:
        # Older game-local checkpoints may predate the ``source`` field.  They
        # are safe to import only from the exact legacy path or the exact
        # application-state path derived from this game directory; callers do
        # not get to supply an arbitrary checkpoint file here.
        try:
            expected_legacy = (game_dir / "_translation_meta" / path.name).resolve()
            expected_persistent = persistent_checkpoint_path(game_dir).resolve()
            if path.resolve() not in {expected_legacy, expected_persistent}:
                return False
        except OSError:
            return False
        return True
    try:
        return Path(source).resolve() == game_dir.resolve()
    except OSError:
        return False


def import_checkpoint_to_workspace(
    game_path: str | Path,
    destination: Path,
    legacy_path: Path | None = None,
) -> Path | None:
    """Copy a valid persistent or legacy checkpoint into the current workspace."""
    game_dir = game_dir_of(game_path)
    candidates = [persistent_checkpoint_path(game_dir)]
    candidates.append(legacy_path or legacy_checkpoint_path(game_dir))
    for candidate in candidates:
        if not candidate.is_file() or not _belongs_to_game(candidate, game_dir):
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if candidate.resolve() != destination.resolve():
                shutil.copy2(candidate, destination)
            return destination
        except OSError:
            continue
    return None


def persist_workspace_checkpoint(game_path: str | Path, checkpoint: Path) -> Path | None:
    """Save the workspace checkpoint in application state for the next run."""
    if not checkpoint.is_file():
        return None
    target = persistent_checkpoint_path(game_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if checkpoint.resolve() != target.resolve():
            shutil.copy2(checkpoint, target)
        return target
    except OSError:
        return None
