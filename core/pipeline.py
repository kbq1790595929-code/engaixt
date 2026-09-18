from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from config import get_config
from core.detector import detect_engine_candidates
from core.diagnostics import Diagnostics
from core.engine_capabilities import can_extract, can_repack, engine_support_summary
from core.manifest import GameManifest
from core.path_resolver import resolve_game_path
from core.pipeline_failure import StageFailure, make_stage_failure, publish_stage_failure
from core.pipeline_stages import stage
from core.pipeline_lock import game_pipeline_lock
from core import (
    pipeline_checkpoint_stage,
    pipeline_detect_stage,
    pipeline_patch_stage,
    pipeline_runtime_stage,
    pipeline_context,
    pipeline_kirikiri_dump,
    pipeline_stage_runtime,
    pipeline_translate_stage,
)
from core.pipeline_runtime_stage import (  # noqa: F401 - re-export：兼容既有 import 与打桩
    _auto_fix_renpy_font,
    _copy_back_safe,
    _has_runtime_capture_items,
    _is_runtime_frida_mode,
    _kirikiri_can_use_native_root_patch,
    _kirikiri_items_have_protected_xp3_filter,
    _kirikiri_should_copy_loose_script,
    _kirikiri_should_use_runtime_overlay,
    _kirikiri_static_xp3_archives,
    _launch_finished_game,
    _mark_runtime_resource_overlay,
    _replace_fonts_if_needed,
    _runtime_overlay_note,
    _should_use_runtime_overlay,
    _should_use_runtime_resource_overlay,
    _stop_processes_under_dir,
    _write_translation_notice,
)
from core.pipeline_checkpoint_stage import (  # noqa: F401 - re-export：兼容既有 import 与打桩
    _checkpoint_item_rel,
    _clean_kirikiri_checkpoint_items,
    _looks_like_kirikiri_control_identifier,
    _safe_workspace_child,
)
from core.pipeline_translate_stage import _get_translator  # noqa: F401 - re-export：兼容既有 import 与打桩
from core.translator_callbacks import translate_batch_with_callbacks
from core.pipeline_kirikiri_dump import (  # noqa: F401 - re-export：兼容既有 import 与 monkeypatch 路径
    _KIRIKIRI_SCN_REF_CACHE,
    _KIRIKIRI_SCN_REF_INDEX_CACHE,
    _append_kirikiri_target_variants,
    _collect_kirikiri_dump_targets,
    _count_files,
    _count_kirikiri_dump_scripts,
    _default_kirikiri_seed_targets,
    _expand_kirikiri_target_name,
    _extend_kirikiri_targets_from_dump_refs,
    _infer_kirikiri_sequential_targets,
    _kirikiri_archive_prefixed_dump_rel,
    _kirikiri_auto_dump_needed,
    _kirikiri_count_threshold_sufficient,
    _kirikiri_dump_source_sort_key,
    _kirikiri_dump_target_closure_complete,
    _kirikiri_expected_script_count,
    _kirikiri_is_active_dump_target,
    _kirikiri_is_dump_target,
    _kirikiri_is_extensionless_dump_target,
    _kirikiri_primary_dump_targets,
    _kirikiri_rel_for_tool_config,
    _kirikiri_target_inner_name,
    _list_kirikiri_dump_script_rels,
    _load_kirikiri_auto_dump_seed_targets,
    _load_kirikiri_explicit_dump_targets,
    _load_kirikiri_scn_ref_index,
    _missing_kirikiri_active_dump_targets,
    _natural_kirikiri_target_sort_key,
    _prepare_kirikiri_auto_dump_seed_targets,
    _prepare_kirikiri_krkrdump_runtime,
    _prepare_kirikiri_krkrpatch_runtime,
    _read_kirikiri_dump_storage_refs,
    _read_kirikiri_dump_storage_refs_indexed,
    _run_kirikiri_external_dump_probe,
    _run_kirikiri_native_dump_probe,
    _safe_generated_child,
    _save_kirikiri_explicit_dump_targets,
    _save_kirikiri_scn_ref_index,
    _write_krkrdump_config,
)
from core.resources import resource_path
from core.workspace import Workspace, safe_replace
from core.launcher import (
    create_bgi_hook_launcher,
    create_godot_display_hook_launcher,
    create_kirikiri_native_launcher,
    launch_game,
    launch_translated_launcher,
    launch_with_injector,
    prepare_kirikiri_native_runtime,
    _overlay_window_command,
)
from core.translation_cache_db import load_translations_from_cache, save_translations_to_cache
from translators.cache import get_cache, get_cache_stats, reset_cache_stats
from utils.lang_detect import detect_batch_language
from utils.text_extract import is_acceptable_same_as_source, validation_source_for_item, verify_translation
from utils.logger import info, warning, error


def _is_core_translation_item(item) -> bool:
    meta = getattr(item, "meta", {}) or {}
    kind = str(getattr(item, "context", "") or meta.get("kind") or "").lower()
    if kind in {"name", "speaker"}:
        return False
    return True


def _has_effective_translation(item) -> bool:
    translated = getattr(item, "translated", "") or ""
    original = getattr(item, "original", "") or ""
    return bool(translated and (translated != original or is_acceptable_same_as_source(original, translated)))




def _detect_game_language(game_path: Path) -> str:
    """尝试从游戏文件检测源语言，不依赖已提取的文本条目。

    优先级：Steam manifest 语言 → 游戏文本文件采样 → 回退 "en"
    """
    from utils.lang_detect import detect_batch_language
    game_dir = game_path if game_path.is_dir() else game_path.parent

    # 1. 尝试扫描 StreamingAssets / 子目录中的文本文件采样
    sample_texts: list[str] = []
    for search_dir in [game_dir] + [d for d in game_dir.glob("*_Data") if d.is_dir()]:
        for ext in ["*.txt", "*.json", "*.csv", "*.xml", "*.lua"]:
            for f in search_dir.glob(ext):
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")[:4096]
                    # 提取引号中的字符串作为样本
                    import re
                    strings = re.findall(r'"([^"]{4,80})"', content)
                    sample_texts.extend(strings[:20])
                    if len(sample_texts) >= 100:
                        break
                except Exception:
                    continue
            if len(sample_texts) >= 100:
                break
        if len(sample_texts) >= 100:
            break

    if sample_texts:
        detected = detect_batch_language(sample_texts)
        if detected and detected != "unknown":
            info(f"从游戏文件检测到源语言: {detected}")
            return detected

    # 2. 回退：常见的 Unity 游戏默认为英文
    info("无法从文件检测源语言，默认使用 en")
    return "en"


