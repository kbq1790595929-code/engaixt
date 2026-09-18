from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.pipeline as pipeline_mod
import core.pipeline_context as pipeline_context
import core.pipeline_kirikiri_dump as kirikiri_dump_mod
import core.pipeline_runtime_stage as runtime_stage_mod
import core.pipeline_stage_runtime as stage_runtime
from config import Config
from core.manifest import GameManifest
from core.pipeline import (
    Pipeline,
    _copy_back_safe,
    _prepare_kirikiri_krkrpatch_runtime,
    _should_use_runtime_resource_overlay,
    _should_use_runtime_overlay,
    _stop_processes_under_dir,
    _write_translation_notice,
)
from core.pipeline_stages import stage_plan
from engines.base import EngineBase, EngineCapabilities, TextItem
from engines.kirikiri.xp3 import _write_xp3_patch


class _ContractEngine(EngineBase):
    name = "contract"
    label = "Contract Engine"
    support_level = "beta"
    capabilities = EngineCapabilities(
        extract=True,
        repack=True,
        static_patch=True,
        runtime_patch=True,
        creates_launcher=True,
        portable_after_patch=True,
    )

    def detect(self, path: Path) -> bool:
        return True

    def unpack(self, path: Path, workspace: Path):
        return []

    def repack(self, items, workspace: Path) -> None:
        return None


class _NoExtractEngine(_ContractEngine):
    name = "no_extract"
    capabilities = EngineCapabilities(extract=False, repack=False, static_patch=False)


def test_run_context_writes_stage_plan_and_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_context, "get_config", lambda: Config(workspace_dir=str(tmp_path)))

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    pipe = Pipeline()

    config = pipe._begin_run_context(
        str(game_dir),
        game_dir,
        {"checkpoint": False, "injector": None, "launch": False},
    )

    assert config.workspace_dir == str(tmp_path)
    assert pipe.workspace.root.parent == tmp_path
    assert pipe.diagnostics.data["input_path"] == str(game_dir)
    assert pipe.diagnostics.data["mode"]["checkpoint"] is False
    assert pipe.diagnostics.data["stage_plan"] == stage_plan()
    assert (pipe.workspace.root / "diagnostics.json").exists()


def test_run_context_records_runtime_and_safe_config_snapshot(tmp_path, monkeypatch):
    secret = "sk-test-secret-value"
    config = Config(
        workspace_dir=str(tmp_path),
        active_translator="deepseek",
        deepseek_api_key=secret,
        openai_api_key="openai-secret",
        deepseek_model="deepseek-v4-flash",
        max_concurrency=100,
        translation_coverage=75,
        translation_cache_enabled=True,
        translation_cache_max_size_gb=1.5,
    )
    monkeypatch.setattr(pipeline_context, "get_config", lambda: config)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    pipe = Pipeline()

    pipe._begin_run_context(str(game_dir), game_dir, {"checkpoint": False})

    runtime = pipe.diagnostics.data["app_runtime"]
    snapshot = pipe.diagnostics.data["config_snapshot"]
    serialized = json.dumps(snapshot, ensure_ascii=False)

    assert runtime["app"] == "EngAixt"
    assert "version" in runtime
    assert "packaged" in runtime
    assert "sys_executable" in runtime
    assert "executable_size" in runtime
    assert snapshot["active_translator"] == "deepseek"
    assert snapshot["active_translator_model"] == "deepseek-v4-flash"
    assert snapshot["max_concurrency"] == 100
    assert snapshot["translation_coverage"] == 75
    assert snapshot["translation_cache_enabled"] is True
    assert snapshot["translation_cache_max_size_gb"] == 1.5
    assert "api_key" not in serialized.lower()
    assert "secret" not in serialized.lower()
    assert secret not in serialized


def test_kirikiri_captured_only_preserves_static_items_and_merges_runtime_items(tmp_path, monkeypatch):
    config = Config(workspace_dir=str(tmp_path), kirikiri_runtime_completion_mode="captured_only")
    monkeypatch.setattr(pipeline_mod, "get_config", lambda: config)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    pipe = Pipeline()
    pipe._begin_run_context(str(game_dir), game_dir, {"checkpoint": False})

    static_items = [TextItem(file="scene.ks", key=f"static_{i}", original=f"静态{i}") for i in range(4852)]
    runtime_items = [TextItem(
        file="__kirikiri_runtime_capture__.jsonl",
        key=f"runtime_{i}",
        original=f"运行时{i}",
        meta={"runtime_capture": True},
    ) for i in range(28)]

    selected = pipe._apply_configured_translation_scope(
        type("KiriKiriEngine", (), {"name": "kirikiri"})(),
        static_items + runtime_items,
        100,
        note="回归测试",
    )

    assert len(selected) == 4880
    assert selected[:4852] == static_items
    assert pipe.diagnostics.data["kirikiri_static_items_preserved"] is True
    assert pipe.diagnostics.data["kirikiri_static_item_count"] == 4852
    assert pipe.diagnostics.data["kirikiri_runtime_capture_count"] == 28
    assert pipe.diagnostics.data["kirikiri_translation_scope_count"] == 4880


