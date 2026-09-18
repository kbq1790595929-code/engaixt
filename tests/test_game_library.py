"""Regression tests for the translated-games library."""

from __future__ import annotations

import json
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.manifest as manifest
import core.translation_cache_db as cache_db
from app import Api, _LIBRARY_PRUNE_STATE


def _write_manifest(manifest_dir: Path, game_id: str, game_dir: Path) -> Path:
    path = manifest_dir / f"{game_id}.json"
    path.write_text(
        json.dumps(
            {
                "game_id": game_id,
                "game_dir": str(game_dir),
                "engine": "Demo",
                "created_at": "2026-01-01 00:00:00",
                "updated_at": "2026-01-01 00:00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_library_only_lists_games_whose_directories_still_exist():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest_dir = root / "manifests"
        manifest_dir.mkdir()
        backup_dir = root / "backups"
        workspaces_dir = root / "workspaces"
        cache_dir = root / "cache"
        backup_dir.mkdir()
        workspaces_dir.mkdir()
        cache_dir.mkdir()
        live_game = root / "Live Game"
        live_game.mkdir()
        missing_game = root / "Missing Game"

        live_manifest = _write_manifest(manifest_dir, "live", live_game)
        missing_manifest = _write_manifest(manifest_dir, "missing", missing_game)
        missing_backup = backup_dir / "missing"
        missing_backup.mkdir()
        (missing_backup / "original.dat").write_bytes(b"backup")

        old_db_dir = cache_db.DB_DIR
        with patch.object(manifest, "MANIFEST_DIR", manifest_dir), \
             patch.object(manifest, "BACKUP_DIR", backup_dir), \
             patch.object(manifest, "WORKSPACES_DIR", workspaces_dir), \
             patch.object(cache_db, "DB_DIR", cache_dir):
            _LIBRARY_PRUNE_STATE["running"] = False
            _LIBRARY_PRUNE_STATE["last"] = 0.0
            db_path = cache_db.get_cache_paths_for_game(missing_game)[0]
            db_path.write_bytes(b"sqlite")
            (Path(str(db_path) + "-wal")).write_bytes(b"wal")

            missing_workspace = workspaces_dir / "job_missing"
            missing_workspace.mkdir()
            (missing_workspace / "translation_checkpoint.json").write_text(
                json.dumps({"source": str(missing_game)}, ensure_ascii=False),
                encoding="utf-8",
            )
            other_workspace = workspaces_dir / "job_other"
            other_workspace.mkdir()
            (other_workspace / "translation_checkpoint.json").write_text(
                json.dumps({"source": str(root / "Other Game")}, ensure_ascii=False),
                encoding="utf-8",
            )

            games = Api().list_translated_games()
            import time
            deadline = time.time() + 3
            while (
                (missing_manifest.exists() or _LIBRARY_PRUNE_STATE["running"])
                and time.time() < deadline
            ):
                time.sleep(0.01)
            cache_db.DB_DIR = old_db_dir

        assert games == [
            {
                "game_id": "live",
                "game_dir": str(live_game),
                "engine": "Demo",
                "created_at": "2026-01-01 00:00:00",
                "updated_at": "2026-01-01 00:00:00",
            }
        ]
        assert live_manifest.exists()
        assert not missing_manifest.exists()
        assert not missing_backup.exists()
        assert not db_path.exists()
        assert not Path(str(db_path) + "-wal").exists()
        assert not missing_workspace.exists()
        assert other_workspace.exists()


def test_library_skips_non_directory_entries():
    """A manifest whose game_dir is a FILE must never appear as a game card."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest_dir = root / "manifests"
        manifest_dir.mkdir()
        backup_dir = root / "backups"
        backup_dir.mkdir()
        workspaces_dir = root / "workspaces"
        workspaces_dir.mkdir()
        cache_dir = root / "cache"
        cache_dir.mkdir()
        a_file = root / "not_a_game.7z"
        a_file.write_bytes(b"archive")
        live_game = root / "Live Game"
        live_game.mkdir()

        file_manifest = _write_manifest(manifest_dir, "file", a_file)
        live_manifest = _write_manifest(manifest_dir, "live", live_game)

        old_db_dir = cache_db.DB_DIR
        with patch.object(manifest, "MANIFEST_DIR", manifest_dir), \
             patch.object(manifest, "BACKUP_DIR", backup_dir), \
             patch.object(manifest, "WORKSPACES_DIR", workspaces_dir), \
             patch.object(cache_db, "DB_DIR", cache_dir):
            _LIBRARY_PRUNE_STATE["running"] = False
            _LIBRARY_PRUNE_STATE["last"] = 0.0
            games = Api().list_translated_games()
            import time
            deadline = time.time() + 3
            while (
                (file_manifest.exists() or _LIBRARY_PRUNE_STATE["running"])
                and time.time() < deadline
            ):
                time.sleep(0.01)
            cache_db.DB_DIR = old_db_dir

        assert games == [
            {
                "game_id": "live",
                "game_dir": str(live_game),
                "engine": "Demo",
                "created_at": "2026-01-01 00:00:00",
                "updated_at": "2026-01-01 00:00:00",
            }
        ]
        assert not file_manifest.exists()
        assert live_manifest.exists()


def test_game_library_launch_prefers_translated_launcher_for_any_engine():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest_dir = root / "manifests"
        manifest_dir.mkdir()
        game_dir = root / "KiriKiri Game"
        game_dir.mkdir()
        launcher = game_dir / "启动汉化版.bat"
        launcher.write_text("@echo off\r\n", encoding="utf-8")
        (game_dir / "Game.exe").write_bytes(b"MZ")
        (manifest_dir / "kirikiri.json").write_text(
            json.dumps(
                {
                    "game_id": "kirikiri",
                    "game_dir": str(game_dir),
                    "engine": "kirikiri",
                    "created_files": [
                        {
                            "path": str(launcher),
                            "rel": launcher.name,
                            "kind": "runtime",
                            "runtime_required": True,
                            "is_dir": False,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        started = []
        raw_started = []
        api = Api()
        def fake_launch(launcher_path, cwd=None, **_kwargs):
            started.append((str(launcher_path), str(cwd)))

        with patch.object(manifest, "MANIFEST_DIR", manifest_dir), \
             patch("app.MANIFEST_DIR", manifest_dir, create=True), \
             patch("core.launcher.launch_translated_launcher", fake_launch), \
             patch("os.startfile", lambda p: raw_started.append(str(p))):
            api.open_game_dir(str(game_dir))

            # open_game_dir launches on a background thread; keep mocks active
            # until the mocked startfile call arrives.
            import time
            deadline = time.time() + 2
            while not started and time.time() < deadline:
                time.sleep(0.01)

        assert started == [(str(launcher), str(game_dir))]
        assert raw_started == []


if __name__ == "__main__":
    test_library_only_lists_games_whose_directories_still_exist()
    test_game_library_launch_prefers_translated_launcher_for_any_engine()
    print("Game library tests passed")
