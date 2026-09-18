from __future__ import annotations

import json
from pathlib import Path

from engines.base import TextItem


def _use_temp_manifest_dirs(monkeypatch, tmp_path: Path):
    import core.manifest as manifest_mod

    state = tmp_path / "state"
    monkeypatch.setattr(manifest_mod, "BASE_DIR", state)
    monkeypatch.setattr(manifest_mod, "BACKUP_DIR", state / "backups")
    monkeypatch.setattr(manifest_mod, "MANIFEST_DIR", state / "manifests")
    monkeypatch.setattr(manifest_mod, "WORKSPACES_DIR", state / "workspaces")
    monkeypatch.setattr(
        manifest_mod,
        "_clear_translation_cache_for_game",
        lambda _game_dir: {"deleted_files": 0, "bytes_freed": 0, "errors": 0},
    )
    return manifest_mod


def _make_rpgmaker_game(tmp_path: Path) -> Path:
    game = tmp_path / "game"
    (game / "js").mkdir(parents=True)
    (game / "save").mkdir()
    (game / "index.html").write_text(
        "\n".join(
            [
                "<html>",
                "<body>",
                '    <script type="text/javascript" src="js/rpgmaker_hook.js"></script>',
                '    <script type="text/javascript" src="js/main.js"></script>',
                "</body>",
                "</html>",
            ]
        ),
        encoding="utf-8",
    )
    (game / "js" / "main.js").write_text(
        "\n".join(
            [
                'var scriptUrls = ["js/rpgmaker_hook.js",',
                '    "js/plugins.js"',
                "];",
            ]
        ),
        encoding="utf-8",
    )
    (game / "package.json").write_text(
        json.dumps({"main": "index.html", "nodejs": True}, ensure_ascii=False),
        encoding="utf-8",
    )
    (game / "js" / "rpgmaker_hook.js").write_text("var MODE = \"replace\";\n", encoding="utf-8")
    (game / "save" / "hook_translation_map.json").write_text("{}", encoding="utf-8")
    profile = game / ".nwjs_profile"
    profile.mkdir()
    (profile / "keep.txt").write_text("profile data", encoding="utf-8")
    return game


def test_rpgmaker_uninstall_cleans_old_empty_manifest_residue(monkeypatch, tmp_path):
    manifest_mod = _use_temp_manifest_dirs(monkeypatch, tmp_path)
    game = _make_rpgmaker_game(tmp_path)
    (game / ".game_translator_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "game_id": "stale",
                "game_dir": "C:/broken/mojibake",
                "engine": "rpgmaker",
                "modified_files": [],
                "created_files": [],
            }
        ),
        encoding="utf-8",
    )

    result = manifest_mod.uninstall_game_translation(game)

    assert result["hook_entries_removed"] == 2
    assert not (game / "js" / "rpgmaker_hook.js").exists()
    assert not (game / "save" / "hook_translation_map.json").exists()
    assert (game / ".nwjs_profile" / "keep.txt").exists()
    assert "rpgmaker_hook.js" not in (game / "index.html").read_text(encoding="utf-8")
    assert "rpgmaker_hook.js" not in (game / "js" / "main.js").read_text(encoding="utf-8")
    assert "nodejs" not in json.loads((game / "package.json").read_text(encoding="utf-8"))
    assert not (game / ".game_translator_manifest.json").exists()