def test_kirikiri_captured_only_still_limits_pure_runtime_items(tmp_path, monkeypatch):
    config = Config(workspace_dir=str(tmp_path), kirikiri_runtime_completion_mode="captured_only")
    monkeypatch.setattr(pipeline_mod, "get_config", lambda: config)

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    pipe = Pipeline()
    pipe._begin_run_context(str(game_dir), game_dir, {"checkpoint": False})
    items = [TextItem(
        file="__kirikiri_runtime_capture__.jsonl",
        key=str(i),
        original=f"运行时{i}",
        meta={"runtime_capture": True},
    ) for i in range(28)]

    selected = pipe._apply_configured_translation_scope(
        type("KiriKiriEngine", (), {"name": "kirikiri"})(), items, 100,
    )

    assert selected == items
    assert pipe.diagnostics.data["kirikiri_static_items_preserved"] is False
    assert pipe.diagnostics.data["kirikiri_static_item_count"] == 0
    assert pipe.diagnostics.data["kirikiri_runtime_capture_count"] == 28


def test_translation_pipeline_does_not_check_tool_updates_during_game_tasks():
    assert "_auto_update_tools_if_due" not in inspect.getsource(Pipeline.run_async)
    assert "_auto_update_tools_if_due" not in inspect.getsource(Pipeline._run_checkpoint_async)


def test_stage_progress_updates_current_stage(tmp_path):
    pipe = Pipeline()
    pipe._begin_run_context(str(tmp_path), tmp_path, {"checkpoint": False})

    pipe._update_progress("translate")

    assert pipe.diagnostics.data["current_stage"] == {
        "key": "translate",
        "label": "AI 翻译",
        "progress": 40,
    }


def test_archive_stage_failure_finishes_diagnostics(tmp_path, monkeypatch):
    pipe = Pipeline()
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"not a real archive")
    pipe._begin_run_context(str(archive), archive, {"checkpoint": False})

    monkeypatch.setattr(stage_runtime, "is_archive", lambda path: True)
    monkeypatch.setattr(stage_runtime, "extract_archive", lambda path, out: False)

    assert pipe._run_archive_stage(archive) is None
    assert pipe.diagnostics.data["success"] is False
    assert pipe.diagnostics.data["failure"]["code"] == "archive_extract_failed"
    assert "压缩包无法解压" in pipe.diagnostics.data["failure"]["user_message"]
    assert "current_stage" in pipe.diagnostics.data
    assert pipe.diagnostics.data["current_stage"]["key"] == "archive"


def test_kirikiri_static_failure_is_readable_and_prepares_confirmed_fallback(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False})
    pipe.manifest = GameManifest.for_game(game)
    engine = _ContractEngine()
    engine.name = "kirikiri"

    launcher = game / "EngAixt_KRKR_Realtime.bat"

    def fake_launcher(game_path, engine=None, checkpoint=None):
        launcher.write_text("@echo off\n", encoding="utf-8")
        return launcher

    monkeypatch.setattr(pipeline_mod, "create_kirikiri_native_launcher", fake_launcher)

    assert pipe._fail_kirikiri_static_stage(
        game,
        engine,
        None,
        stage="script_extract",
        code="krkr_static_extract_failed",
        detail="internal=0; garbro=0; msg_tool=no_output",
    ) is False

    failure = pipe.diagnostics.data["failure"]
    assert failure["stage"] == "script_extract"
    assert failure["code"] == "krkr_static_extract_failed"
    assert failure["fallback"] == "realtime"
    assert failure["fallback_available"] is True
    assert failure["rollback"] is True
    assert failure["actions"] == ["改用实时翻译"]
    assert failure["tool_attempts"] == []
    assert launcher.exists()
    assert pipe.diagnostics.data["success"] is False


def test_engine_candidates_diagnostics_include_capabilities(tmp_path):
    pipe = Pipeline()
    pipe._begin_run_context(str(tmp_path), tmp_path, {"checkpoint": False})

    engine = _ContractEngine()
    old_detect = pipeline_mod.detect_engine_candidates
    pipeline_mod.detect_engine_candidates = lambda path: [(engine, 99, ["synthetic"])]
    try:
        assert pipe._select_engine(tmp_path) is engine
    finally:
        pipeline_mod.detect_engine_candidates = old_detect

    candidates = pipe.diagnostics.data["engine_candidates"]
    assert candidates[0]["name"] == "contract"
    caps = candidates[0]["support"]["capabilities"]
    assert caps["static_patch"] is True
    assert caps["runtime_patch"] is True
    assert caps["creates_launcher"] is True
    assert caps["portable_after_patch"] is True


def test_diagnostics_file_remains_json_ready(tmp_path):
    pipe = Pipeline()
    pipe._begin_run_context(str(tmp_path), tmp_path, {"checkpoint": True})
    pipe._update_progress("detect")

    data = json.loads(pipe.diagnostics.path.read_text(encoding="utf-8"))
    assert data["stage_plan"][0]["key"] == "archive"
    assert data["current_stage"]["key"] == "detect"


