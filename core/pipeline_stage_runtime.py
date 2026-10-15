from __future__ import annotations

import fnmatch
from pathlib import Path

from core.engine_capabilities import can_extract
from core.pipeline_detect_stage import record_final_extraction
from core.manifest import GameManifest
from core.preflight import run_preflight
from utils.archive import extract_archive, find_game_root, is_archive
from utils.lang_detect import detect_batch_language
from utils.logger import error, info, warning


def run_archive_stage(pipeline, path: Path) -> Path | None:
    pipeline._update_progress("archive")
    if not is_archive(path):
        return path

    info(f"检测到压缩包: {path.name}")
    extract_dir = pipeline.workspace.root / "extracted"
    result = extract_archive(path, extract_dir)
    if not result:
        error("解压失败")
        pipeline._fail_stage_code(
            "archive",
            "archive_extract_failed",
            detail=f"archive={path}",
            rollback=True,
            next_actions=("确认压缩包完整，或先解压后选择游戏目录。",),
        )
        return None

    game_root = find_game_root(extract_dir) or extract_dir
    pipeline.diagnostics.set("resolved_game_path", str(game_root.resolve()))
    info(f"游戏根目录: {game_root}")
    return game_root


def run_detection_stage(pipeline, path: Path, injector: str | None):
    pipeline._update_progress("detect")
    engine = pipeline._select_engine(path)
    if engine is None:
        return None, injector

    pipeline.manifest = GameManifest.for_game(path)
    pipeline.manifest.set_engine(engine)
    try:
        from core.game_identity import resolve_game_identity

        identity = resolve_game_identity(path, manifest_data=pipeline.manifest.data)
        pipeline.manifest.set_game_identity(identity.title, identity.source)
        pipeline.diagnostics.set("game_identity", identity.to_dict())
    except Exception as exc:
        identity = None
        pipeline.diagnostics.set("game_identity", {
            "title": "",
            "source": "identity_error",
            "error": str(exc),
        })
    pipeline.diagnostics.set("engine", {
        "name": getattr(engine, "name", ""),
        "label": getattr(engine, "label", ""),
    })
    try:
        from core.usage_statistics import update_usage_run

        update_usage_run(
            getattr(pipeline, "usage_run_id", ""),
            engine=getattr(engine, "name", ""),
            game_title=identity.title if identity else "",
            title_source=identity.source if identity else "",
            game_path=str(path.resolve()),
        )
    except Exception:
        pass

    injector = pipeline._auto_select_injector(injector, engine, path)
    preflight = run_preflight(path, engine, injector)
    pipeline.diagnostics.set("preflight", preflight)
    for suggestion in preflight.get("suggestions", []):
        pipeline.diagnostics.suggest(suggestion)
    if not preflight.get("ok", True):
        blocking = {
            str(check.get("name") or "")
            for check in preflight.get("checks", [])
            if check.get("status") == "missing" and check.get("required", True)
        }
        if "game_dir_writable" in blocking:
            pipeline.diagnostics.warn(
                "游戏目录不可写（只读介质或权限不足），回填与检查点无法写回游戏目录",
                dir=str(path),
            )
        else:
            pipeline.diagnostics.warn("预检发现缺失依赖，流程会继续但成功率可能下降")
    return engine, injector


