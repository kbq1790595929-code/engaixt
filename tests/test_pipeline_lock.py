from pathlib import Path

import pytest

from core.pipeline_lock import GamePipelineLock, PipelineAlreadyRunning, game_identity


def test_pipeline_lock_rejects_duplicate_game_in_same_process(tmp_path: Path):
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "Game.exe"
    exe.write_bytes(b"MZ")
    locks = tmp_path / "locks"

    with GamePipelineLock(exe, base_dir=locks):
        with pytest.raises(PipelineAlreadyRunning, match="已有翻译任务"):
            with GamePipelineLock(game, base_dir=locks):
                pass

    with GamePipelineLock(game, base_dir=locks):
        pass


def test_pipeline_lock_maps_exe_and_directory_to_same_identity(tmp_path: Path):
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "Game.exe"
    exe.write_bytes(b"MZ")

    assert game_identity(exe) == game_identity(game)