def test_translation_notice_contains_release_links_and_disclaimer(tmp_path):
    game = tmp_path / "game"
    game.mkdir()

    notice = _write_translation_notice(game, _ContractEngine(), mode="离线翻译/回填")

    assert notice == game / "EngAixt汉化说明.txt"
    text = notice.read_text(encoding="utf-8")
    assert "https://engaixt.com/" in text
    assert "免责声明" in text
    assert "Contract Engine" in text
    assert "离线翻译/回填" in text


def test_completion_notice_records_diagnostics_path(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False})

    pipe._write_completion_notice(game, _ContractEngine(), mode="JSON 回填")

    notice = game / "EngAixt汉化说明.txt"
    assert notice.exists()
    assert pipe.diagnostics.data["completion_notice"] == str(notice)


def test_detection_stage_records_preflight_and_suggestions(tmp_path, monkeypatch):
    pipe = Pipeline()
    pipe._begin_run_context(str(tmp_path), tmp_path, {"checkpoint": False})

    engine = _ContractEngine()
    manifest_calls = []

    class _Manifest:
        def set_engine(self, selected):
            manifest_calls.append(selected.name)

    monkeypatch.setattr(pipe, "_select_engine", lambda path: engine)
    monkeypatch.setattr(pipe, "_auto_select_injector", lambda injector, selected, path: "native")
    monkeypatch.setattr(stage_runtime.GameManifest, "for_game", lambda path: _Manifest())
    monkeypatch.setattr(
        stage_runtime,
        "run_preflight",
        lambda path, selected, injector: {
            "ok": False,
            "suggestions": ["install runtime"],
        },
    )

    selected, injector = pipe._run_detection_stage(tmp_path, None)

    assert selected is engine
    assert injector == "native"
    assert manifest_calls == ["contract"]
    assert pipe.diagnostics.data["current_stage"]["key"] == "detect"
    assert pipe.diagnostics.data["preflight"]["suggestions"] == ["install runtime"]
    assert pipe.diagnostics.data["suggestions"] == ["install runtime"]
    assert pipe.diagnostics.data["warnings"][0]["message"] == "预检发现缺失依赖，流程会继续但成功率可能下降"


def test_extract_stage_filters_files_and_records_diagnostics(tmp_path):
    pipe = Pipeline()
    pipe._begin_run_context(str(tmp_path), tmp_path, {"checkpoint": False})

    engine = _ContractEngine()
    items = [
        TextItem(file="scenario/intro.ks", original="one"),
        TextItem(file="system/menu.ks", original="two"),
    ]
    engine.unpack = lambda path, workspace: list(items)

    selected, filtered, extracted_count = pipe._run_extract_stage(
        tmp_path,
        engine,
        ["scenario/*"],
    )

    assert selected is engine
    assert extracted_count == 2
    assert [item.original for item in filtered] == ["one"]
    assert pipe.diagnostics.data["current_stage"]["key"] == "extract"
    assert pipe.diagnostics.data["steps"][-1] == {
        "name": "file_filter",
        "status": "ok",
        "details": {"selected": 1, "total": 2, "patterns": ["scenario/*"]},
    }


def test_kirikiri_xp3_scripts_are_not_copied_back_as_loose_files(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    workspace = tmp_path / "ws"
    original = workspace / "original"
    original.mkdir(parents=True)
    (original / "scene.txt.scn").write_bytes(b"patched")

    item = TextItem(
        file="scene.txt.scn",
        original="old",
        translated="new",
        meta={"from_xp3": True, "runtime_dump": True},
    )
    engine = _ContractEngine()
    engine.name = "kirikiri"

    class _Workspace:
        root = workspace

    _copy_back_safe(_Workspace(), game, [item], engine)

    assert not (game / "scene.txt.scn").exists()


def test_kirikiri_krkrpatch_config_omits_loose_dir_when_archive_patch_exists(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "krkr.exe"
    exe.write_bytes(b"MZ")
    meta = game / "_translation_meta"
    meta.mkdir()
    (meta / "kirikiri_patch.xp3").write_bytes(b"XP3")
    (meta / "kirikiri_patch").mkdir()

    calls = []

    def fake_prepare(game_dir, exe_path, patch_archives):
        calls.append((game_dir, exe_path, list(patch_archives)))
        return {"ok": True}

    monkeypatch.setattr("core.pipeline_runtime_stage._prepare_kirikiri_krkrpatch_runtime", fake_prepare)

    class _Engine:
        def find_exe(self, _path):
            return exe

    pipe = Pipeline()
    pipe._prepare_kirikiri_patch_bridge_tooling(game, _Engine())

    assert calls == [(game, exe, ["_translation_meta/kirikiri_patch.xp3"])]


def test_kirikiri_xp3pack_bridge_is_manifest_owned(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    source = tmp_path / "tools" / "version.dll"
    source.parent.mkdir()
    source.write_bytes(b"bridge")

    monkeypatch.setattr("core.tool_manager.find_tool", lambda name: source if name == "kirikiri_unencrypted_version" else None)

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False})
    pipe.manifest = GameManifest.for_game(game)
    engine = _ContractEngine()
    engine.name = "kirikiri"

    result = pipe._prepare_kirikiri_unencrypted_version_bridge(game, engine)

    assert result["ok"] is True
    assert result["status"] == "installed"
    assert (game / "version.dll").read_bytes() == b"bridge"
    assert any(item["rel"] == "version.dll" for item in pipe.manifest.data["created_files"])


def test_kirikiri_xp3_scripts_are_forgotten_from_modified_manifest(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    loose = game / "scene.txt.scn"
    loose.write_bytes(b"old loose copy")

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False})
    pipe.manifest = GameManifest.for_game(game)
    pipe.manifest.record_modified(loose)

    item = TextItem(
        file="scene.txt.scn",
        original="old",
        translated="new",
        meta={"from_xp3": True, "runtime_dump": True},
    )
    engine = _ContractEngine()
    engine.name = "kirikiri"

    pipe._record_modified_outputs(game, [item], engine)

    assert pipe.manifest.data["modified_files"] == []