def test_rpgmaker_runtime_deploy_records_restore_manifest(monkeypatch, tmp_path):
    manifest_mod = _use_temp_manifest_dirs(monkeypatch, tmp_path)
    from core.rpgmaker_runtime import _inject_hook, translate_and_deploy

    game = _make_rpgmaker_game(tmp_path)
    (game / "index.html").write_text(
        '<script type="text/javascript" src="js/main.js"></script>\n',
        encoding="utf-8",
    )
    (game / "js" / "main.js").write_text('var scriptUrls = ["js/plugins.js"];\n', encoding="utf-8")
    (game / "package.json").write_text(json.dumps({"main": "index.html"}), encoding="utf-8")
    (game / "js" / "rpgmaker_hook.js").unlink()
    (game / "save" / "hook_translation_map.json").unlink()

    manifest = manifest_mod.GameManifest.for_game(game)
    _inject_hook(game, "scan", manifest=manifest)
    count = translate_and_deploy(
        game,
        [TextItem(file="hook", key="k", original="こんにちは", translated="你好")],
        manifest=manifest,
    )

    assert count == 1
    data = json.loads((game / ".game_translator_manifest.json").read_text(encoding="utf-8"))
    modified = {item["rel"].replace("\\", "/") for item in data["modified_files"]}
    created = {item["rel"].replace("\\", "/") for item in data["created_files"]}
    assert {"index.html", "js/main.js", "package.json"} <= modified
    assert {"js/rpgmaker_hook.js", "save/hook_translation_map.json"} <= created

    result = manifest.restore_and_uninstall()

    assert result["restored"] >= 3
    assert not (game / "js" / "rpgmaker_hook.js").exists()
    assert not (game / "save" / "hook_translation_map.json").exists()
    assert "rpgmaker_hook.js" not in (game / "index.html").read_text(encoding="utf-8")
    assert json.loads((game / "package.json").read_text(encoding="utf-8")) == {"main": "index.html"}


def test_rpgmaker_scan_prefers_static_without_injecting_scan_hook(monkeypatch, tmp_path):
    import core.rpgmaker_runtime as runtime

    game = tmp_path / "game"
    game.mkdir()
    item = TextItem(file="data/CommonEvents.json", key="1", original="こんにちは")

    monkeypatch.setattr(runtime, "_scan_static", lambda game_dir: [item])

    def fail_hook_scan(*_args, **_kwargs):
        raise AssertionError("hook scan should not run when static RPGMaker scan succeeds")

    monkeypatch.setattr(runtime, "_scan_via_hook", fail_hook_scan)

    items = runtime.scan(game)

    assert items == [item]


def test_rpgmaker_deploy_installs_replace_hook_without_prior_scan(monkeypatch, tmp_path):
    manifest_mod = _use_temp_manifest_dirs(monkeypatch, tmp_path)
    from core.rpgmaker_runtime import translate_and_deploy

    game = _make_rpgmaker_game(tmp_path)
    (game / "index.html").write_text(
        '<script type="text/javascript" src="js/main.js"></script>\n',
        encoding="utf-8",
    )
    (game / "js" / "main.js").write_text('var scriptUrls = ["js/plugins.js"];\n', encoding="utf-8")
    (game / "js" / "rpgmaker_hook.js").unlink()
    (game / "save" / "hook_translation_map.json").unlink()

    manifest = manifest_mod.GameManifest.for_game(game)
    count = translate_and_deploy(
        game,
        [TextItem(file="data/CommonEvents.json", key="1", original="こんにちは", translated="你好")],
        manifest=manifest,
    )

    assert count == 1
    hook_text = (game / "js" / "rpgmaker_hook.js").read_text(encoding="utf-8")
    assert 'var MODE = "replace"' in hook_text
    assert "rpgmaker_hook.js" in (game / "index.html").read_text(encoding="utf-8")
    assert json.loads((game / "save" / "hook_translation_map.json").read_text(encoding="utf-8")) == {
        "こんにちは": "你好"
    }


def test_rpgmaker_mv_unpack_extracts_without_translating(monkeypatch, tmp_path):
    import core.rpgmaker_runtime as runtime
    from engines.rpgmaker import RPGMakerEngine

    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    (game / "nw.dll").write_bytes(b"")
    (game / "data" / "CommonEvents.json").write_text(
        json.dumps([
            None,
            {
                "id": 1,
                "name": "intro",
                "list": [{"code": 401, "parameters": ["こんにちは"]}],
            },
        ], ensure_ascii=False),
        encoding="utf-8",
    )

    def fail_translate(*_args, **_kwargs):
        raise AssertionError("RPGMaker unpack must not call translators during extraction")

    monkeypatch.setattr(runtime, "translate_scanned_items", fail_translate)

    items = RPGMakerEngine().unpack(game / "Game.exe", tmp_path / "workspace")

    assert len(items) == 1
    assert items[0].original == "こんにちは"
    assert items[0].translated == ""
