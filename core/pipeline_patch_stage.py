"""patch 阶段：--from-json 仅回填模式，从 JSON 检查点读取翻译并回填/部署/启动。

模块级函数收 pipeline 作第一参数（Pipeline 类保留委托薄壳），
与 core/pipeline_stage_runtime.py 的拆分模式一致。
"""
from __future__ import annotations

from pathlib import Path

from core.diagnostics import Diagnostics
from core.engine_capabilities import can_extract, can_repack
from core.manifest import GameManifest
from core.path_resolver import resolve_game_path
from utils.logger import error, info, warning
from utils.text_extract import is_acceptable_same_as_source, validation_source_for_item, verify_translation


async def do_patch_only(pipeline, game_path: Path, json_path: Path,
                        launch: bool, injector: str | None) -> bool:
    """仅回填模式：从 JSON 读取翻译，跳过提取和翻译。"""
    from core import pipeline as _pipeline_mod
    import json as _json

    game_path = Path(resolve_game_path(game_path))

    if pipeline.diagnostics is None and pipeline.workspace is not None:
        pipeline.diagnostics = Diagnostics(pipeline.workspace.root)
        pipeline.diagnostics.set("input_path", str(game_path.resolve()))
        pipeline.diagnostics.set("mode", {
            "checkpoint": True,
            "patch_only": True,
            "injector": injector,
            "launch": launch,
        })

    if not json_path.exists():
        if pipeline.diagnostics:
            pipeline.diagnostics.error("JSON 检查点不存在", json_path=json_path)
            pipeline.diagnostics.finish(False)
        error(f"JSON 检查点不存在: {json_path}")
        return False

    data = _json.loads(json_path.read_text(encoding="utf-8"))

    engine = pipeline._select_engine(game_path)
    if engine is None:
        if pipeline.diagnostics:
            pipeline.diagnostics.error("无法识别游戏引擎，JSON 回填模式需要已知引擎", path=game_path)
            pipeline.diagnostics.finish(False)
        error("无法识别游戏引擎，JSON 回填模式暂不支持")
        return False
    pipeline.manifest = GameManifest.for_game(game_path)
    pipeline.manifest.set_engine(engine)

    injector = pipeline._auto_select_injector(injector, engine, game_path)

    raw_items = data.get("items", [])
    if getattr(engine, "name", "") == "kirikiri":
        raw_items = _pipeline_mod._clean_kirikiri_checkpoint_items(raw_items)

    translated = []
    for r in raw_items:
        if r.get("translated") and r["translated"] != r["original"]:
            source_for_validation = validation_source_for_item(type("_CheckpointItem", (), {
                "original": r["original"],
                "meta": r.get("meta", {}),
            })())
            safe_translated, _warns = verify_translation(source_for_validation, r["translated"])
            if not safe_translated or (
                safe_translated == r["original"]
                and not is_acceptable_same_as_source(r["original"], safe_translated)
            ):
                continue
            from engines.base import TextItem
            translated.append(TextItem(
                file=r["file"], key=r.get("key", ""),
                original=r["original"], translated=safe_translated,
                context=r.get("context", ""),
                line=r.get("line", 0),
                meta=r.get("meta", {}),
            ))

    if getattr(engine, "name", "") == "kirikiri" and _pipeline_mod._has_runtime_capture_items(translated):
        if pipeline.diagnostics:
            pipeline.diagnostics.set("injector", injector)
            pipeline.diagnostics.set("runtime_overlay_only", True)

    if hasattr(engine, "filter_repack_items"):
        translated = engine.filter_repack_items(translated)

    if not translated:
        if pipeline.diagnostics:
            pipeline.diagnostics.warn("JSON 中没有已翻译的条目", json_path=json_path)
            pipeline.diagnostics.finish(False)
        warning("JSON 中没有已翻译的条目")
        return False

    info(f"从 JSON 加载: {len(translated)} 条翻译")
    if pipeline.diagnostics:
        pipeline.diagnostics.set("patch_source_json", str(json_path.resolve()))
        pipeline.diagnostics.set("translation_stats", {
            "translated": len(translated),
            "total": data.get("total", len(translated)),
            "blocked": 0,
        })
    source_lang = data.get("source_lang", "en")
    engine._source_lang = source_lang  # 传递源语言给 repack 阶段

    pipeline._update_progress("回填文本", 70)
    runtime_resource_overlay = _pipeline_mod._should_use_runtime_resource_overlay(injector, engine, game_path, translated)
    runtime_overlay_only = _pipeline_mod._should_use_runtime_overlay(injector, engine, game_path, translated) and not runtime_resource_overlay
    if runtime_overlay_only:
        info(_pipeline_mod._runtime_overlay_note(injector, engine))
        if pipeline.diagnostics:
            pipeline.diagnostics.set("runtime_overlay_only", True)
        pipeline._disable_kirikiri_static_patch_artifacts_for_runtime(game_path, engine)
    elif runtime_resource_overlay:
        info(_pipeline_mod._runtime_overlay_note(injector, engine))
        if pipeline.diagnostics:
            pipeline.diagnostics.set("runtime_resource_overlay", True)
        pipeline._disable_kirikiri_static_patch_artifacts_for_runtime(game_path, engine)
        _pipeline_mod._mark_runtime_resource_overlay(engine, True)
        pipeline._setup_engine_repack(engine, game_path)
        pipeline._artifact_snapshot = pipeline.manifest.snapshot_tool_artifacts() if pipeline.manifest else set()
        if not can_repack(engine):
            if pipeline.diagnostics:
                pipeline.diagnostics.warn("当前引擎不支持自动回填", engine=getattr(engine, "name", ""))
                pipeline.diagnostics.finish(False)
            return False
        if getattr(engine, "name", "") == "kirikiri":
            pipeline._prepare_kirikiri_repack_sources_from_meta_dump(game_path, translated)
        if pipeline._workspace_has_repack_sources(translated):
            info("回填资源已由检查点工作区提供，跳过重新提取")
            if pipeline.diagnostics:
                pipeline.diagnostics.set("repack_source_prepare_skipped", True)
        elif can_extract(engine):
            engine._progress = pipeline.progress
            try:
                engine.unpack(game_path, pipeline.workspace.root)
            finally:
                engine._progress = None
        engine.repack(items=translated, workspace=pipeline.workspace.root)
    else:
        pipeline._setup_engine_repack(engine, game_path)
        pipeline._artifact_snapshot = pipeline.manifest.snapshot_tool_artifacts() if pipeline.manifest else set()
        if not can_repack(engine):
            if pipeline.diagnostics:
                pipeline.diagnostics.warn("当前引擎不支持自动回填", engine=getattr(engine, "name", ""))
                pipeline.diagnostics.suggest("该引擎需要专用工具手动回填资源。")
                pipeline.diagnostics.finish(False)
            return False
        if getattr(engine, "name", "") == "kirikiri":
            pipeline._prepare_kirikiri_repack_sources_from_meta_dump(game_path, translated)
        if pipeline._workspace_has_repack_sources(translated):
            info("回填资源已由检查点工作区提供，跳过重新提取")
            if pipeline.diagnostics:
                pipeline.diagnostics.set("repack_source_prepare_skipped", True)
        elif can_extract(engine):
            engine._progress = pipeline.progress
            try:
                engine.unpack(game_path, pipeline.workspace.root)
            finally:
                engine._progress = None
        pipeline._backup_game_files(game_path, translated, engine)
        engine.repack(items=translated, workspace=pipeline.workspace.root)

    pipeline._update_progress("字体替换", 75)
    if not runtime_overlay_only:
        _pipeline_mod._replace_fonts_if_needed(engine, game_path, pipeline.workspace.root)

    pipeline._update_progress("复制回游戏", 85)
    if runtime_resource_overlay:
        if pipeline.manifest:
            pipeline.manifest.record_new_tool_artifacts(pipeline._artifact_snapshot, engine)
    elif not runtime_overlay_only:
        _pipeline_mod._copy_back_safe(pipeline.workspace, game_path, translated, engine)
        pipeline._record_modified_outputs(game_path, translated, engine)
        if pipeline.manifest:
            pipeline.manifest.record_new_tool_artifacts(pipeline._artifact_snapshot, engine)
    if pipeline.diagnostics:
        if runtime_overlay_only:
            pipeline.diagnostics.set("repack_verification", {
                "checked": True,
                "hits": 0,
                "files_checked": 0,
                "note": "运行时显示层 hook 模式，未写回游戏文本文件",
            })
        else:
            pipeline.diagnostics.set("repack_verification", pipeline._verify_repack_outputs(game_path, translated, engine))
    pipeline._prepare_runtime_dependencies(game_path, engine)
    pipeline._create_runtime_launchers(game_path, engine, json_path)

    if launch:
        pipeline._update_progress("启动游戏", 95)
        _pipeline_mod._launch_finished_game(game_path, engine, injector, json_path)
    else:
        pipeline._stop_translation_proxy(engine)

    pipeline._update_progress("完成", 100)
    info("回填完成！")
    pipeline._write_completion_notice(game_path, engine, mode="JSON 回填")
    if pipeline.diagnostics:
        pipeline.diagnostics.finish(True)
    return True