def test_kirikiri_static_xp3_archives_are_backed_up_and_recorded(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    archive = game / "data.xp3"
    sig = game / "data.xp3.sig"
    archive.write_bytes(b"xp3")
    sig.write_bytes(b"sig")

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False})
    pipe.manifest = GameManifest.for_game(game)

    item = TextItem(
        file="scenario/intro.ks",
        original="old",
        translated="new",
        meta={"from_xp3": True, "xp3_filter": {"archive": "data.xp3", "key": 1}},
    )
    engine = _ContractEngine()
    engine.name = "kirikiri"

    pipe._backup_game_files(game, [item], engine)
    pipe._record_modified_outputs(game, [item], engine)

    modified = {Path(row["path"]).name for row in pipe.manifest.data["modified_files"]}
    assert {"data.xp3", "data.xp3.sig"} <= modified
    assert (pipe.manifest.backup_root / "data.xp3").exists()
    assert (pipe.manifest.backup_root / "data.xp3.sig").exists()


def test_record_modified_outputs_deduplicates_shared_archive_targets(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    archive = game / "Data.wolf"
    archive.write_bytes(b"archive")
    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False})
    pipe.manifest = GameManifest.for_game(game)
    engine = _ContractEngine()
    engine.name = "wolf"
    items = [
        TextItem(file="Data.wolf", original=f"original-{index}", translated=f"translated-{index}")
        for index in range(100)
    ]
    recorded: list[Path] = []
    monkeypatch.setattr(pipe.manifest, "record_modified", lambda target: recorded.append(target))

    pipe._record_modified_outputs(game, items, engine)

    assert recorded == [archive]


def test_kirikiri_protected_xp3_items_use_runtime_fallback_after_static_first(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    engine = _ContractEngine()
    engine.name = "kirikiri"
    item = TextItem(
        file="scenario/intro.ks",
        original="old",
        translated="new",
        meta={"from_xp3": True, "xp3_filter": {"archive": "data.xp3", "kind": "koihazi_xp3dec"}},
    )

    assert _should_use_runtime_overlay(None, engine, game, [item]) is True
    assert _should_use_runtime_resource_overlay(None, engine, game, [item]) is True


def test_kirikiri_static_first_uses_resource_overlay_for_protected_xp3(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    engine = _ContractEngine()
    engine.name = "kirikiri"
    item = TextItem(
        file="scenario/intro.ks",
        original="old",
        translated="new",
        meta={"from_xp3": True, "xp3_filter": {"archive": "data.xp3", "kind": "koihazi_xp3dec"}},
    )

    assert _should_use_runtime_overlay(None, engine, game, [item]) is True
    assert _should_use_runtime_resource_overlay(None, engine, game, [item]) is True


def test_kirikiri_native_root_patch_no_longer_skips_display_overlay(tmp_path):
    game = tmp_path / "game"
    game.mkdir()

    class _NativeRootPatchEngine(_ContractEngine):
        name = "kirikiri"

        def _should_use_native_root_patch(self, _game_dir, changed=None):
            return True

    item = TextItem(
        file="scenario/intro.ks",
        original="old",
        translated="new",
        meta={"from_xp3": True, "xp3_filter": {"archive": "data.xp3", "kind": "koihazi_xp3dec"}},
    )

    engine = _NativeRootPatchEngine()

    assert _should_use_runtime_overlay(None, engine, game, [item]) is True
    assert _should_use_runtime_resource_overlay(None, engine, game, [item]) is True


def test_kirikiri_empty_extract_bootstraps_runtime_capture_mode(tmp_path, monkeypatch):
    config = Config(workspace_dir=str(tmp_path / "workspaces"), keep_workspace=True)
    monkeypatch.setattr(pipeline_context, "get_config", lambda: config)
    monkeypatch.setattr(runtime_stage_mod, "get_config", lambda: config)

    game = tmp_path / "game"
    game.mkdir()
    engine = _ContractEngine()
    engine.name = "kirikiri"

    created: list[Path] = []

    def fake_launcher(game_path, engine=None, checkpoint=None):
        launcher = Path(game_path) / "启动汉化版.bat"
        launcher.write_text("@echo off\r\n", encoding="utf-8")
        created.append(launcher)
        return launcher

    monkeypatch.setattr(runtime_stage_mod, "create_kirikiri_native_launcher", fake_launcher)

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False, "launch": False})
    pipe.manifest = GameManifest.for_game(game)
    pipe.manifest.set_engine(engine)

    ok = pipe._enter_kirikiri_runtime_capture_mode(
        game,
        engine,
        None,
        None,
        launch=False,
        extract_only=False,
    )

    capture = game / "_translation_meta" / "kirikiri_runtime_capture.jsonl"
    assert ok is True
    assert capture.exists()
    assert created == [game / "启动汉化版.bat"]
    assert "success" not in pipe.diagnostics.data
    assert pipe.diagnostics.data["runtime_overlay_only"] is True
    assert pipe.diagnostics.data["kirikiri_runtime_capture_mode"]["reason"] == "static_extract_empty"
    assert pipe.diagnostics.data["kirikiri_runtime_capture_mode"]["capture"] == str(capture)