def run_extract_stage(pipeline, path: Path, engine, file_filter: list[str] | None = None):
    pipeline._update_progress("extract")
    pipeline._current_game_path = path
    if not can_extract(engine):
        pipeline._record_unsupported_engine(engine)
        engine = pipeline._fallback_generic(path) or engine
    pipeline._current_engine = engine

    engine._progress = pipeline.progress
    engine._manifest = getattr(pipeline, "manifest", None)
    try:
        items = engine.unpack(path, pipeline.workspace.root)
    except Exception as exc:
        pipeline._extract_stage_error = str(exc)
        items = []
        warning(f"{getattr(engine, 'label', '引擎')} 文本提取异常: {exc}")
    finally:
        engine._progress = None

    pipeline._record_extraction(engine, items)
    diagnostics_snapshot = getattr(engine, "diagnostics_snapshot", None)
    if callable(diagnostics_snapshot):
        try:
            snapshot = diagnostics_snapshot()
            if snapshot:
                pipeline.diagnostics.set("engine_stage_diagnostics", snapshot)
        except Exception as exc:
            pipeline.diagnostics.warn(
                "引擎阶段诊断汇总失败",
                engine=getattr(engine, "name", ""),
                error=str(exc),
            )
    pipeline._validate_extraction_quality(items)
    if (
        getattr(engine, "name", "") == "kirikiri"
        and getattr(engine, "_protected_archives", None)
        and not bool(getattr(engine, "_static_external_decrypt_succeeded", False))
    ):
        protected_count = int(getattr(engine, "_protected_script_count", 0) or 0)
        # 运行时捕获来自游戏运行时真正绘制过的文本，不是受保护脚本的噪声。
        # 噪声判定与丢弃都必须只针对静态候选：实测某游戏 data.xp3 由 XP3 解密
        # 插件保护、静态提取 0 条时，已有 24 条运行时捕获，却因为
        # ``24 < max(200, 169 * 2)`` 被连带清空，最终误报「没有提取出可解析脚本」。
        runtime_items = [
            item for item in items
            if bool((getattr(item, "meta", None) or {}).get("runtime_capture"))
        ]
        static_items = [
            item for item in items
            if not bool((getattr(item, "meta", None) or {}).get("runtime_capture"))
        ]
        if protected_count and len(static_items) < max(200, protected_count * 2):
            pipeline.diagnostics.set("runtime_dump_required", False)
            pipeline.diagnostics.set("static_decrypt_required", True)
            pipeline.diagnostics.set("kirikiri_protected_static_noise", {
                "items": len(items),
                "static_items": len(static_items),
                "runtime_items_kept": len(runtime_items),
                "protected_script_count": protected_count,
                "protected_archives": list(getattr(engine, "_protected_archives", [])),
                "protected_formats": sorted(getattr(engine, "_protected_formats", set())),
            })
            if runtime_items:
                warning(
                    "KiriKiri 静态提取疑似只得到受保护脚本的噪声，已丢弃 "
                    f"{len(static_items)} 条静态候选，保留运行时捕获文本 "
                    f"{len(runtime_items)} 条继续翻译"
                )
                items = runtime_items
            else:
                warning(
                    "KiriKiri 静态提取疑似只得到受保护脚本的噪声，已停止翻译；默认不运行 dump，等待静态解密支持"
                )
                items = []

    extracted_count = len(items)
    if items and file_filter:
        total_before = len(items)
        items = [
            item for item in items
            if any(fnmatch.fnmatch(item.file.lower(), pat.strip().lower()) for pat in file_filter)
        ]
        info(f"文件过滤: {'/'.join(file_filter)} -> 选中 {len(items)}/{total_before} 条")
        pipeline.diagnostics.step("file_filter", selected=len(items), total=total_before, patterns=file_filter)

    record_final_extraction(pipeline, engine, items)
    return engine, items, extracted_count


def detect_language_stage(pipeline, items: list, target_lang: str) -> tuple[str, str]:
    pipeline._update_progress("language")
    texts = [item.original for item in items]
    source_lang = detect_batch_language(texts)
    info(f"检测到源语言: {source_lang}")
    pipeline.diagnostics.set("languages", {"source": source_lang, "target": target_lang})
    return source_lang, target_lang


def apply_coverage_limit(pipeline, items: list, coverage_percent: int, *, note: str = "") -> list:
    total_count = len(items)
    coverage = max(1, min(100, coverage_percent))
    if coverage >= 100:
        return items

    count = max(1, int(total_count * coverage / 100))
    selected = items[:count]
    suffix = f"，{note}" if note else ""
    info(f"翻译覆盖: 前 {coverage}%（{count}/{total_count} 条{suffix}）")
    pipeline.diagnostics.step("coverage", selected=count, total=total_count, coverage=coverage)
    return selected
