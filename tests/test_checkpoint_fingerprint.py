"""检查点与游戏内容指纹的回归测试。

重新解压/替换游戏资源后，旧检查点不能静默复用；否则日志会先"翻译"再
"解包"，看起来像流程倒置，实际是加载了上一份安装的译文。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core.checkpoint_store import _belongs_to_game
from core.manifest import game_content_fingerprint


def _make_game(root: Path) -> Path:
    game = root / "Game"
    game.mkdir()
    (game / "data.xp3").write_bytes(b"payload")
    (game / "Game.exe").write_bytes(b"MZ")
    return game


def _write_checkpoint(path: Path, game: Path, fingerprint: str | None = None) -> None:
    data = {
        "source": str(game.resolve()),
        "items": [{"file": "a.ks", "original": "x", "translated": "y"}],
    }
    if fingerprint is not None:
        data["content_fingerprint"] = fingerprint
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_matching_fingerprint_is_reused(tmp_path: Path) -> None:
    game = _make_game(tmp_path)
    checkpoint = tmp_path / "translation_checkpoint.json"
    _write_checkpoint(checkpoint, game, game_content_fingerprint(game))

    assert _belongs_to_game(checkpoint, game) is True


def test_fingerprint_from_another_install_is_rejected(tmp_path: Path) -> None:
    game = _make_game(tmp_path)
    checkpoint = tmp_path / "translation_checkpoint.json"
    _write_checkpoint(checkpoint, game, "0000000000000000")

    assert _belongs_to_game(checkpoint, game) is False


def test_legacy_checkpoint_is_rejected_when_game_is_newer(tmp_path: Path) -> None:
    game = _make_game(tmp_path)
    checkpoint = tmp_path / "translation_checkpoint.json"
    _write_checkpoint(checkpoint, game)
    old = time.time() - 3600
    os.utime(checkpoint, (old, old))

    assert _belongs_to_game(checkpoint, game) is False


def test_legacy_checkpoint_is_reused_when_game_is_older(tmp_path: Path) -> None:
    game = _make_game(tmp_path)
    checkpoint = tmp_path / "translation_checkpoint.json"
    _write_checkpoint(checkpoint, game)
    future = time.time() + 3600
    os.utime(checkpoint, (future, future))

    assert _belongs_to_game(checkpoint, game) is True