def test_kirikiri_runtime_resource_overlay_verifies_meta_patch(tmp_path):
    game = tmp_path / "game"
    patch_dir = game / "_translation_meta" / "kirikiri_patch" / "scenario"
    patch_dir.mkdir(parents=True)
    (patch_dir / "intro.ks").write_text("@start\n你好。\n", encoding="utf-8")
    (game / "_translation_meta" / "kirikiri_patch_manifest.txt").write_text(
        "scenario/intro.ks\n",
        encoding="utf-8",
    )

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False})
    engine = _ContractEngine()
    engine.name = "kirikiri"
    engine._runtime_resource_overlay = True
    item = TextItem(
        file="scenario/intro.ks",
        original="こんにちは。",
        translated="你好。",
        meta={"from_xp3": True},
    )

    result = pipe._verify_repack_outputs(game, [item], engine)

    assert result["checked"] is True
    assert result["hits"] == 1
    assert result["files_checked"] == 1
    assert result["note"] == "KiriKiri runtime overlay resource verification"


def test_kirikiri_static_root_xp3_is_verified_from_archive_payload(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    source = tmp_path / "source"
    (source / "scenario").mkdir(parents=True)
    script = source / "scenario" / "intro.ks"
    script.write_text("@start\n浣犲ソ。\n", encoding="utf-8")
    _write_xp3_patch(game / "patch.xp3", source, [script])

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False})
    engine = _ContractEngine()
    engine.name = "kirikiri"
    item = TextItem(
        file="scenario/intro.ks",
        original="こんにちは。",
        translated="浣犲ソ。",
        meta={"from_xp3": True},
    )

    result = pipe._verify_repack_outputs(game, [item], engine)

    assert result["checked"] is True
    assert result["hits"] == 1
    assert result["files_checked"] == 1
    assert result["patch_xp3"] == "patch.xp3"


def test_kirikiri_static_malformed_xp3_is_rejected(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    (game / "patch.xp3").write_bytes(b"not-an-xp3")

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False})
    engine = _ContractEngine()
    engine.name = "kirikiri"
    item = TextItem(
        file="scenario/intro.ks",
        original="こんにちは。",
        translated="你好。",
        meta={"from_xp3": True},
    )

    result = pipe._verify_repack_outputs(game, [item], engine)

    assert result["checked"] is False
    assert result["hits"] == 0
    assert result["archive_reports"][0]["readable"] is False


def test_kirikiri_static_source_xp3_rewrite_is_verified(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    script = source / "scenario.ks"
    script.write_text("@start\n你好。\n", encoding="utf-8")
    _write_xp3_patch(game / "data.xp3", source, [script])
    (game / "_translation_meta").mkdir()
    (game / "_translation_meta" / "kirikiri_static_xp3_rebuild.json").write_text(
        json.dumps({
            "rebuilt_archives": ["data.xp3"],
            "rebuilt_files": ["scenario.ks"],
            "rebuilt_files_by_archive": {"data.xp3": ["scenario.ks"]},
        }),
        encoding="utf-8",
    )

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False})
    engine = _ContractEngine()
    engine.name = "kirikiri"
    item = TextItem(
        file="scenario.ks",
        original="こんにちは。",
        translated="你好。",
        meta={"from_xp3": True},
    )

    result = pipe._verify_repack_outputs(game, [item], engine)

    assert result["checked"] is True
    assert result["hits"] == 1
    assert result["source_archive_rewrite_verified"]["hits"] == 1


def test_kirikiri_static_rejects_an_invalid_active_archive_even_with_another_hit(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    script = source / "scenario.ks"
    script.write_text("你好。", encoding="utf-8")
    _write_xp3_patch(game / "patch.xp3", source, [script])
    (game / "_translation_meta").mkdir()
    (game / "_translation_meta" / "kirikiri_patch.xp3").write_bytes(b"broken")

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False})
    engine = _ContractEngine()
    engine.name = "kirikiri"
    item = TextItem(
        file="scenario.ks",
        original="こんにちは。",
        translated="你好。",
        meta={"from_xp3": True},
    )

    result = pipe._verify_repack_outputs(game, [item], engine)

    assert result["hits"] == 1
    assert result["invalid_archives"] == ["_translation_meta/kirikiri_patch.xp3"]
    assert pipe._kirikiri_verification_failed(result) is True


