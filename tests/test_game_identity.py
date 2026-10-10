from __future__ import annotations

import json
from pathlib import Path

from core.game_identity import resolve_game_identity


def test_rpgmaker_system_json_is_used_as_work_title(tmp_path: Path):
    data_dir = tmp_path / "renamed-folder" / "www" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "System.json").write_text(
        json.dumps({"gameTitle": "天使の早漏治療クリニック"}, ensure_ascii=False),
        encoding="utf-8",
    )

    identity = resolve_game_identity(data_dir.parents[1])

    assert identity.title == "天使の早漏治療クリニック"
    assert identity.source == "rpgmaker_json"


def test_rpgmaker_game_ini_title_wins_over_directory(tmp_path: Path):
    game_dir = tmp_path / "download-123"
    game_dir.mkdir()
    (game_dir / "Game.ini").write_text("[Game]\nTitle=作品タイトル\n", encoding="utf-8")

    identity = resolve_game_identity(game_dir)

    assert identity.title == "作品タイトル"
    assert identity.source == "rpgmaker_ini"


def test_godot_and_renpy_metadata_are_supported(tmp_path: Path):
    godot = tmp_path / "godot-dir"
    godot.mkdir()
    (godot / "project.godot").write_text('[application]\nconfig/name="Kick the Demon Out"\n', encoding="utf-8")
    assert resolve_game_identity(godot).title == "Kick the Demon Out"

    renpy = tmp_path / "renpy-dir"
    (renpy / "game").mkdir(parents=True)
    (renpy / "game" / "options.rpy").write_text(
        'define config.name = _("Actual Work Name")\n', encoding="utf-8"
    )
    assert resolve_game_identity(renpy).title == "Actual Work Name"


def test_steam_manifest_name_is_used_for_work_title(tmp_path: Path):
    steamapps = tmp_path / "steamapps"
    game_dir = steamapps / "common" / "InstallFolder"
    game_dir.mkdir(parents=True)
    (steamapps / "appmanifest_42.acf").write_text(
        '"AppState" { "appid" "42" "name" "Published Work Name" "installdir" "InstallFolder" }',
        encoding="utf-8",
    )

    identity = resolve_game_identity(game_dir)

    assert identity.title == "Published Work Name"
    assert identity.source == "steam_manifest"


def test_manifest_title_is_stable_and_directory_is_only_fallback(tmp_path: Path):
    game_dir = tmp_path / "arbitrary-folder"
    game_dir.mkdir()

    stable = resolve_game_identity(
        game_dir,
        manifest_data={"game_title": "固化作品名", "title_source": "rpgmaker_json"},
    )
    fallback = resolve_game_identity(game_dir)

    assert stable.title == "固化作品名"
    assert stable.source == "rpgmaker_json"
    assert fallback.title == "arbitrary-folder"
    assert fallback.source == "directory_fallback"