class Pipeline:
    def __init__(self, progress_callback: object = None, item_progress_callback: object = None,
                 meta_callback: object = None):
        self.progress = progress_callback
        self.item_progress = item_progress_callback
        self.meta = meta_callback
        self.workspace: Workspace | None = None
        self.diagnostics: Diagnostics | None = None
        self.manifest: GameManifest | None = None
        self._artifact_snapshot: set[str] = set()
        self._blocked_count = 0
        self._checkpoint_path: Path | None = None
        self._item_progress_current = 0
        self._item_progress_total = 0
        self._item_progress_pct = 0.0

    def _meta(self, key: str, val):
        if self.meta:
            try:
                self.meta(key, val)
            except Exception:
                pass

    def _fail_stage(self, failure: StageFailure) -> bool:
        """Publish a readable failure and finish the current run once."""
        publish_stage_failure(self.diagnostics, self.meta, failure)
        if self.diagnostics:
            self.diagnostics.finish(False)
        return False

    def _fail_stage_code(self, stage: str, code: str, *, detail: str = "",
                         fallback: str = "", actions: tuple[str, ...] = (),
                         tool_attempts: tuple[dict, ...] = (),
                         rollback: bool = False, **extra) -> bool:
        return self._fail_stage(make_stage_failure(
            stage,
            code,
            detail=detail,
            fallback=fallback,
            actions=actions,
            tool_attempts=tool_attempts,
            rollback=rollback,
            **extra,
        ))

    def _fail_kirikiri_static_stage(
        self,
        game_path: Path,
        engine,
        checkpoint: Path | None,
        *,
        stage: str,
        code: str,
        detail: str = "",
    ) -> bool:
        """Report a KRKR static failure and prepare, but never start, realtime."""
        game_dir = game_path if game_path.is_dir() else game_path.parent
        meta_dir = game_dir / "_translation_meta"
        capture_path = meta_dir / "kirikiri_runtime_capture.jsonl"
        launcher: Path | None = None
        try:
            meta_dir.mkdir(parents=True, exist_ok=True)
            if not capture_path.exists():
                capture_path.write_text("", encoding="utf-8")
            launcher = create_kirikiri_native_launcher(
                game_path,
                engine=engine,
                checkpoint=checkpoint,
            )
        except Exception as exc:
            detail = f"{detail}; realtime launcher preparation error: {exc}".strip("; ")

        if launcher:
            if self.diagnostics:
                self.diagnostics.set("kirikiri_realtime_fallback", {
                    "available": True,
                    "launcher": str(launcher),
                    "capture": str(capture_path),
                    "started": False,
                })
                if self.manifest:
                    self.manifest.record_created(launcher, kind="runtime", runtime_required=True)
            info("KRKR 静态方案失败，已准备实时 Hook 启动器；等待用户确认")
            return self._fail_stage_code(
                stage,
                code,
                detail=detail,
                fallback="realtime",
                actions=("改用实时翻译",),
                tool_attempts=tuple(getattr(engine, "_static_external_tool_attempts", []) or []),
                rollback=True,
                next_actions=("点击“改用实时翻译”启动 KRKR 原生 Hook。",),
                fallback_launcher=str(launcher),
                capture=str(capture_path),
            )

        return self._fail_stage_code(
            "runtime",
            "runtime_prepare_failed",
            detail=detail or "KRKR 原生 Hook 启动器未生成",
            rollback=True,
            next_actions=("重新安装完整发布包，或使用仅提取检查工具组件。",),
        )

    def _rollback_game_changes(self, game_path: Path) -> bool:
        """Restore manifest backups and remove only new KRKR patch artifacts."""
        game_dir = game_path if game_path.is_dir() else game_path.parent
        restored = False
        if self.manifest:
            for record in self.manifest.data.get("modified_files", []):
                target = self.manifest._manifest_item_path(record)
                backup = Path(str(record.get("backup") or ""))
                if not backup.is_file():
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, target)
                    restored = True
                except OSError as exc:
                    warning(f"回滚游戏文件失败: {target} - {exc}")

        # KiriKiri creates these during repack outside the normal copy-back
        # directory. Preserve artifacts which existed before this run.
        before = getattr(self, "_artifact_snapshot", set()) or set()
        candidates = [
            game_dir / "patch.xp3",
            game_dir / "_translation_meta" / "kirikiri_patch.xp3",
            game_dir / "_translation_meta" / "kirikiri_patch",
            game_dir / "_translation_meta" / "kirikiri_patch_manifest.txt",
        ]
        for target in candidates:
            if not target.exists() or str(target.resolve()) in before:
                if not target.is_dir() or str(target.resolve()) not in before:
                    continue
                for child in sorted(target.rglob("*"), key=lambda path: len(path.parts), reverse=True):
                    if str(child.resolve()) in before:
                        continue
                    try:
                        if child.is_dir():
                            child.rmdir()
                        else:
                            child.unlink()
                    except OSError as exc:
                        warning(f"鍥炴粴 KRKR 琛ヤ竵瀛愭枃浠跺垹闄ゅけ璐? {child} - {exc}")
                continue
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                restored = True
            except OSError as exc:
                warning(f"回滚 KRKR 补丁产物失败: {target} - {exc}")
        if self.diagnostics:
            self.diagnostics.set("failure_rollback", {"attempted": True, "restored": restored})
        return restored

    def _run_repack_stage(self, game_path: Path, engine, items: list) -> bool:
        """Run repack as a failure-aware stage shared by both pipeline modes."""
        try:
            engine.repack(items, self.workspace.root)
            failed_files = list(getattr(engine, "_static_repack_failed_files", []) or [])
            if failed_files and getattr(engine, "name", "") == "kirikiri":
                raise RuntimeError(
                    "KRKR static repack left files unpatched: "
                    + ", ".join(str(path) for path in failed_files[:8])
                )
            return True
        except Exception as exc:
            self._rollback_game_changes(game_path)
            if getattr(engine, "name", "") == "kirikiri":
                return self._fail_kirikiri_static_stage(
                    game_path,
                    engine,
                    self._checkpoint_path,
                    stage="repack",
                    code="krkr_static_repack_failed",
                    detail=str(exc),
                )
            return self._fail_stage_code(
                "repack",
                "repack_failed",
                detail=str(exc),
                rollback=True,
                next_actions=("请确认游戏文件未被其他程序占用后重试。",),
            )

    def _kirikiri_verification_failed(self, verification: dict) -> bool:
        if not verification or verification.get("engine") != "kirikiri":
            return False
        if verification.get("invalid_archives"):
            return True
        if not verification.get("checked"):
            return True
        return int(verification.get("hits", 0) or 0) <= 0

    def _ensure_translator_ready(self, config, items: list, game_path: Path, engine=None) -> bool:
        """Fail before any API call when the selected cloud translator lacks a key."""
        translator_name = str(getattr(config, "active_translator", "") or "").strip().lower()
        translator = _get_translator(translator_name)
        if translator is None:
            if getattr(engine, "name", "") == "kirikiri":
                return self._fail_kirikiri_static_stage(
                    game_path, engine, self._checkpoint_path,
                    stage="translate", code="translation_no_result",
                    detail=f"unknown_or_unavailable_translator={translator_name}",
                )
            return self._fail_stage_code(
                "translate",
                "translation_no_result",
                detail=f"unknown_or_unavailable_translator={translator_name}",
                rollback=True,
                next_actions=("在设置中选择可用翻译器。",),
            )

        # A fully cached run can repack without a provider key. Load the cache
        # before rejecting the configuration, so reruns remain offline-safe.
        if game_path:
            load_translations_from_cache(game_path, items)
        if any(not _has_effective_translation(item) for item in items):
            local_translators = {"hy_mt2"}
            if translator_name not in local_translators:
                try:
                    from translators.factory import translator_api_key

                    api_key = translator_api_key(translator_name, config)
                except Exception:
                    api_key = ""
                if not api_key:
                    if getattr(engine, "name", "") == "kirikiri":
                        return self._fail_kirikiri_static_stage(
                            game_path, engine, self._checkpoint_path,
                            stage="translate", code="translation_key_missing",
                            detail=f"translator={translator_name}; pending={sum(1 for item in items if not _has_effective_translation(item))}",
                        )
                    return self._fail_stage_code(
                        "translate",
                        "translation_key_missing",
                        detail=f"translator={translator_name}; pending={sum(1 for item in items if not _has_effective_translation(item))}",
                        rollback=True,
                        next_actions=("在设置中填写当前翻译器的 API Key 后重试。",),
                    )
        return True

    def _reset_item_progress(self):
        self._item_progress_current = 0
        self._item_progress_total = 0
        self._item_progress_pct = 0.0

    def _update_progress(self, step: str, pct: float | None = None):
        raw_step = step
        stage_key = ""
        if pct is None:
            try:
                stage_info = stage(step)
                stage_key = stage_info.key
                step = stage_info.label
                pct = stage_info.progress
            except ValueError:
                pct = 0
        if stage_key == "translate" or raw_step in {"translate", "polish"}:
            self._reset_item_progress()
        if self.progress:
            try:
                self.progress(step, pct)
            except Exception:
                pass
        if self.diagnostics and stage_key:
            self.diagnostics.set("current_stage", {
                "key": stage_key,
                "label": step,
                "progress": pct,
            })
            self.diagnostics.mark_stage(stage_key, step, pct)

    def _update_item_progress(self, current: int, total: int):
        try:
            current_i = max(0, int(current))
            total_i = max(1, int(total))
        except Exception:
            return
        current_i = min(current_i, total_i)
        if self._item_progress_total and current_i < self._item_progress_current:
            return
        pct = 40 + (current_i / max(total_i, 1)) * 15
        if pct < self._item_progress_pct:
            pct = self._item_progress_pct
        self._item_progress_current = current_i
        self._item_progress_total = total_i
        self._item_progress_pct = pct
        self._meta("translate_progress", {"current": current_i, "total": total_i})
        if self.item_progress:
            try:
                self.item_progress(current_i, total_i)
            except Exception:
                pass
        elif self.progress:
            try:
                self.progress(f"翻译中 ({current_i}/{total_i})", pct)
            except Exception:
                pass
        return

    def _write_completion_notice(self, game_path: Path, engine=None, *, mode: str = "") -> None:
        notice = _write_translation_notice(game_path, engine, mode=mode)
        if notice and self.diagnostics:
            self.diagnostics.set("completion_notice", str(notice))
        if notice:
            info(f"已写入汉化说明: {notice.name}")

    def _required_translation_coverage_ratio(self) -> float:
        return pipeline_translate_stage.required_translation_coverage_ratio(self)

    def _has_required_translation_coverage(self, items: list, min_ratio: float | None = None) -> bool:
        return pipeline_translate_stage.has_required_translation_coverage(self, items, min_ratio)

    def _fail_insufficient_translation_coverage(self, items: list) -> None:
        engine = getattr(self, "_current_engine", None)
        if getattr(engine, "name", "") == "kirikiri":
            translated = sum(1 for item in items if _has_effective_translation(item))
            total = len(items)
            return self._fail_kirikiri_static_stage(
                getattr(self, "_current_game_path", Path()),
                engine,
                self._checkpoint_path,
                stage="translate",
                code="translation_coverage_low",
                detail=f"translated={translated}; total={total}; required={self._required_translation_coverage_ratio():.3f}",
            )
        return pipeline_translate_stage.fail_insufficient_translation_coverage(self, items)

    def _should_skip_cached_tail_translation(self, items: list, cached_count: int) -> tuple[bool, int, float]:
        return pipeline_translate_stage.should_skip_cached_tail_translation(self, items, cached_count)

    def _skip_cached_tail_translation(self, items: list, cached_count: int) -> bool:
        return pipeline_translate_stage.skip_cached_tail_translation(self, items, cached_count)

    def _reset_api_cache_stats(self, translator_name: str):
        return pipeline_translate_stage.reset_api_cache_stats(self, translator_name)

    def _record_api_cache_stats(self, game_path: Path, total_texts: int):
        return pipeline_translate_stage.record_api_cache_stats(self, game_path, total_texts)

    def _checkpoint_resume_candidate(self, game_path: Path, file_filter: list[str] | None = None) -> Path | None:
        return pipeline_checkpoint_stage.checkpoint_resume_candidate(self, game_path, file_filter)

    def _load_items_from_checkpoint_json(self, checkpoint: Path) -> tuple[list, str, str, dict]:
        return pipeline_checkpoint_stage.load_items_from_checkpoint_json(self, checkpoint)

    def _reuse_checkpoint_workspace_if_available(self, checkpoint: Path) -> bool:
        return pipeline_checkpoint_stage.reuse_checkpoint_workspace_if_available(self, checkpoint)

    def _workspace_has_repack_sources(self, items: list) -> bool:
        return pipeline_checkpoint_stage.workspace_has_repack_sources(self, items)

    def _prepare_kirikiri_repack_sources_from_meta_dump(self, game_path: Path, items: list) -> int:
        return pipeline_kirikiri_dump.prepare_kirikiri_repack_sources_from_meta_dump(self, game_path, items)

    async def _run_checkpoint_resume_fast_path(
        self,
        path: Path,
        engine,
        injector: str | None,
        checkpoint: Path,
        launch: bool,
    ) -> bool:
        return await pipeline_checkpoint_stage.run_checkpoint_resume_fast_path(
            self, path, engine, injector, checkpoint, launch)

    def _merge_kirikiri_runtime_capture_raw_items(self, game_path: Path, raw_items: list[dict]) -> list[dict]:
        return pipeline_kirikiri_dump.merge_kirikiri_runtime_capture_raw_items(self, game_path, raw_items)

    def _begin_run_context(self, input_path: str, path: Path, mode: dict):
        return pipeline_context.begin_run_context(self, input_path, path, mode)

    def _run_archive_stage(self, path: Path) -> Path | None:
        return pipeline_stage_runtime.run_archive_stage(self, path)

    def _run_detection_stage(self, path: Path, injector: str | None):
        return pipeline_stage_runtime.run_detection_stage(self, path, injector)

    def _run_extract_stage(self, path: Path, engine, file_filter: list[str] | None = None):
        return pipeline_stage_runtime.run_extract_stage(self, path, engine, file_filter)

    def _try_kirikiri_pre_extract_auto_dump(self, path: Path, engine) -> bool:
        return pipeline_kirikiri_dump.try_kirikiri_pre_extract_auto_dump(self, path, engine)

    def _try_kirikiri_auto_dump_stage(
        self,
        path: Path,
        engine,
        items: list,
        file_filter: list[str] | None = None,
    ):
        return pipeline_kirikiri_dump.try_kirikiri_auto_dump_stage(self, path, engine, items, file_filter)

    def _detect_language_stage(self, items: list, target_lang: str) -> tuple[str, str]:
        return pipeline_stage_runtime.detect_language_stage(self, items, target_lang)

    def _apply_coverage_limit(self, items: list, coverage_percent: int, *, note: str = "") -> list:
        return pipeline_stage_runtime.apply_coverage_limit(self, items, coverage_percent, note=note)

    def _apply_configured_translation_scope(
        self,
        engine,
        items: list,
        coverage_percent: int,
        *,
        note: str = "",
    ) -> list:
        """Apply user-configured task scope before translating.

        KiriKiri runtime hook can learn visible strings while the player runs the
        game. When both static candidates and runtime captures exist, the user
        chooses whether to translate only captured display text, follow the
        normal coverage slider, or force the full checkpoint.
        """
        selected = list(items)
        effective_coverage = coverage_percent
        mode = "coverage"

        if getattr(engine, "name", "") == "kirikiri" and _has_runtime_capture_items(selected):
            raw_mode = str(
                getattr(get_config(), "kirikiri_runtime_completion_mode", "captured_only")
                or "captured_only"
            ).strip().lower()
            aliases = {
                "captured": "captured_only",
                "capture": "captured_only",
                "runtime": "captured_only",
                "full": "all",
                "100": "all",
            }
            mode = aliases.get(raw_mode, raw_mode)
            if mode not in {"captured_only", "coverage", "all"}:
                mode = "captured_only"

            runtime_items = [
                item for item in selected
                if bool(getattr(item, "meta", {}).get("runtime_capture"))
            ]
            static_items = [
                item for item in selected
                if not bool(getattr(item, "meta", {}).get("runtime_capture"))
            ]
            has_static_items = bool(static_items)

            # A runtime capture is a supplement when static extraction produced
            # usable items.  ``captured_only`` is only meaningful for a pure
            # runtime result; applying it to a mixed result silently discarded
            # the complete static catalog (for example 4,852 -> 28 items).
            if mode == "captured_only" and runtime_items and not has_static_items:
                selected = runtime_items
                effective_coverage = 100
                info(f"KiriKiri 补翻模式: 只翻译运行时捕获文本 {len(selected)}/{len(items)} 条")
            elif mode == "captured_only" and runtime_items and has_static_items:
                effective_coverage = 100 if int(coverage_percent or 100) >= 100 else coverage_percent
                info(
                    f"KiriKiri 静态文本已保留: 静态 {len(static_items)} 条 + "
                    f"运行时补充 {len(runtime_items)} 条，共 {len(selected)} 条"
                )
            elif mode == "all":
                effective_coverage = 100
                info(f"KiriKiri 补翻模式: 完整检查点补齐 {len(selected)} 条")
            else:
                info(
                    f"KiriKiri 补翻模式: 按覆盖率 {max(1, min(100, int(coverage_percent or 100)))}% "
                    f"处理捕获+检查点文本 {len(selected)} 条"
                )
            if self.diagnostics:
                self.diagnostics.set("kirikiri_runtime_completion_mode", mode)
                self.diagnostics.set("kirikiri_static_items_preserved", has_static_items)
                self.diagnostics.set("kirikiri_static_item_count", len(static_items))
                self.diagnostics.set("kirikiri_runtime_capture_count", len(runtime_items))
                self.diagnostics.set("kirikiri_translation_scope_count", len(selected))

        return self._apply_coverage_limit(selected, effective_coverage, note=note)

    def run(self, input_path: str, launch: bool = True, injector: str | None = None, file_filter: list[str] | None = None):
        """同步入口，内部驱动 async 流水线。"""
        try:
            with game_pipeline_lock(input_path):
                return asyncio.run(self.run_async(input_path, launch, injector, file_filter))
        except Exception as e:
            import traceback
            error(f"管线异常退出: {e}\n{traceback.format_exc()}")
            try:
                self.progress("错误", 0)
            except Exception:
                pass
            return False

    def run_with_checkpoint(self, input_path: str, launch: bool = True,
                             injector: str | None = None,
                             file_filter: list[str] | None = None,
                             resume_json: str = "",
                             extract_only: bool = False,
                             patch_only: bool = False):
        """JSON 检查点管线：提取→翻译→回填 三阶段，支持断点续传。"""
        try:
            with game_pipeline_lock(input_path):
                return asyncio.run(self._run_checkpoint_async(
                    input_path, launch, injector, file_filter, resume_json, extract_only, patch_only))
        except Exception as e:
            import traceback
            error(f"管线异常退出: {e}\n{traceback.format_exc()}")
            try:
                self.progress("错误", 0)
            except Exception:
                pass
            return False

    def polish_checkpoint(self, input_path: str, checkpoint_json: str = "", budget_cny: float = 1.0,
                          launch: bool = False, injector: str | None = None):
        """Polish existing Chinese translations in a checkpoint, then repack from it."""
        try:
            with game_pipeline_lock(input_path):
                return asyncio.run(self._polish_checkpoint_async(
                    input_path, checkpoint_json, budget_cny, launch, injector
                ))
        except Exception as e:
            import traceback
            error(f"polish pipeline failed: {e}\n{traceback.format_exc()}")
            try:
                self.progress("error", 0)
            except Exception:
                pass
            return False

    async def _run_checkpoint_async(self, input_path: str, launch: bool,
                                     injector: str | None, file_filter: list[str] | None,
                                     resume_json: str, extract_only: bool, patch_only: bool):
        from pathlib import Path as _Path

        resolved_input = resolve_game_path(input_path)
        path = _Path(resolved_input)
        if not path.exists():
            error(f"路径不存在: {input_path} -> {resolved_input}")
            return False

        config = self._begin_run_context(input_path, path, {
            "checkpoint": True,
            "extract_only": extract_only,
            "patch_only": patch_only,
            "injector": injector,
            "launch": launch,
        })
        checkpoint = self.workspace.root / "translation_checkpoint.json"

        try:
            # ---- 仅回填模式：从 JSON 直接跳到 repack ----
            if patch_only:
                resume = _Path(resume_json) if resume_json else checkpoint
                return await self._do_patch_only(path, resume, launch, injector)

            # Step 1: 解压
            path = self._run_archive_stage(path)
            if path is None:
                return False

            # Step 2: 检测引擎
            engine, injector = self._run_detection_stage(path, injector)
            self._current_engine = engine
            self._current_game_path = path
            if engine is None:
                return self._fail_stage_code(
                    "detect",
                    "engine_not_found",
                    detail=f"path={path}",
                    next_actions=("确认选择的是游戏目录或主 exe，然后重新检测。",),
                )

            fast_checkpoint = None
            if not extract_only and not resume_json:
                fast_checkpoint = self._checkpoint_resume_candidate(path, file_filter)
            if fast_checkpoint:
                return await self._run_checkpoint_resume_fast_path(
                    path,
                    engine,
                    injector,
                    fast_checkpoint,
                    launch,
                )

            if self._try_kirikiri_pre_extract_auto_dump(path, engine):
                if getattr(engine, "_kirikiri_auto_dump_incomplete", False):
                    if getattr(engine, "name", "") == "kirikiri":
                        return self._fail_kirikiri_static_stage(
                            path,
                            engine,
                            checkpoint,
                            stage="script_extract",
                            code="krkr_static_extract_failed",
                            detail="运行时 dump 目标不完整，静态脚本候选未通过验证",
                        )
                    return self._fail_stage_code(
                        "script_extract",
                        "extract_failed",
                        detail="运行时 dump 目标不完整",
                    )

            # Step 3: 解包文本
            engine, items, extracted_count = self._run_extract_stage(path, engine, file_filter)
            engine, items, extracted_count = self._try_kirikiri_auto_dump_stage(path, engine, items, file_filter)
            if getattr(engine, "_kirikiri_auto_dump_incomplete", False):
                return self._fail_kirikiri_static_stage(
                    path,
                    engine,
                    checkpoint,
                    stage="script_extract",
                    code="krkr_static_extract_failed",
                    detail="运行时 dump 目标不完整，静态脚本候选未通过验证",
                ) if getattr(engine, "name", "") == "kirikiri" else self._fail_stage_code(
                    "script_extract",
                    "extract_failed",
                    detail="运行时 dump 目标不完整",
                )
            if not items:
                if extracted_count:
                    warning("过滤后无可翻译文本")
                    if getattr(engine, "name", "") == "kirikiri":
                        return self._fail_kirikiri_static_stage(
                            path,
                            engine,
                            checkpoint,
                            stage="script_extract",
                            code="krkr_no_readable_script",
                            detail=f"提取结果 {extracted_count} 条，但过滤后没有可解析的可见文本",
                        )
                    return self._fail_stage_code(
                        "script_extract",
                        "no_readable_text",
                        detail=f"提取结果 {extracted_count} 条，但过滤后没有可翻译文本",
                    )
                warning("未提取到可翻译文本")
                self._record_no_items(engine, path, injector)
                # xunity 模式：即使提取不到文本，也要部署运行时环境
                # XUAT 会在运行时 Hook 文本组件，不需要预提取
                if injector == "xunity":
                    info("xunity 模式：跳过文本提取，直接部署运行时翻译环境")
                    self._setup_engine_repack(engine, path)
                    engine._source_lang = _detect_game_language(path)  # 尝试从游戏文件检测源语言
                    engine.repack([], self.workspace.root)
                    if launch:
                        launch_with_injector(path, injector, engine=engine, checkpoint=checkpoint)
                    else:
                        self._stop_translation_proxy(engine)
                    self._write_completion_notice(path, engine, mode="XUnity 运行时翻译环境")
                    self.diagnostics.finish(True)
                    return True
                if getattr(engine, "name", "") == "kirikiri":
                    return self._fail_kirikiri_static_stage(
                        path,
                        engine,
                        checkpoint,
                        stage="script_extract",
                        code="krkr_no_readable_script",
                        detail="内置解析器、GARbro 和 msg-tool 都没有产出可解析脚本",
                    )
                return self._fail_stage_code(
                    "script_extract",
                    "no_readable_text",
                    detail=f"engine={getattr(engine, 'name', '')}",
                )
            if getattr(engine, "name", "") == "kirikiri" and _has_runtime_capture_items(items):
                if self.diagnostics:
                    self.diagnostics.set("injector", injector)
                    self.diagnostics.set("runtime_overlay_only", True)
                info("KiriKiri 使用运行时捕获文本：离线翻译后将通过原生 KAGParser hook 显示")

            # Preflight must preserve the complete extraction result. In
            # particular, KRKR's captured_only runtime mode is a translation
            # scope for a real run, not a reason to discard static candidates
            # from the checkpoint used by the GUI estimate.
            if not extract_only:
                items = self._apply_configured_translation_scope(
                    engine,
                    items,
                    config.translation_coverage,
                )
            elif getattr(engine, "name", "") == "kirikiri" and _has_runtime_capture_items(items):
                self.diagnostics.set("runtime_capture_scope_deferred", {
                    "mode": "captured_only",
                    "captured_count": sum(
                        1 for item in items
                        if bool(getattr(item, "meta", {}).get("runtime_capture"))
                    ),
                    "extracted_count": len(items),
                })

            # 源语言检测
            source_lang, target_lang = self._detect_language_stage(items, config.target_lang)

            # Step 4: 加载已有检查点（断点续传）
            self._update_progress("checkpoint_load")
            game_checkpoint = _game_meta_path(path, "translation_checkpoint.json")
            if not extract_only and not checkpoint.exists() and game_checkpoint.exists():
                # 验证 game_checkpoint 不是测试数据残留
                try:
                    ckpt_data = json.loads(game_checkpoint.read_text(encoding="utf-8-sig"))
                    if ckpt_data.get("source") == "mock":
                        warning(f"检测到测试数据残留 {game_checkpoint}，已忽略")
                    elif not ckpt_data.get("items"):
                        warning(f"检查点无有效条目 {game_checkpoint}，已忽略")
                    else:
                        checkpoint = game_checkpoint
                except Exception as e:
                    warning(f"读取 {game_checkpoint} 失败: {e}")
            if not extract_only and checkpoint.exists():
                self._load_checkpoint_into(items, checkpoint)

            # 导出/更新 JSON 检查点
            self._update_progress("checkpoint_export")
            self._checkpoint_path = checkpoint
            self._save_checkpoint_json(items, path, source_lang, target_lang, checkpoint)
            info(f"检查点已保存: {checkpoint.name}")
            self.diagnostics.set("checkpoint", str(checkpoint))
            self.diagnostics.set("game_checkpoint", str(game_checkpoint))

            # 仅提取模式：到此为止
            if extract_only:
                self._update_progress("complete")
                info(f"仅提取模式: 文本已导出到 {checkpoint}")
                info(f"  下一步: python main.py {input_path} --from-json {checkpoint}")
                self.diagnostics.finish(True)
                return True

            # Step 5: AI 翻译（增量保存到 JSON）
            if not self._ensure_translator_ready(config, items, path, engine):
                return False
            self._update_progress("translate")
            self._reset_api_cache_stats(config.active_translator)
            items = await self._translate_with_checkpoint(
                items, checkpoint, source_lang, target_lang, path
            )
            self._record_api_cache_stats(path, len(items))

            # 安全校验
            self._update_progress("validate")
            items = pipeline_runtime_stage.sanitize_engine_translations(engine, items, target_lang)
            items = self._validate_all(items)
            self._sync_checkpoint_json(checkpoint, items)
            items = await self._retry_invalid_translations(
                items, checkpoint, source_lang, target_lang, path
            )
            self._record_api_cache_stats(path, len(items))
            translated_count = sum(1 for i in items if _has_effective_translation(i))
            if self._blocked_count > 0:
                warning(f"安全拦截: {self._blocked_count} 条翻译被回退原文")
            info(f"翻译完成: {translated_count}/{len(items)} 条")
            self.diagnostics.set("translation_stats", {
                "translated": translated_count,
                "total": len(items),
                "blocked": self._blocked_count,
            })

            if not self._has_required_translation_coverage(items):
                self._fail_insufficient_translation_coverage(items)
                return False

            if translated_count == 0:
                warning("没有成功翻译任何文本！请检查 API Key 是否已配置。")
                if getattr(engine, "name", "") == "kirikiri":
                    return self._fail_kirikiri_static_stage(
                        path,
                        engine,
                        checkpoint,
                        stage="translate",
                        code="translation_no_result",
                        detail="翻译阶段没有得到有效译文",
                    )
                return self._fail_stage_code(
                    "translate",
                    "translation_no_result",
                    detail="没有成功翻译任何文本",
                    rollback=True,
                    next_actions=("检查翻译器、API Key 和网络连接后重试。",),
                )

            # Step 6: 回填前备份
            self._update_progress("backup")
            runtime_resource_overlay = _should_use_runtime_resource_overlay(injector, engine, path, items)
            runtime_overlay_only = _should_use_runtime_overlay(injector, engine, path, items) and not runtime_resource_overlay
            if runtime_overlay_only:
                info(_runtime_overlay_note(injector, engine))
                self.diagnostics.set("runtime_overlay_only", True)
                self._disable_kirikiri_static_patch_artifacts_for_runtime(path, engine)
            elif runtime_resource_overlay:
                info(_runtime_overlay_note(injector, engine))
                self.diagnostics.set("runtime_resource_overlay", True)
                self._disable_kirikiri_static_patch_artifacts_for_runtime(path, engine)
                _mark_runtime_resource_overlay(engine, True)
            elif can_repack(engine):
                self._backup_game_files(path, items, engine)
            else:
                info("当前引擎不支持安全自动回填，跳过备份与写回准备")
            self._artifact_snapshot = self.manifest.snapshot_tool_artifacts() if self.manifest and not runtime_overlay_only else set()

            # Step 7: 回填
            self._update_progress("repack")
            if runtime_overlay_only:
                info("运行时 hook 模式无需修改游戏文本资源或主程序")
            else:
                self._verify_repack_integrity(items, engine)
                self._setup_engine_repack(engine, path)
                engine._source_lang = source_lang  # 传递源语言给 repack 阶段
                engine._target_lang = target_lang
                if not can_repack(engine):
                    self.diagnostics.warn("当前引擎不支持自动回填", engine=getattr(engine, "name", ""))
                    self.diagnostics.suggest("使用“仅提取”导出 JSON，配合专用工具手动回填资源。")
                else:
                    if not self._run_repack_stage(path, engine, items):
                        return False

            # Step 7.5: CJK 字体
            self._update_progress("fonts")
            if not runtime_overlay_only:
                _replace_fonts_if_needed(engine, path, self.workspace.root)

            # Step 8: 复制回游戏
            self._update_progress("copy_back")
            if runtime_overlay_only:
                self.diagnostics.set("repack_verification", {
                    "checked": True,
                    "hits": 0,
                    "files_checked": 0,
                    "note": "运行时显示层 hook 模式，未写回游戏文本文件",
                })
            elif runtime_resource_overlay:
                self.diagnostics.set("repack_verification", self._verify_repack_outputs(path, items, engine))
                if self.manifest:
                    self.manifest.record_new_tool_artifacts(self._artifact_snapshot, engine)
            else:
                _copy_back_safe(self.workspace, path, items, engine)
                self._record_modified_outputs(path, items, engine)
                if self.manifest:
                    self.manifest.record_new_tool_artifacts(self._artifact_snapshot, engine)
                verification = self._verify_repack_outputs(path, items, engine)
                self.diagnostics.set("repack_verification", verification)
                if self._kirikiri_verification_failed(verification):
                    self._rollback_game_changes(path)
                    return self._fail_kirikiri_static_stage(
                        path,
                        engine,
                        checkpoint,
                        stage="repack",
                        code="krkr_static_repack_failed",
                        detail=(
                            f"回填验证 checked={verification.get('checked')} "
                            f"hits={verification.get('hits', 0)} "
                            f"invalid_archives={verification.get('invalid_archives', [])}"
                        ),
                    )
            self._prepare_runtime_dependencies(path, engine)
            self._create_runtime_launchers(path, engine, checkpoint)

            if getattr(engine, "name", "") == "unity" and not injector:
                info("Unity 引擎物理替换已尝试。如果游戏运行异常，请使用 XUnity 注入器模式重试")

            # Step 9: 启动游戏
            self._update_progress("launch")
            if launch:
                _launch_finished_game(path, engine, injector, checkpoint)
            else:
                self._stop_translation_proxy(engine)

            info("全部完成！")
            self._write_completion_notice(path, engine, mode="离线翻译/回填")
            self.diagnostics.finish(True)

            if not config.keep_workspace:
                self.workspace.cleanup()

            return True

        except Exception as e:
            if self.diagnostics:
                self.diagnostics.record_exception("流水线异常", e)
                self.diagnostics.finish(False)
            error(f"流水线异常: {e}")
            raise

    async def run_async(self, input_path: str, launch: bool = True, injector: str | None = None, file_filter: list[str] | None = None):
        resolved_input = resolve_game_path(input_path)
        path = Path(resolved_input)
        if not path.exists():
            error(f"路径不存在: {input_path} -> {resolved_input}")
            return False

        config = self._begin_run_context(input_path, path, {
            "checkpoint": False,
            "injector": injector,
            "launch": launch,
        })

        try:
            # Step 1: 解压（如果是压缩包）
            path = self._run_archive_stage(path)
            if path is None:
                return False

            # Step 2: 检测引擎
            engine, injector = self._run_detection_stage(path, injector)
            self._current_engine = engine
            self._current_game_path = path
            if engine is None:
                return self._fail_stage_code(
                    "detect",
                    "engine_not_found",
                    detail=f"path={path}",
                    next_actions=("确认选择的是游戏目录或主 exe，然后重新检测。",),
                )

            if self._try_kirikiri_pre_extract_auto_dump(path, engine):
                if getattr(engine, "_kirikiri_auto_dump_incomplete", False):
                    if getattr(engine, "name", "") == "kirikiri":
                        return self._fail_kirikiri_static_stage(
                            path,
                            engine,
                            self._checkpoint_path,
                            stage="script_extract",
                            code="krkr_static_extract_failed",
                            detail="运行时 dump 目标不完整，静态脚本候选未通过验证",
                        )
                    return self._fail_stage_code(
                        "script_extract",
                        "extract_failed",
                        detail="运行时 dump 目标不完整",
                    )

            # Step 3: 解包文本
            engine, items, extracted_count = self._run_extract_stage(path, engine, file_filter)
            engine, items, extracted_count = self._try_kirikiri_auto_dump_stage(path, engine, items, file_filter)
            if getattr(engine, "_kirikiri_auto_dump_incomplete", False):
                return self._fail_kirikiri_static_stage(
                    path,
                    engine,
                    self._checkpoint_path,
                    stage="script_extract",
                    code="krkr_static_extract_failed",
                    detail="运行时 dump 目标不完整，静态脚本候选未通过验证",
                ) if getattr(engine, "name", "") == "kirikiri" else self._fail_stage_code(
                    "script_extract",
                    "extract_failed",
                    detail="运行时 dump 目标不完整",
                )
            if not items:
                if extracted_count:
                    warning("过滤后无可翻译文本")
                    if getattr(engine, "name", "") == "kirikiri":
                        return self._fail_kirikiri_static_stage(
                            path,
                            engine,
                            self._checkpoint_path,
                            stage="script_extract",
                            code="krkr_no_readable_script",
                            detail=f"提取结果 {extracted_count} 条，但过滤后没有可解析的可见文本",
                        )
                    return self._fail_stage_code(
                        "script_extract",
                        "no_readable_text",
                        detail=f"提取结果 {extracted_count} 条，但过滤后没有可翻译文本",
                    )
                warning("未提取到可翻译文本")
                self._record_no_items(engine, path, injector)
                if injector == "xunity":
                    info("xunity 模式：跳过文本提取，直接部署运行时翻译环境")
                    self._setup_engine_repack(engine, path)
                    engine._source_lang = _detect_game_language(path)  # 尝试从游戏文件检测源语言
                    engine.repack([], self.workspace.root)
                    if launch:
                        launch_with_injector(path, injector, engine=engine, checkpoint=self._checkpoint_path)
                    else:
                        self._stop_translation_proxy(engine)
                    self._write_completion_notice(path, engine, mode="XUnity 运行时翻译环境")
                    self.diagnostics.finish(True)
                    return True
                if getattr(engine, "name", "") == "kirikiri":
                    return self._fail_kirikiri_static_stage(
                        path,
                        engine,
                        self._checkpoint_path,
                        stage="script_extract",
                        code="krkr_no_readable_script",
                        detail="内置解析器、GARbro 和 msg-tool 都没有产出可解析脚本",
                    )
                return self._fail_stage_code(
                    "script_extract",
                    "no_readable_text",
                    detail=f"engine={getattr(engine, 'name', '')}",
                )
            if getattr(engine, "name", "") == "kirikiri" and _has_runtime_capture_items(items):
                if self.diagnostics:
                    self.diagnostics.set("injector", injector)
                    self.diagnostics.set("runtime_overlay_only", True)
                info("KiriKiri 使用运行时捕获文本：离线翻译后将通过原生 KAGParser hook 显示")

            # Step 4: 检测源语言
            source_lang, target_lang = self._detect_language_stage(items, config.target_lang)

            # ---- xunity 注入模式：跳过离线批量翻译，直接部署运行时环境 ----
            # XUAT 在游戏运行时实时翻译，管线只需部署 BepInEx + 插件 + 字体 + 配置。
            # 离线翻译所有提取文本是浪费 token（很多文本游戏中不会出现）。
            if injector == "xunity":
                info("xunity 模式：跳过离线批量翻译，直接部署运行时翻译环境")
                self._update_progress("部署翻译环境", 40)
                self._setup_engine_repack(engine, path)
                engine._source_lang = source_lang
                engine._target_lang = target_lang
                self._update_progress("repack")
                engine.repack(items, self.workspace.root)
                self._update_progress("fonts")
                _replace_fonts_if_needed(engine, path, self.workspace.root)
                self._update_progress("copy_back")
                self._update_progress("launch")
                if launch:
                    launch_with_injector(path, injector, engine=engine, checkpoint=self._checkpoint_path)
                else:
                    self._stop_translation_proxy(engine)
                info("xunity 运行时翻译环境部署完成")
                self._write_completion_notice(path, engine, mode="XUnity 运行时翻译环境")
                self.diagnostics.finish(True)
                return True

            # ---- 翻译覆盖百分比/补翻范围过滤（按游戏进度顺序取前 N%） ----
            items = self._apply_configured_translation_scope(
                engine,
                items,
                config.translation_coverage,
                note="按游戏进度顺序",
            )

            # Step 4.5: 从 SQLite 缓存加载已有翻译（增量更新）
            self._update_progress("缓存加载", 38)
            cached_count = load_translations_from_cache(path, items)
            if cached_count > 0:
                remaining = sum(1 for it in items if not _has_effective_translation(it))
                info(f"缓存命中 {cached_count} 条，剩余待翻译: {remaining}")
            self.diagnostics.step("cache_load", loaded=cached_count, total=len(items))
            skip_ai_tail = self._skip_cached_tail_translation(items, cached_count)

            if not self._ensure_translator_ready(config, items, path, engine):
                return False

            # Step 5: AI 翻译
            self._update_progress("translate")
            translator = _get_translator(config.active_translator)
            if skip_ai_tail:
                info("AI 翻译阶段已跳过少量尾巴文本")
            elif translator:
                self._reset_api_cache_stats(config.active_translator)
                items = await translate_batch_with_callbacks(
                    translator,
                    items, source_lang, target_lang,
                    on_progress=self._update_item_progress,
                    on_speed=lambda data: self._meta("local_speed", data),
                )
                self._record_api_cache_stats(path, len(items))
                # Step 5.5: 保存到 SQLite 缓存
                save_translations_to_cache(path, items)
            else:
                warning("未配置翻译器，跳过翻译")

            # ---- 防线 1 & 2: 翻译后强制安全校验 ----
            self._update_progress("validate")
            items = pipeline_runtime_stage.sanitize_engine_translations(engine, items, target_lang)
            items = self._validate_all(items)
            if self._checkpoint_path:
                self._sync_checkpoint_json(self._checkpoint_path, items)
                items = await self._retry_invalid_translations(
                    items, self._checkpoint_path, source_lang, target_lang, path
                )
                self._record_api_cache_stats(path, len(items))
            translated_count = sum(1 for i in items if _has_effective_translation(i))
            if self._blocked_count > 0:
                warning(f"安全拦截: {self._blocked_count} 条翻译被回退原文（占位符被破坏）")
            info(f"翻译完成: {translated_count}/{len(items)} 条（{self._blocked_count} 条被拦截）")
            self.diagnostics.set("translation_stats", {
                "translated": translated_count,
                "total": len(items),
                "blocked": self._blocked_count,
            })

            if not self._has_required_translation_coverage(items):
                self._fail_insufficient_translation_coverage(items)
                return False

            # 防线 6: 翻译量为 0 且非运行时注入模式 → 阻断，避免无翻译直接启动游戏
            if translated_count == 0:
                warning("没有成功翻译任何文本！请检查 API Key 是否已配置。")
                if getattr(engine, "name", "") == "kirikiri":
                    return self._fail_kirikiri_static_stage(
                        path,
                        engine,
                        self._checkpoint_path,
                        stage="translate",
                        code="translation_no_result",
                        detail="翻译阶段没有得到有效译文",
                    )
                return self._fail_stage_code(
                    "translate",
                    "translation_no_result",
                    detail="没有成功翻译任何文本",
                    rollback=True,
                    next_actions=("检查翻译器、API Key 和网络连接后重试。",),
                )

            if injector == "frida":
                self._checkpoint_path = self.workspace.root / "translation_checkpoint.json"
                self._save_checkpoint_json(items, path, source_lang, target_lang, self._checkpoint_path)
                self.diagnostics.set("checkpoint", str(self._checkpoint_path))

            # Step 6: 回填前备份
            self._update_progress("backup")
            runtime_resource_overlay = _should_use_runtime_resource_overlay(injector, engine, path, items)
            runtime_overlay_only = _should_use_runtime_overlay(injector, engine, path, items) and not runtime_resource_overlay
            if runtime_overlay_only:
                info(_runtime_overlay_note(injector, engine))
                self.diagnostics.set("runtime_overlay_only", True)
                self._disable_kirikiri_static_patch_artifacts_for_runtime(path, engine)
            elif runtime_resource_overlay:
                info(_runtime_overlay_note(injector, engine))
                self.diagnostics.set("runtime_resource_overlay", True)
                self._disable_kirikiri_static_patch_artifacts_for_runtime(path, engine)
                _mark_runtime_resource_overlay(engine, True)
            elif can_repack(engine):
                self._backup_game_files(path, items, engine)
            else:
                info("当前引擎不支持安全自动回填，跳过备份与写回准备")

            # Step 7: 回填
            self._update_progress("repack")
            if runtime_overlay_only:
                info("运行时 hook 模式无需修改游戏文本资源或主程序")
            else:
                self._verify_repack_integrity(items, engine)
                self._setup_engine_repack(engine, path)
                engine._source_lang = source_lang  # 传递源语言给 repack 阶段
                engine._target_lang = target_lang
                if not can_repack(engine):
                    self.diagnostics.warn("当前引擎不支持自动回填", engine=getattr(engine, "name", ""))
                    self.diagnostics.suggest("使用“仅提取”导出 JSON，配合专用工具手动回填资源。")
                else:
                    if not self._run_repack_stage(path, engine, items):
                        return False

            # Step 7.5: CJK 字体替换（Godot 游戏）
            self._update_progress("fonts")
            if not runtime_overlay_only:
                _replace_fonts_if_needed(engine, path, self.workspace.root)

            # Step 8: 复制回游戏源目录
            self._update_progress("copy_back")
            if runtime_overlay_only:
                self.diagnostics.set("repack_verification", {
                    "checked": True,
                    "hits": 0,
                    "files_checked": 0,
                    "note": "运行时显示层 hook 模式，未写回游戏文本文件",
                })
            elif runtime_resource_overlay:
                self.diagnostics.set("repack_verification", self._verify_repack_outputs(path, items, engine))
                if self.manifest:
                    self.manifest.record_new_tool_artifacts(self._artifact_snapshot, engine)
            else:
                _copy_back_safe(self.workspace, path, items, engine)
                self._record_modified_outputs(path, items, engine)
                if self.manifest:
                    self.manifest.record_new_tool_artifacts(self._artifact_snapshot, engine)
                verification = self._verify_repack_outputs(path, items, engine)
                self.diagnostics.set("repack_verification", verification)
                if self._kirikiri_verification_failed(verification):
                    self._rollback_game_changes(path)
                    return self._fail_kirikiri_static_stage(
                        path,
                        engine,
                        self._checkpoint_path,
                        stage="repack",
                        code="krkr_static_repack_failed",
                        detail=(
                            f"回填验证 checked={verification.get('checked')} "
                            f"hits={verification.get('hits', 0)} "
                            f"invalid_archives={verification.get('invalid_archives', [])}"
                        ),
                    )
            self._prepare_runtime_dependencies(path, engine)
            self._create_runtime_launchers(path, engine, self._checkpoint_path)

            # ---- 防线 5: 物理替换失败时建议 XUnity ----
            if getattr(engine, "name", "") == "unity" and not injector:
                info("Unity 引擎物理替换已尝试。如果游戏运行异常，请使用 XUnity 注入器模式重试")
                info("  命令行: python main.py <路径> --injector xunity")

            # Step 9: 启动游戏
            self._update_progress("launch")
            if launch:
                _launch_finished_game(path, engine, injector, self._checkpoint_path)
            else:
                self._stop_translation_proxy(engine)

            info("全部完成！")
            self._write_completion_notice(path, engine, mode="离线翻译/回填")
            self.diagnostics.finish(True)

            if not config.keep_workspace:
                self.workspace.cleanup()

            return True

        except Exception as e:
            if self.diagnostics:
                self.diagnostics.record_exception("流水线异常", e)
                self.diagnostics.finish(False)
            error(f"流水线异常: {e}")
            raise

    # ------------------------------------------------------------------
    # JSON 检查点辅助方法
    # ------------------------------------------------------------------

    async def _polish_checkpoint_async(self, input_path: str, checkpoint_json: str,
                                       budget_cny: float, launch: bool,
                                       injector: str | None) -> bool:
        import json as _json
        from pathlib import Path as _Path

        from engines.base import TextItem
        from translators.polisher import DeepSeekPolisher

        resolved_input = resolve_game_path(input_path)
        path = _Path(resolved_input)
        if not path.exists():
            error(f"path does not exist: {input_path} -> {resolved_input}")
            return False

        self._begin_run_context(input_path, path, {
            "checkpoint": True,
            "polish": True,
            "injector": injector,
            "launch": launch,
        })

        checkpoint = _Path(checkpoint_json) if checkpoint_json else _game_meta_path(path, "translation_checkpoint.json")
        if not checkpoint.exists() and self.workspace:
            fallback = self.workspace.root / "translation_checkpoint.json"
            if fallback.exists():
                checkpoint = fallback
        if not checkpoint.exists():
            error(f"translation checkpoint missing: {checkpoint}")
            return self._fail_stage_code(
                "patch",
                "checkpoint_missing",
                detail=f"json_path={checkpoint}",
                rollback=True,
                next_actions=("重新完成提取，或选择包含原文和译文的有效检查点。",),
            )

        data = _json.loads(checkpoint.read_text(encoding="utf-8-sig"))
        items: list[TextItem] = []
        raw_by_key: dict[tuple[str, str, str], dict] = {}
        for raw in data.get("items", []):
            item = TextItem(
                file=raw.get("file", ""),
                key=raw.get("key", ""),
                original=raw.get("original", ""),
                translated=raw.get("translated", ""),
                context=raw.get("context", ""),
                line=raw.get("line", 0),
                meta=raw.get("meta", {}) or {},
            )
            items.append(item)
            raw_by_key[(item.file, item.key, item.original)] = raw

        self._update_progress("polish", 45)
        result = await DeepSeekPolisher().polish_items(
            items,
            budget_cny=budget_cny,
            on_progress=lambda cur, tot: self._update_item_progress(cur, tot),
        )

        changed_meta = 0
        for item in result.items:
            raw = raw_by_key.get((item.file, item.key, item.original))
            if raw is None:
                continue
            if raw.get("translated") != item.translated:
                raw["translated"] = item.translated
            if raw.get("meta") != item.meta:
                raw["meta"] = item.meta
                changed_meta += 1

        data["translated_count"] = sum(1 for r in data.get("items", []) if r.get("translated"))
        data["polish_stats"] = result.stats.to_dict()
        payload = _json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        checkpoint.write_text(payload, encoding="utf-8")
        game_checkpoint = _game_meta_path(path, "translation_checkpoint.json")
        if game_checkpoint != checkpoint:
            game_checkpoint.parent.mkdir(parents=True, exist_ok=True)
            game_checkpoint.write_text(payload, encoding="utf-8")

        stats_path = _game_meta_path(path, "polish_stats.json")
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(_json.dumps(result.stats.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        if self.diagnostics:
            self.diagnostics.set("polish_stats", result.stats.to_dict())
        info(
            f"文本润色完成: 候选 {result.stats.eligible_texts}，改写 {result.stats.changed_count}，"
            f"已审 {result.stats.unchanged_count}，预算截断 {result.stats.budget_limited}，"
            f"估算费用 ¥{result.stats.estimated_cost_cny}"
        )
        if changed_meta:
            info(f"润色检查点已标记 {changed_meta} 条审校状态")

        self._update_progress("patch", 70)
        return await self._do_patch_only(path, checkpoint, launch, injector)

    def _load_checkpoint_into(self, items: list, checkpoint: Path):
        return pipeline_checkpoint_stage.load_checkpoint_into(self, items, checkpoint)

    def _save_checkpoint_json(self, items: list, game_path: Path,
                               source_lang: str, target_lang: str, json_path: Path):
        return pipeline_checkpoint_stage.save_checkpoint_json(
            self, items, game_path, source_lang, target_lang, json_path)

    def _sync_checkpoint_json(self, checkpoint: Path, items: list) -> None:
        return pipeline_checkpoint_stage.sync_checkpoint_json(self, checkpoint, items)

    async def _translate_with_checkpoint(self, items: list, checkpoint: Path,
                                          source_lang: str, target_lang: str,
                                          game_path: Path | None = None) -> list:
        return await pipeline_translate_stage.translate_with_checkpoint(
            self, items, checkpoint, source_lang, target_lang, game_path)

    async def _retry_invalid_translations(self, items: list, checkpoint: Path,
                                          source_lang: str, target_lang: str,
                                          game_path: Path | None = None) -> list:
        return await pipeline_translate_stage.retry_invalid_translations(
            self, items, checkpoint, source_lang, target_lang, game_path)

    async def _do_patch_only(self, game_path: Path, json_path: Path,
                              launch: bool, injector: str | None) -> bool:
        return await pipeline_patch_stage.do_patch_only(self, game_path, json_path, launch, injector)

    # ------------------------------------------------------------------
    # 安全校验
    # ------------------------------------------------------------------

    def _select_engine(self, path: Path):
        return pipeline_detect_stage.select_engine(self, path)

    @staticmethod
    def _try_signature_hints(path: Path):
        return pipeline_detect_stage.try_signature_hints(path)

    async def _fallback_realtime(self, path: Path, launch: bool) -> bool:
        """引擎检测失败时的兜底方案：用通用 Hook 捕获文本，字幕窗口显示译文。

        不需要知道引擎类型，不需要提取文件，不需要回填。
        启动游戏 → Frida Hook ExtTextOutW/DrawTextW/mono_string_new → 字幕窗口。
        """
        import subprocess as _sp, time as _t, sys as _sys
        import asyncio, hashlib

        # 找可执行文件
        p = path if path.is_dir() else path.parent
        exes = sorted(
            [f for f in p.glob("*.exe") if "unins" not in f.stem.lower() and "crash" not in f.stem.lower()],
            key=lambda x: x.stat().st_size, reverse=True,
        )
        if not exes:
            self.diagnostics.error("未找到游戏可执行文件", path="path")
            self.diagnostics.finish(False)
            error("未找到可执行文件，无法启动实时翻译")
            return False
        exe = exes[0]

        try:
            import frida
        except ImportError:
            info("Frida 未安装，回退到直接启动游戏")
            if launch:
                _sp.Popen([str(exe)], cwd=str(exe.parent))
            self.diagnostics.finish(True)
            return True

        info(f"[兜底] 启动游戏: {exe.name}")
        self._update_progress("启动游戏", 40)
        proc = _sp.Popen([str(exe)], cwd=str(exe.parent))
        _t.sleep(2)

        try:
            device = frida.get_local_device()
            session = device.attach(proc.pid)
        except Exception as e:
            info(f"[兜底] Frida 连接失败 ({e})，游戏已正常启动")
            self.diagnostics.finish(True)
            return True

        # 启动字幕 overlay 子进程
        shm_name = f"gametrans_rt_{proc.pid}"
        from core.translation_overlay_window import OverlayShmWriter
        overlay_cmd, overlay_cwd, overlay_env = _overlay_window_command(
            exe.parent.name, exe.name, str(exe), shm_name,
        )
        creationflags = 0
        if _sys.platform == "win32":
            creationflags = getattr(_sp, "CREATE_NO_WINDOW", 0)
        overlay_proc = None
        try:
            overlay_proc = _sp.Popen(
                overlay_cmd, cwd=overlay_cwd, env=overlay_env,
                creationflags=creationflags
            )
        except Exception as e:
            info(f"[兜底] 字幕窗口启动失败: {e}")

        shm_writer = OverlayShmWriter(shm_name)

        script_path = resource_path("frida", "realtime_hook.js")
        script_src = "var GM_SRC_LANG = 'auto';\n" + script_path.read_text(encoding="utf-8")
        script = session.create_script(script_src)

        from core.realtime_translator import RealtimeTranslator
        from engines.base import TextItem

        translator = _get_translator(get_config().active_translator)
        if not translator:
            info("[兜底] 未配置翻译器，游戏直接运行无翻译")
            shm_writer.close()
            if overlay_proc and overlay_proc.poll() is None:
                try: overlay_proc.terminate()
                except Exception: pass
            self.diagnostics.finish(True)
            return True

        cfg = get_config()
        src_lang = str(getattr(cfg, "source_lang", "auto") or "auto")
        tgt_lang = str(getattr(cfg, "target_lang", "zh-CN") or "zh-CN")

        def translate_fn(text: str) -> str:
            async def run_once() -> str:
                item = TextItem(
                    file="__fallback_realtime__.jsonl",
                    key=hashlib.sha1(text.encode("utf-8")).hexdigest()[:16],
                    original=text,
                    translated="",
                    context="realtime",
                    meta={"runtime_capture": True},
                )
                result = await translator.translate_batch([item], src_lang, tgt_lang)
                if result and result[0].translated:
                    return str(result[0].translated).strip()
                return ""
            return asyncio.run(run_once())

        pipeline = RealtimeTranslator(translate_fn, source_lang=src_lang, target_lang=tgt_lang)

        def push_result(original: str, translated: str):
            try:
                shm_writer.write(original, translated)
            except Exception:
                pass

        def on_msg(msg, _data):
            if msg["type"] != "send":
                return
            pl = msg.get("payload", {})
            if pl.get("type") == "new_text":
                text = pl.get("text", "")
                if text:
                    priority = 1 if len(text) < 20 else 0
                    pipeline.submit(text, on_result=push_result, priority=priority)

        script.on("message", on_msg)
        script.load()

        info(f"[兜底] 实时翻译已就绪（字幕窗口模式），游戏运行时自动翻译")
        self._update_progress("翻译中", 50)
        self.diagnostics.set("fallback", {"mode": "realtime_overlay", "exe": str(exe), "shm": shm_name})
        self._write_completion_notice(path, None, mode="实时翻译/显示层")
        self.diagnostics.finish(True)

        if not launch:
            shm_writer.close()
            try: session.detach()
            except Exception: pass
            try: proc.terminate()
            except Exception: pass
            if overlay_proc and overlay_proc.poll() is None:
                try: overlay_proc.terminate()
                except Exception: pass
            return True

        # 等待游戏或字幕窗口关闭
        try:
            while True:
                if proc.poll() is not None:
                    break
                if overlay_proc and overlay_proc.poll() is not None:
                    break
                _t.sleep(0.5)
        finally:
            shm_writer.close()
            try: session.detach()
            except Exception: pass
            if overlay_proc and overlay_proc.poll() is None:
                try: overlay_proc.terminate()
                except Exception: pass
        return True

    @staticmethod
    def _auto_probe(path: Path) -> dict | None:
        return pipeline_detect_stage.auto_probe(path)

    def _validate_extraction_quality(self, items: list) -> None:
        return pipeline_detect_stage.validate_extraction_quality(self, items)

    def _record_unsupported_engine(self, engine):
        return pipeline_detect_stage.record_unsupported_engine(self, engine)

    def _fallback_generic(self, path: Path):
        return pipeline_detect_stage.fallback_generic(self, path)

    def _record_extraction(self, engine, items: list):
        return pipeline_detect_stage.record_extraction(self, engine, items)

    def _record_no_items(self, engine, path: Path, injector: str | None):
        return pipeline_detect_stage.record_no_items(self, engine, path, injector)

    def _enter_kirikiri_runtime_capture_mode(
        self,
        game_path: Path,
        engine,
        injector: str | None,
        checkpoint: Path | None,
        *,
        launch: bool,
        extract_only: bool,
    ) -> bool:
        return pipeline_runtime_stage.enter_kirikiri_runtime_capture_mode(
            self, game_path, engine, injector, checkpoint,
            launch=launch, extract_only=extract_only)

    def _record_modified_outputs(self, game_path: Path, items: list, engine):
        return pipeline_runtime_stage.record_modified_outputs(self, game_path, items, engine)

    def _verify_repack_outputs(self, game_path: Path, items: list, engine) -> dict:
        return pipeline_runtime_stage.verify_repack_outputs(self, game_path, items, engine)

    def _verify_kirikiri_runtime_overlay_outputs(self, game_path: Path, translated: list, result: dict) -> dict:
        return pipeline_runtime_stage.verify_kirikiri_runtime_overlay_outputs(self, game_path, translated, result)

    def _verify_bgi_outputs(self, game_path: Path, translated: list, engine, result: dict) -> dict:
        return pipeline_runtime_stage.verify_bgi_outputs(self, game_path, translated, engine, result)

    def _verify_godot_pck_outputs(self, game_path: Path, translated: list, engine, result: dict) -> dict:
        return pipeline_runtime_stage.verify_godot_pck_outputs(self, game_path, translated, engine, result)

    def _validate_all(self, items: list) -> list:
        return pipeline_runtime_stage.validate_all(self, items)

    @staticmethod
    def _stop_translation_proxy(engine):
        return pipeline_runtime_stage.stop_translation_proxy(engine)

    def _auto_select_injector(self, injector: str | None, engine, game_path: Path) -> str | None:
        return pipeline_runtime_stage.auto_select_injector(self, injector, engine, game_path)

    def _create_runtime_launchers(self, game_path: Path, engine, checkpoint: Path | None) -> None:
        return pipeline_runtime_stage.create_runtime_launchers(self, game_path, engine, checkpoint)

    def _prepare_runtime_dependencies(self, game_path: Path, engine) -> None:
        return pipeline_runtime_stage.prepare_runtime_dependencies(self, game_path, engine)

    def _restore_godot_static_patch_artifacts_for_runtime(self, game_path: Path) -> list[str]:
        return pipeline_runtime_stage.restore_godot_static_patch_artifacts_for_runtime(self, game_path)

    def _disable_kirikiri_static_patch_artifacts_for_runtime(self, game_path: Path, engine) -> None:
        return pipeline_runtime_stage.disable_kirikiri_static_patch_artifacts_for_runtime(self, game_path, engine)

    def _prepare_kirikiri_patch_bridge_tooling(self, game_path: Path, engine) -> None:
        return pipeline_runtime_stage.prepare_kirikiri_patch_bridge_tooling(self, game_path, engine)

    def _prepare_kirikiri_unencrypted_version_bridge(self, game_path: Path, engine) -> dict:
        return pipeline_runtime_stage.prepare_kirikiri_unencrypted_version_bridge(self, game_path, engine)

    def _setup_engine_repack(self, engine, game_path: Path):
        return pipeline_runtime_stage.setup_engine_repack(self, engine, game_path)

    def _verify_repack_integrity(self, items: list, engine) -> bool:
        return pipeline_runtime_stage.verify_repack_integrity(self, items, engine)

    def _backup_game_files(self, game_path: Path, items: list | None = None, engine=None):
        return pipeline_runtime_stage.backup_game_files(self, game_path, items, engine)


def _contains_cjk(text: str) -> bool:
    for ch in text:
        code = ord(ch)
        if (
            0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
            or 0x20000 <= code <= 0x2A6DF
            or 0x2A700 <= code <= 0x2B73F
            or 0x2B740 <= code <= 0x2B81F
            or 0x2B820 <= code <= 0x2CEAF
        ):
            return True
    return False




def _read_game_meta_json(game_path: Path, filename: str) -> dict:
    path = _game_meta_path(game_path, filename)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}




def _game_meta_path(game_path: Path, filename: str) -> Path:
    game_dir = game_path if game_path.is_dir() else game_path.parent
    return game_dir / "_translation_meta" / filename




def _contains_japanese_kana(text: str) -> bool:
    return any(
        "\u3040" <= ch <= "\u30ff" or "\uff66" <= ch <= "\uff9f"
        for ch in text or ""
    )


def _godot_variant_string_values(data: bytes) -> set[str]:
    """Read exact Godot VARIANT_STRING values from binary .scn/.res data."""
    import struct

    values: set[str] = set()
    pos = 0
    while pos + 12 <= len(data):
        try:
            vtype = struct.unpack_from("<I", data, pos)[0]
        except Exception:
            break
        if vtype == 5:
            try:
                slen = struct.unpack_from("<I", data, pos + 4)[0]
            except Exception:
                break
            if 0 < slen < 10000 and pos + 8 + slen <= len(data):
                raw = data[pos + 8:pos + 8 + slen]
                if raw and raw[-1] == 0:
                    try:
                        values.add(raw[:-1].decode("utf-8"))
                    except UnicodeDecodeError:
                        pass
                    pos += 8 + slen
                    continue
        pos += 1
    return values




def _find_gamemaker_data_win(game_dir: Path) -> Path | None:
    direct = game_dir / "data.win"
    if direct.is_file():
        return direct
    for sub in ("game", "data", "assets"):
        candidate = game_dir / sub / "data.win"
        if candidate.is_file():
            return candidate
    return None