def test_kirikiri_rollback_restores_preexisting_patch_archive_byte_for_byte(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    patch = game / "patch.xp3"
    original = b"previous-patch-content"
    patch.write_bytes(original)

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False})
    pipe.manifest = GameManifest.for_game(game)
    pipe.manifest.backup_file(patch)
    pipe._artifact_snapshot = pipe.manifest.snapshot_tool_artifacts()
    patch.write_bytes(b"new-broken-patch")

    assert pipe._rollback_game_changes(game) is True
    assert patch.read_bytes() == original


def test_kirikiri_rollback_keeps_old_loose_patch_files_and_removes_new_ones(tmp_path):
    game = tmp_path / "game"
    old_patch = game / "_translation_meta" / "kirikiri_patch" / "old.ks"
    old_patch.parent.mkdir(parents=True)
    old_patch.write_text("old", encoding="utf-8")

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False})
    pipe.manifest = GameManifest.for_game(game)
    pipe._artifact_snapshot = pipe.manifest.snapshot_tool_artifacts() if pipe.manifest else set()
    new_patch = old_patch.parent / "new.ks"
    new_patch.write_text("new", encoding="utf-8")

    pipe._rollback_game_changes(game)

    assert old_patch.read_text(encoding="utf-8") == "old"
    assert not new_patch.exists()


def test_kirikiri_runtime_overlay_disables_static_patch_artifacts(tmp_path):
    game = tmp_path / "game"
    meta = game / "_translation_meta"
    meta.mkdir(parents=True)
    (game / "patch.xp3").write_bytes(b"patch")
    (meta / "kirikiri_patch.xp3").write_bytes(b"bridge")
    (meta / "kirikiri_patch_manifest.txt").write_text("scenario/intro.ks\n", encoding="utf-8")
    patch_dir = meta / "kirikiri_patch"
    patch_dir.mkdir()
    (patch_dir / "intro.ks").write_text("patched", encoding="utf-8")

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": False})
    engine = _ContractEngine()
    engine.name = "kirikiri"

    pipe._disable_kirikiri_static_patch_artifacts_for_runtime(game, engine)

    assert not (game / "patch.xp3").exists()
    assert not (meta / "kirikiri_patch.xp3").exists()
    assert not (meta / "kirikiri_patch_manifest.txt").exists()
    assert not patch_dir.exists()
    assert (game / "patch.xp3.disabled_runtime_overlay").exists()
    assert (meta / "kirikiri_patch.xp3.disabled_runtime_overlay").exists()
    assert (meta / "kirikiri_patch_manifest.txt.disabled_runtime_overlay").exists()
    assert (meta / "kirikiri_patch.disabled_runtime_overlay").is_dir()


def test_extract_stage_uses_generic_fallback_for_unsupported_engine(tmp_path, monkeypatch):
    pipe = Pipeline()
    pipe._begin_run_context(str(tmp_path), tmp_path, {"checkpoint": False})

    fallback = _ContractEngine()
    fallback.name = "fallback"
    fallback.unpack = lambda path, workspace: [TextItem(file="plain.txt", original="text")]
    monkeypatch.setattr(pipe, "_fallback_generic", lambda path: fallback)

    selected, items, extracted_count = pipe._run_extract_stage(tmp_path, _NoExtractEngine(), None)

    assert selected is fallback
    assert extracted_count == 1
    assert items[0].original == "text"
    assert pipe.diagnostics.data["warnings"][0]["details"]["engine"] == "no_extract"


def test_coverage_limit_keeps_order_and_records_selected_count(tmp_path):
    pipe = Pipeline()
    pipe._begin_run_context(str(tmp_path), tmp_path, {"checkpoint": False})
    items = [TextItem(file="script", original=str(i)) for i in range(10)]

    selected = pipe._apply_coverage_limit(items, 35, note="按游戏进度顺序")

    assert [item.original for item in selected] == ["0", "1", "2"]
    assert pipe.diagnostics.data["steps"][-1] == {
        "name": "coverage",
        "status": "ok",
        "details": {"selected": 3, "total": 10, "coverage": 35},
    }


def test_language_stage_records_source_and_target(tmp_path, monkeypatch):
    pipe = Pipeline()
    pipe._begin_run_context(str(tmp_path), tmp_path, {"checkpoint": False})
    monkeypatch.setattr(stage_runtime, "detect_batch_language", lambda texts: "ja")

    source, target = pipe._detect_language_stage(
        [TextItem(file="script", original="こんにちは")],
        "zh-CN",
    )

    assert (source, target) == ("ja", "zh-CN")
    assert pipe.diagnostics.data["current_stage"]["key"] == "language"
    assert pipe.diagnostics.data["languages"] == {"source": "ja", "target": "zh-CN"}


def test_pipeline_stage_methods_delegate_to_runtime_modules(monkeypatch, tmp_path):
    pipe = Pipeline()
    calls = []

    monkeypatch.setattr(
        pipeline_context,
        "begin_run_context",
        lambda *args: calls.append(("context", args[1:])) or "config",
    )
    monkeypatch.setattr(
        stage_runtime,
        "run_archive_stage",
        lambda *args: calls.append(("archive", args[1:])) or tmp_path,
    )
    monkeypatch.setattr(
        stage_runtime,
        "run_detection_stage",
        lambda *args: calls.append(("detect", args[1:])) or ("engine", "injector"),
    )
    monkeypatch.setattr(
        stage_runtime,
        "run_extract_stage",
        lambda *args: calls.append(("extract", args[1:])) or ("engine", ["item"], 1),
    )
    monkeypatch.setattr(
        stage_runtime,
        "detect_language_stage",
        lambda *args: calls.append(("language", args[1:])) or ("ja", "zh-CN"),
    )
    monkeypatch.setattr(
        stage_runtime,
        "apply_coverage_limit",
        lambda *args, **kwargs: calls.append(("coverage", args[1:], kwargs)) or ["kept"],
    )

    assert pipe._begin_run_context("input", tmp_path, {"checkpoint": False}) == "config"
    assert pipe._run_archive_stage(tmp_path) == tmp_path
    assert pipe._run_detection_stage(tmp_path, None) == ("engine", "injector")
    assert pipe._run_extract_stage(tmp_path, "engine", ["*.ks"]) == ("engine", ["item"], 1)
    assert pipe._detect_language_stage(["item"], "zh-CN") == ("ja", "zh-CN")
    assert pipe._apply_coverage_limit(["item"], 50, note="test") == ["kept"]

    assert [call[0] for call in calls] == [
        "context",
        "archive",
        "detect",
        "extract",
        "language",
        "coverage",
    ]


def test_checkpoint_resume_candidate_requires_current_game_source(tmp_path):
    game = tmp_path / "game"
    meta = game / "_translation_meta"
    meta.mkdir(parents=True)
    checkpoint = meta / "translation_checkpoint.json"
    checkpoint.write_text(
        json.dumps({
            "source": str(game),
            "source_lang": "ja",
            "target_lang": "zh-CN",
            "items": [{"file": "scene.ks", "original": "こんにちは", "translated": ""}],
        }),
        encoding="utf-8",
    )

    pipe = Pipeline()

    assert pipe._checkpoint_resume_candidate(game) == checkpoint
    assert pipe._checkpoint_resume_candidate(game, ["scene/*"]) is None

    checkpoint.write_text(
        json.dumps({
            "source": str(tmp_path / "other"),
            "items": [{"file": "scene.ks", "original": "こんにちは", "translated": ""}],
        }),
        encoding="utf-8",
    )

    assert pipe._checkpoint_resume_candidate(game) is None


def test_rpgmaker_old_line_checkpoint_is_not_fast_resumed(tmp_path):
    game = tmp_path / "game"
    (game / "www" / "data").mkdir(parents=True)
    (game / "www" / "data" / "CommonEvents.json").write_text("[]", encoding="utf-8")
    meta = game / "_translation_meta"
    meta.mkdir()
    checkpoint = meta / "translation_checkpoint.json"
    checkpoint.write_text(
        json.dumps({
            "source": str(game),
            "items": [{
                "file": "hook",
                "original": "\\n<\u304a\u3058\u3058>\u300c\u5916\u306e\u8a71\u300d",
                "translated": "\u9519\u4f4d\u65e7\u8bd1\u6587",
            }],
        }),
        encoding="utf-8",
    )

    assert Pipeline()._checkpoint_resume_candidate(game) is None


def test_kirikiri_fast_resume_merges_runtime_capture_into_checkpoint(tmp_path, monkeypatch):
    game = tmp_path / "game"
    meta = game / "_translation_meta"
    meta.mkdir(parents=True)
    checkpoint = meta / "translation_checkpoint.json"
    checkpoint.write_text(
        json.dumps({
            "source": str(game),
            "source_lang": "ja",
            "target_lang": "zh-CN",
            "items": [
                {
                    "file": "scene.ks",
                    "key": "line_1",
                    "original": "既存テキスト",
                    "translated": "现有文本",
                    "context": "message",
                    "line": 1,
                    "meta": {"engine": "kirikiri"},
                }
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (meta / "kirikiri_runtime_capture.jsonl").write_text(
        json.dumps({"text": "「なんだよ、またお前と同じクラスか」", "source": "kagparser"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    class _KiriKiriEngine:
        name = "kirikiri"

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": True})

    async def fake_translate(items, checkpoint_path, source_lang, target_lang, game_path):
        for item in items:
            if item.original == "「なんだよ、またお前と同じクラスか」":
                item.translated = "「什么啊，怎么又和你同班」"
        pipe._sync_checkpoint_json(checkpoint_path, items)
        return items

    async def fake_retry(items, checkpoint_path, source_lang, target_lang, game_path):
        return items

    async def fake_patch(path, checkpoint_path, launch, injector):
        data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        captured = [
            raw for raw in data["items"]
            if raw["original"] == "「なんだよ、またお前と同じクラスか」"
        ]
        assert captured
        assert captured[0]["translated"] == "「什么啊，怎么又和你同班」"
        assert captured[0]["meta"]["runtime_capture"] is True
        return True

    monkeypatch.setattr(pipe, "_reset_api_cache_stats", lambda _name: None)
    monkeypatch.setattr(pipe, "_record_api_cache_stats", lambda _path, _total: None)
    monkeypatch.setattr(pipe, "_translate_with_checkpoint", fake_translate)
    monkeypatch.setattr(pipe, "_validate_all", lambda items: items)
    monkeypatch.setattr(pipe, "_retry_invalid_translations", fake_retry)
    monkeypatch.setattr(pipe, "_do_patch_only", fake_patch)

    ok = asyncio.run(pipe._run_checkpoint_resume_fast_path(
        game,
        _KiriKiriEngine(),
        None,
        checkpoint,
        False,
    ))

    assert ok is True
    assert pipe.diagnostics.data["kirikiri_runtime_capture_merged"] == 1


def test_checkpoint_sync_updates_all_duplicate_engine_bindings(tmp_path):
    game = tmp_path / "game"
    meta = game / "_translation_meta"
    meta.mkdir(parents=True)
    checkpoint = meta / "translation_checkpoint.json"
    raw_items = [
        {
            "file": "Data.wolf",
            "key": "/commands/1/stringArgs/0",
            "original": "同じ台詞です。",
            "translated": "",
            "context": "WOLF command / Message",
            "line": 0,
            "meta": {"wolf_json": f"maps/Map00{index}.json"},
        }
        for index in (1, 2)
    ]
    checkpoint.write_text(
        json.dumps({"source": str(game), "translated_count": 0, "items": raw_items}, ensure_ascii=False),
        encoding="utf-8",
    )
    items = [
        TextItem(
            file=raw["file"],
            key=raw["key"],
            original=raw["original"],
            translated="相同的台词。",
            context=raw["context"],
            meta=raw["meta"],
        )
        for raw in raw_items
    ]

    Pipeline()._sync_checkpoint_json(checkpoint, items)

    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["translated_count"] == 2
    assert [row["translated"] for row in saved["items"]] == ["相同的台词。", "相同的台词。"]


def test_workspace_repack_sources_detects_reused_checkpoint_original(tmp_path):
    pipe = Pipeline()
    pipe._begin_run_context(str(tmp_path), tmp_path, {"checkpoint": True})
    original = pipe.workspace.root / "original"
    (original / "scenario").mkdir(parents=True)
    (original / "scenario" / "intro.ks").write_text("old", encoding="utf-8")

    items = [
        TextItem(
            file="scenario/intro.ks",
            original="こんにちは",
            translated="你好",
        )
    ]

    assert pipe._workspace_has_repack_sources(items) is True


def test_kirikiri_fast_repack_sources_use_meta_dump(tmp_path):
    game = tmp_path / "game"
    dump = game / "_translation_meta" / "kirikiri_dump" / "scenario"
    dump.mkdir(parents=True)
    (dump / "intro.ks").write_text("old", encoding="utf-8")

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": True})
    items = [
        TextItem(
            file="scenario/intro.ks",
            original="こんにちは",
            translated="你好",
            meta={"engine": "kirikiri", "from_xp3": True, "runtime_dump": True},
        )
    ]

    copied = pipe._prepare_kirikiri_repack_sources_from_meta_dump(game, items)

    assert copied == 1
    assert (pipe.workspace.root / "original" / "scenario" / "intro.ks").read_text(encoding="utf-8") == "old"
    assert pipe._workspace_has_repack_sources(items) is True


def test_process_cleanup_does_not_kill_commandline_only_matches(tmp_path, monkeypatch):
    commands = []

    class _Result:
        stdout = "0"

    def fake_run(args, **kwargs):
        commands.append(args)
        return _Result()

    monkeypatch.setattr(subprocess, "run", fake_run)

    _stop_processes_under_dir(tmp_path / "game")

    joined = "\n".join(str(arg) for call in commands for arg in call)
    assert "CommandLine.Contains($root)" not in joined
    assert "ExecutablePath.StartsWith($root" in joined


def test_kirikiri_runtime_dump_launch_is_disabled_by_default(tmp_path, monkeypatch):
    game = tmp_path / "game"
    meta = game / "_translation_meta"
    meta.mkdir(parents=True)
    (meta / "kirikiri_dump_targets.txt").write_text("scn.xp3>001\n", encoding="utf-8")

    pipe = Pipeline()
    pipe._begin_run_context(str(game), game, {"checkpoint": True})

    engine = _ContractEngine()
    engine.name = "kirikiri"

    monkeypatch.setattr(kirikiri_dump_mod, "_kirikiri_auto_dump_needed", lambda path, selected, items: True)
    monkeypatch.setattr(kirikiri_dump_mod, "_kirikiri_expected_script_count", lambda path, selected: 1)
    monkeypatch.setattr(
        kirikiri_dump_mod,
        "_run_kirikiri_external_dump_probe",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("external KrkrDump should be opt-in")),
    )
    monkeypatch.setattr(
        kirikiri_dump_mod,
        "_run_kirikiri_native_dump_probe",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("native dump launch should be opt-in")),
    )

    pipe._try_kirikiri_auto_dump_stage(game, engine, [])

    assert pipe.diagnostics.data["kirikiri_auto_dump"]["status"] == "skipped"
    assert pipe.diagnostics.data["kirikiri_auto_dump"]["reason"] == "dump_disabled_unstable"
