"""KiriKiri 自动 dump 阶段：seed/targets 读写、scn ref 索引、目标名解析、
外部 KrkrDump / 原生 dump 探针、KrkrDump/KrkrPatch 工具运行时与 config 写入。

模块级函数收 pipeline 作第一参数（Pipeline 类保留委托薄壳），
与 core/pipeline_stage_runtime.py 的拆分模式一致。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from config import get_config
from core.launcher import prepare_kirikiri_native_runtime
from utils.logger import info, warning


def prepare_kirikiri_repack_sources_from_meta_dump(pipeline, game_path: Path, items: list) -> int:
    from core import pipeline as _pipeline_mod

    if not pipeline.workspace:
        return 0
    game_dir = game_path if game_path.is_dir() else game_path.parent
    meta_dir = game_dir / "_translation_meta"
    dump_dir = meta_dir / "kirikiri_dump"
    dump_zip = meta_dir / "kirikiri_dump.zip"
    original_dir = pipeline.workspace.root / "original"
    needed = {
        _pipeline_mod._checkpoint_item_rel(getattr(item, "file", "") or "")
        for item in items
        if _pipeline_mod._has_effective_translation(item)
    }
    needed.discard("")
    if not needed:
        return 0

    copied = 0
    for rel in sorted(needed):
        dst = _pipeline_mod._safe_workspace_child(original_dir, rel)
        if dst is None or dst.exists():
            continue
        src = dump_dir / rel
        if src.is_file():
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
            except OSError as exc:
                warning(f"KiriKiri 快速回填源复制失败: {rel} - {exc}")

    if dump_zip.exists() and copied < len(needed):
        try:
            import zipfile as _zipfile
            with _zipfile.ZipFile(dump_zip) as zf:
                names = {name.replace("\\", "/"): name for name in zf.namelist()}
                for rel in sorted(needed):
                    dst = _pipeline_mod._safe_workspace_child(original_dir, rel)
                    if dst is None or dst.exists():
                        continue
                    archive_name = names.get(rel) or names.get(f"kirikiri_dump/{rel}")
                    if not archive_name:
                        continue
                    info_obj = zf.getinfo(archive_name)
                    if info_obj.file_size > 8 * 1024 * 1024:
                        warning(f"KiriKiri 快速回填源跳过过大脚本: {rel}")
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(zf.read(archive_name))
                    copied += 1
        except Exception as exc:
            warning(f"KiriKiri dump archive 快速准备失败: {exc}")

    if copied:
        info(f"KiriKiri 从资源层 dump 快速准备回填脚本: {copied}/{len(needed)}")
        if pipeline.diagnostics:
            pipeline.diagnostics.set("kirikiri_fast_repack_sources", {
                "copied": copied,
                "needed": len(needed),
                "source": str(dump_dir if dump_dir.is_dir() else dump_zip),
            })
    return copied


def merge_kirikiri_runtime_capture_raw_items(pipeline, game_path: Path, raw_items: list[dict]) -> list[dict]:
    """Add new runtime KAGParser captures to a reused KiriKiri checkpoint.

    Fast resume intentionally skips extraction, but KiriKiri runtime overlay
    learns missing visible strings while the game runs. Those captured misses
    still need to enter the next translation pass.
    """
    from core import pipeline as _pipeline_mod
    if not bool(getattr(get_config(), "kirikiri_runtime_merge_captures", True)):
        if pipeline.diagnostics:
            pipeline.diagnostics.set("kirikiri_runtime_capture_merge_skipped", True)
        return raw_items
    meta_dir = (game_path if game_path.is_dir() else game_path.parent) / "_translation_meta"
    capture = meta_dir / "kirikiri_runtime_capture.jsonl"
    if not capture.exists():
        return raw_items
    try:
        from engines.kirikiri import KiriKiriEngine

        runtime_items = KiriKiriEngine()._load_runtime_capture_items(
            game_path if game_path.is_dir() else game_path.parent
        )
    except Exception as exc:
        warning(f"KiriKiri 运行时捕获记录合并失败: {exc}")
        return raw_items
    if not runtime_items:
        return raw_items

    existing_originals = {str(raw.get("original", "")) for raw in raw_items}
    merged = list(raw_items)
    added = 0
    for item in runtime_items:
        if item.original in existing_originals:
            continue
        existing_originals.add(item.original)
        merged.append({
            "file": item.file,
            "key": item.key,
            "original": item.original,
            "context": item.context,
            "line": item.line,
            "meta": item.meta,
            "translated": item.translated if _pipeline_mod._has_effective_translation(item) else "",
        })
        added += 1
    if added:
        info(f"KiriKiri 从运行时捕获记录补充 {added} 条文本到续翻检查点")
        if pipeline.diagnostics:
            pipeline.diagnostics.set("kirikiri_runtime_capture_merged", added)
    return merged


def try_kirikiri_pre_extract_auto_dump(pipeline, path: Path, engine) -> bool:
    if getattr(engine, "name", "") != "kirikiri":
        return False
    game_dir = path if path.is_dir() else path.parent
    prepared = _prepare_kirikiri_auto_dump_seed_targets(game_dir)
    if pipeline.diagnostics and prepared:
        pipeline.diagnostics.set("kirikiri_pre_extract_target_index", prepared)
    if prepared and pipeline.diagnostics:
        pipeline.diagnostics.set("kirikiri_auto_dump", {
            "status": "skipped",
            "reason": "dump_disabled_unstable",
            "target_count": int((prepared or {}).get("target_count") or 0),
            "protected_hint_count": int((prepared or {}).get("protected_hint_count") or 0),
        })
        pipeline.diagnostics.suggest(
            "KiriKiri runtime dump is disabled in the pipeline; use static XP3 decrypt or offline extractor paths instead."
        )
    if prepared:
        info("KiriKiri runtime dump disabled; using static extraction/decrypt paths only")
    return False
    if not _load_kirikiri_auto_dump_seed_targets(game_dir):
        return False
    if not getattr(get_config(), "kirikiri_auto_launch_dump", False):
        if pipeline.diagnostics:
            pipeline.diagnostics.set("kirikiri_auto_dump", {
                "status": "skipped",
                "reason": "auto_launch_dump_disabled",
                "target_count": int((prepared or {}).get("target_count") or 0),
            })
            pipeline.diagnostics.suggest(
                "KiriKiri 受保护脚本需要启动游戏做资源层 dump；当前默认关闭自动拉起游戏以避免影响桌面/加速器。"
            )
        info("KiriKiri 自动启动 dump 已关闭，先尝试静态提取")
        return False
    if not _kirikiri_auto_dump_needed(path, engine, []):
        return False
    pipeline._try_kirikiri_auto_dump_stage(path, engine, [], None)
    return True


def try_kirikiri_auto_dump_stage(
    pipeline,
    path: Path,
    engine,
    items: list,
    file_filter: list[str] | None = None,
):
    from core import pipeline as _pipeline_mod
    if getattr(engine, "name", "") != "kirikiri":
        return engine, items, len(items)
    setattr(engine, "_kirikiri_auto_dump_incomplete", False)

    if not _kirikiri_auto_dump_needed(path, engine, items):
        return engine, items, len(items)

    game_dir = path if path.is_dir() else path.parent
    existing_dump_count = _count_kirikiri_dump_scripts(game_dir)
    if pipeline.diagnostics:
        pipeline.diagnostics.set("kirikiri_auto_dump", {
            "status": "skipped",
            "reason": "dump_disabled_unstable",
            "initial_dump_count": existing_dump_count,
        })
        pipeline.diagnostics.suggest(
            "KiriKiri runtime dump is disabled in the pipeline; use static XP3 decrypt or offline extractor paths instead."
        )
    info("KiriKiri runtime dump disabled; using static extraction/decrypt paths only")
    return engine, items, len(items)
    if not getattr(get_config(), "kirikiri_auto_launch_dump", False) and existing_dump_count <= 0:
        if pipeline.diagnostics:
            pipeline.diagnostics.set("kirikiri_auto_dump", {
                "status": "skipped",
                "reason": "auto_launch_dump_disabled",
                "initial_dump_count": existing_dump_count,
            })
        info("KiriKiri 自动启动 dump 已关闭，跳过运行时 dump 探针")
        return engine, items, len(items)
    meta_dir = game_dir / "_translation_meta"
    target_file = meta_dir / "kirikiri_dump_targets.txt"
    expected = _kirikiri_expected_script_count(path, engine)
    dumped_rels = set(_list_kirikiri_dump_script_rels(game_dir))
    before_count = len(dumped_rels)
    target_queue: list[str] = []
    target_seen: set[str] = set()
    for target in _load_kirikiri_auto_dump_seed_targets(game_dir):
        _append_kirikiri_target_variants(target_queue, target_seen, target)
    for target in _collect_kirikiri_dump_targets(game_dir, expected, include_inferred=False):
        _append_kirikiri_target_variants(target_queue, target_seen, target)
    if target_queue:
        _save_kirikiri_explicit_dump_targets(game_dir, target_queue)

    pipeline._update_progress("KiriKiri 自动脚本 dump", 24)
    info(
        "KiriKiri 受保护脚本进入自动 dump 阶段: "
        f"已有 {before_count} 个，预计 {expected or '未知'} 个"
    )
    if pipeline.diagnostics:
        pipeline.diagnostics.set("kirikiri_auto_dump", {
            "status": "started",
            "expected_script_count": expected,
            "initial_dump_count": before_count,
        })

    dumped_total = before_count
    rounds: list[dict] = []
    attempted_targets: set[str] = set()
    protected_probe_target_count = sum(
        1 for target in target_queue
        if ">" in target and _kirikiri_is_extensionless_dump_target(target)
    )
    max_rounds = max(16, min(96, (protected_probe_target_count + 3) // 4 + 8))
    batch_size = 32
    dump_dir = game_dir / "_translation_meta" / "kirikiri_dump"
    ref_index = _load_kirikiri_scn_ref_index(game_dir)
    if getattr(get_config(), "kirikiri_use_external_krkrdump", False):
        external_result = _run_kirikiri_external_dump_probe(
            path,
            engine,
            target_queue,
            timeout_seconds=90,
        )
    else:
        external_result = {
            "attempted": False,
            "tool": "krkrdump",
            "reason": "disabled_by_default",
        }
        if pipeline.diagnostics:
            pipeline.diagnostics.step("kirikiri_external_dump", status="skipped", **external_result)
    if external_result.get("attempted"):
        current_rels = set(_list_kirikiri_dump_script_rels(game_dir))
        new_rels = sorted(current_rels - dumped_rels, key=_natural_kirikiri_target_sort_key)
        dumped_rels = current_rels
        after_count = len(dumped_rels)
        added = max(0, after_count - dumped_total)
        dumped_total = after_count
        ref_targets_added = _extend_kirikiri_targets_from_dump_refs(
            game_dir,
            dump_dir,
            new_rels,
            ref_index,
            target_queue,
            target_seen,
        )
        if ref_index.get("_dirty"):
            _save_kirikiri_scn_ref_index(game_dir, ref_index)
        if ref_targets_added:
            _save_kirikiri_explicit_dump_targets(game_dir, target_queue)
        external_payload = {
            "round": 0,
            "mode": "krkrdump",
            "target_count": len(target_queue),
            "dump_count": after_count,
            "added": added,
            "new_ref_targets": ref_targets_added,
            **external_result,
        }
        rounds.append(external_payload)
        if pipeline.diagnostics:
            pipeline.diagnostics.step("kirikiri_external_dump", **external_payload)
        info(
            "KiriKiri KrkrDump 文件流 dump: "
            f"导入 {external_result.get('imported_files', 0)} 个脚本，"
            f"当前 {after_count}/{expected or '未知'}"
        )
    for round_idx in range(1, max_rounds + 1):
        if not target_queue:
            for target in _collect_kirikiri_dump_targets(game_dir, expected, include_inferred=False):
                _append_kirikiri_target_variants(target_queue, target_seen, target)
        missing_targets = _missing_kirikiri_active_dump_targets(game_dir, target_queue)
        missing_targets = [target for target in missing_targets if target not in attempted_targets]
        passive_targets = _missing_kirikiri_active_dump_targets(
            game_dir,
            target_queue,
            exclude=attempted_targets,
            active_only=False,
        )
        if not missing_targets:
            if passive_targets:
                missing_targets = passive_targets[:batch_size]
            elif not target_queue and expected:
                missing_targets = _default_kirikiri_seed_targets()
            else:
                break

        current_batch_size = 4 if any(_kirikiri_is_extensionless_dump_target(t) for t in missing_targets) else batch_size
        missing_targets = missing_targets[:current_batch_size]
        attempted_targets.update(missing_targets)
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(
            "\n".join(missing_targets) + ("\n" if missing_targets else ""),
            encoding="utf-8",
        )
        info(f"KiriKiri 自动 dump 第 {round_idx} 轮: 尝试 {len(missing_targets)} 个脚本目标")
        active_probe_count = sum(
            1 for target in missing_targets
            if _kirikiri_is_active_dump_target(target) or _kirikiri_is_extensionless_dump_target(target)
        )
        if any(_kirikiri_is_extensionless_dump_target(t) for t in missing_targets):
            probe_timeout = max(20, min(60, len(missing_targets) * 12))
        else:
            probe_timeout = max(45, min(240, len(missing_targets) * 8))
        if active_probe_count == 0:
            probe_timeout = 30
        if getattr(get_config(), "kirikiri_auto_launch_dump", False):
            result = _run_kirikiri_native_dump_probe(
                path,
                engine,
                missing_targets,
                timeout_seconds=probe_timeout,
            )
        else:
            result = {
                "launched": False,
                "reason": "auto_launch_dump_disabled",
                "target_count": len(missing_targets),
                "extensionless_target_count": sum(
                    1 for target in missing_targets
                    if _kirikiri_is_extensionless_dump_target(target)
                ),
                "before": dumped_total,
                "after": dumped_total,
                "added": 0,
            }
        current_rels = set(_list_kirikiri_dump_script_rels(game_dir))
        new_rels = sorted(current_rels - dumped_rels, key=_natural_kirikiri_target_sort_key)
        dumped_rels = current_rels
        after_count = len(dumped_rels)
        added = max(0, after_count - dumped_total)
        dumped_total = after_count
        ref_targets_added = _extend_kirikiri_targets_from_dump_refs(
            game_dir,
            dump_dir,
            new_rels,
            ref_index,
            target_queue,
            target_seen,
        )
        if ref_index.get("_dirty"):
            _save_kirikiri_scn_ref_index(game_dir, ref_index)
        if ref_targets_added:
            _save_kirikiri_explicit_dump_targets(game_dir, target_queue)
        round_payload = {
            "round": round_idx,
            "target_count": len(missing_targets),
            "dump_count": after_count,
            "added": added,
            "new_ref_targets": ref_targets_added,
            **result,
        }
        rounds.append(round_payload)
        if pipeline.diagnostics:
            pipeline.diagnostics.step("kirikiri_auto_dump_round", **round_payload)
        if _kirikiri_dump_target_closure_complete(game_dir):
            _pipeline_mod._stop_processes_under_dir(game_dir)
            break
        if expected and after_count >= expected and _kirikiri_count_threshold_sufficient(game_dir):
            _pipeline_mod._stop_processes_under_dir(game_dir)
            break
        remaining_high_confidence = _missing_kirikiri_active_dump_targets(
            game_dir,
            target_queue,
            exclude=attempted_targets,
        )
        remaining_passive = _missing_kirikiri_active_dump_targets(
            game_dir,
            target_queue,
            exclude=attempted_targets,
            active_only=False,
        )
        if added == 0 and ref_targets_added == 0 and not remaining_high_confidence and not remaining_passive:
            break

    # Avoid making the finished launcher spend time probing already dumped
    # targets on every user launch. If dump remains incomplete, leave the
    # last missing target list in place as a diagnostic/retry aid.
    target_closure_complete = _kirikiri_dump_target_closure_complete(game_dir)
    if target_closure_complete or (expected and dumped_total >= expected and _kirikiri_count_threshold_sufficient(game_dir)):
        if target_queue:
            _save_kirikiri_explicit_dump_targets(game_dir, target_queue)
        if target_file.exists():
            try:
                target_file.unlink()
            except OSError:
                pass
    elif target_queue:
        pending_targets = _missing_kirikiri_active_dump_targets(
            game_dir,
            target_queue,
            exclude=attempted_targets,
            active_only=False,
        )
        if pending_targets:
            target_file.write_text(
                "\n".join(pending_targets[:260]) + "\n",
                encoding="utf-8",
            )

    current_dump_count = len(_list_kirikiri_dump_script_rels(game_dir))
    primary_targets = _kirikiri_primary_dump_targets(game_dir)
    active_missing = _missing_kirikiri_active_dump_targets(
        game_dir,
        primary_targets,
    )
    passive_missing = _missing_kirikiri_active_dump_targets(
        game_dir,
        primary_targets,
        active_only=False,
    )
    inferred_missing = _missing_kirikiri_active_dump_targets(
        game_dir,
        _collect_kirikiri_dump_targets(game_dir, expected, include_inferred=True),
    )
    low_confidence_missing_count = max(0, len(inferred_missing) - len(active_missing))
    complete_before_refresh = bool(
        target_closure_complete
        or (expected and current_dump_count >= expected and _kirikiri_count_threshold_sufficient(game_dir))
    )
    if not complete_before_refresh:
        if pipeline.diagnostics:
            pipeline.diagnostics.set("kirikiri_auto_dump", {
                "status": "incomplete",
                "expected_script_count": expected,
                "initial_dump_count": before_count,
                "final_dump_count": current_dump_count,
                "target_closure_complete": target_closure_complete,
                "active_missing_count": len(active_missing),
                "passive_missing_count": len(passive_missing),
                "total_missing_count": len(passive_missing),
                "low_confidence_missing_count": low_confidence_missing_count,
                "low_confidence_probe_disabled": True,
                "rounds": rounds,
                "refresh_extract_skipped": True,
            })
        setattr(engine, "_kirikiri_auto_dump_incomplete", True)
        warning(
            "KiriKiri 自动 dump 未拿到完整脚本，已停止翻译以避免生成半成品: "
            f"{current_dump_count}/{expected}"
        )
        if pipeline.diagnostics:
            pipeline.diagnostics.warn(
                "KiriKiri 自动 dump 不完整，已阻断翻译",
                dumped=current_dump_count,
                expected=expected,
            )
            pipeline.diagnostics.suggest(
                "该游戏的受保护脚本需要更多启动入口或 PackinOne 映射解析；当前阶段不会重复全量提取。"
            )
        return engine, items, len(items)

    engine, refreshed_items, extracted_count = pipeline._run_extract_stage(path, engine, file_filter)
    refreshed_dump_count = int(getattr(engine, "_runtime_dump_count", 0) or _count_kirikiri_dump_scripts(game_dir))
    target_closure_complete = _kirikiri_dump_target_closure_complete(game_dir)
    complete = bool(
        target_closure_complete
        or (expected and refreshed_dump_count >= expected and _kirikiri_count_threshold_sufficient(game_dir))
    )
    if pipeline.diagnostics:
        pipeline.diagnostics.set("kirikiri_auto_dump", {
            "status": "complete" if complete else "incomplete",
            "expected_script_count": expected,
            "initial_dump_count": before_count,
            "final_dump_count": refreshed_dump_count,
            "target_closure_complete": target_closure_complete,
            "rounds": rounds,
        })
    if not complete:
        setattr(engine, "_kirikiri_auto_dump_incomplete", True)
        warning(
            "KiriKiri 自动 dump 未拿到完整脚本，已停止翻译以避免生成半成品: "
            f"{refreshed_dump_count}/{expected}"
        )
        if pipeline.diagnostics:
            pipeline.diagnostics.warn(
                "KiriKiri 自动 dump 不完整，已阻断翻译",
                dumped=refreshed_dump_count,
                expected=expected,
            )
            pipeline.diagnostics.suggest(
                "该游戏的受保护脚本需要更多启动入口或 PackinOne 映射解析；"
                "管线不会继续翻译少量脚本来伪装全量完成。"
            )
    return engine, refreshed_items, extracted_count


def _prepare_kirikiri_auto_dump_seed_targets(game_dir: Path) -> dict[str, object] | None:
    try:
        from engines.kirikiri import prepare_kirikiri_dump_targets_from_game
    except Exception as exc:
        warning(f"KiriKiri XP3 索引目标准备失败: {exc}")
        return None
    try:
        result = prepare_kirikiri_dump_targets_from_game(game_dir)
    except Exception as exc:
        warning(f"KiriKiri XP3 索引目标准备失败: {exc}")
        return None
    target_count = int(result.get("target_count") or 0)
    if target_count:
        if result.get("runtime_dump_targets_enabled"):
            info(f"KiriKiri 首次快速 dump: 从 XP3 索引准备 {target_count} 个脚本目标")
        else:
            info(f"KiriKiri 受保护 XP3 索引发现 {target_count} 个脚本候选；默认不启动 dump，仅记录静态解密诊断")
    return result


def _kirikiri_auto_dump_needed(game_path: Path, engine, items: list) -> bool:
    game_dir = game_path if game_path.is_dir() else game_path.parent
    expected = _kirikiri_expected_script_count(game_path, engine)
    has_seed_targets = bool(_load_kirikiri_auto_dump_seed_targets(game_dir))
    if not expected and not has_seed_targets:
        return False
    dump_count = int(getattr(engine, "_runtime_dump_count", 0) or _count_kirikiri_dump_scripts(game_dir))
    if expected and dump_count >= expected:
        if not _kirikiri_count_threshold_sufficient(game_dir):
            return True
        return False
    if dump_count > 0:
        return True
    if not items and has_seed_targets:
        return True
    protected = int(getattr(engine, "_protected_script_count", 0) or 0)
    return protected > 0 and len(items) < max(200, protected * 2)


def _kirikiri_count_threshold_sufficient(game_dir: Path) -> bool:
    """Counts are only enough when there is no explicit target closure to honor."""
    targets = _kirikiri_primary_dump_targets(game_dir)
    if not targets:
        return True
    return not _missing_kirikiri_active_dump_targets(game_dir, targets, active_only=False)


def _kirikiri_expected_script_count(game_path: Path, engine) -> int:
    protected = int(getattr(engine, "_protected_script_count", 0) or 0)
    game_dir = game_path if game_path.is_dir() else game_path.parent
    diag_path = game_dir / "_translation_meta" / "extract_diagnostics.json"
    if diag_path.exists():
        try:
            diag = json.loads(diag_path.read_text(encoding="utf-8-sig", errors="replace"))
            protected = max(protected, int(diag.get("protected_script_count") or 0))
        except Exception:
            pass
    return protected


def _count_kirikiri_dump_scripts(game_dir: Path) -> int:
    return len(_list_kirikiri_dump_script_rels(game_dir))


def _list_kirikiri_dump_script_rels(game_dir: Path) -> list[str]:
    dump_dir = game_dir / "_translation_meta" / "kirikiri_dump"
    if not dump_dir.is_dir():
        return []
    rels: list[str] = []
    for path in dump_dir.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in {".ks", ".tjs", ".scn"} or not suffix:
            try:
                rels.append(path.relative_to(dump_dir).as_posix())
            except ValueError:
                continue
    return rels


_KIRIKIRI_SCN_REF_CACHE: dict[tuple[str, int, int], list[str]] = {}
_KIRIKIRI_SCN_REF_INDEX_CACHE: dict[str, dict] = {}


def _read_kirikiri_dump_storage_refs(path: Path) -> list[str]:
    try:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return []
    cached = _KIRIKIRI_SCN_REF_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from utils.kirikiri_psb import extract_kirikiri_scn_storage_refs
    except Exception:
        refs: list[str] = []
    else:
        try:
            refs = extract_kirikiri_scn_storage_refs(path.read_bytes())
        except Exception:
            refs = []
    _KIRIKIRI_SCN_REF_CACHE[key] = refs
    if len(_KIRIKIRI_SCN_REF_CACHE) > 512:
        for old_key in list(_KIRIKIRI_SCN_REF_CACHE)[:128]:
            _KIRIKIRI_SCN_REF_CACHE.pop(old_key, None)
    return refs


def _load_kirikiri_scn_ref_index(game_dir: Path) -> dict:
    index_path = game_dir / "_translation_meta" / "kirikiri_scn_refs.json"
    cache_key = str(index_path.resolve())
    try:
        stat = index_path.stat()
        cached = _KIRIKIRI_SCN_REF_INDEX_CACHE.get(cache_key)
        if cached and cached.get("_mtime_ns") == stat.st_mtime_ns and cached.get("_size") == stat.st_size:
            return cached
        data = json.loads(index_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            data = {}
        data.setdefault("files", {})
        data["_mtime_ns"] = stat.st_mtime_ns
        data["_size"] = stat.st_size
        _KIRIKIRI_SCN_REF_INDEX_CACHE[cache_key] = data
        return data
    except Exception:
        data = {"version": 1, "files": {}}
        _KIRIKIRI_SCN_REF_INDEX_CACHE[cache_key] = data
        return data


def _save_kirikiri_scn_ref_index(game_dir: Path, index: dict) -> None:
    index_path = game_dir / "_translation_meta" / "kirikiri_scn_refs.json"
    try:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "files": index.get("files", {}),
        }
        index_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        stat = index_path.stat()
        data["_mtime_ns"] = stat.st_mtime_ns
        data["_size"] = stat.st_size
        _KIRIKIRI_SCN_REF_INDEX_CACHE[str(index_path.resolve())] = data
    except OSError:
        pass


def _read_kirikiri_dump_storage_refs_indexed(game_dir: Path, dump_dir: Path, path: Path, index: dict) -> list[str]:
    try:
        rel = path.relative_to(dump_dir).as_posix()
        stat = path.stat()
    except OSError:
        return []
    except ValueError:
        return _read_kirikiri_dump_storage_refs(path)
    files = index.setdefault("files", {})
    entry = files.get(rel)
    if (
        isinstance(entry, dict)
        and entry.get("mtime_ns") == stat.st_mtime_ns
        and entry.get("size") == stat.st_size
        and isinstance(entry.get("refs"), list)
    ):
        return [str(ref) for ref in entry.get("refs", [])]
    refs = _read_kirikiri_dump_storage_refs(path)
    files[rel] = {
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "refs": refs,
    }
    index["_dirty"] = True
    return refs


def _default_kirikiri_seed_targets() -> list[str]:
    prefixes = ["scenario", "scn", "script", "scripts"]
    names = {"first.ks.scn", "start.ks.scn", "prologue.ks.scn"}
    for idx in range(1, 6):
        names.add(f"{idx:02d}.ks.scn")
        for prefix in prefixes:
            names.add(f"{prefix}/{idx:02d}.ks.scn")
    return sorted(names, key=_natural_kirikiri_target_sort_key)


def _collect_kirikiri_dump_targets(
    game_dir: Path,
    expected: int = 0,
    *,
    include_inferred: bool = True,
) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    if not include_inferred:
        for target in _load_kirikiri_auto_dump_seed_targets(game_dir):
            _append_kirikiri_target_variants(targets, seen, target)
        if targets:
            return targets
    dump_dir = game_dir / "_translation_meta" / "kirikiri_dump"
    if dump_dir.is_dir():
        ref_index = _load_kirikiri_scn_ref_index(game_dir)
        dumped_rels: list[str] = []
        dumped_paths = [path for path in dump_dir.rglob("*") if path.is_file()]
        dumped_paths.sort(key=lambda path: _kirikiri_dump_source_sort_key(dump_dir, path))
        for path in dumped_paths:
            rel = path.relative_to(dump_dir).as_posix()
            dumped_rels.append(rel)
            if path.suffix.lower() == ".scn":
                for ref in _read_kirikiri_dump_storage_refs_indexed(game_dir, dump_dir, path, ref_index):
                    _append_kirikiri_target_variants(targets, seen, ref)
        if ref_index.get("_dirty"):
            _save_kirikiri_scn_ref_index(game_dir, ref_index)
        if include_inferred:
            for target in _infer_kirikiri_sequential_targets(dumped_rels, expected):
                _append_kirikiri_target_variants(targets, seen, target)
    if not targets:
        for target in _default_kirikiri_seed_targets():
            _append_kirikiri_target_variants(targets, seen, target)
    return targets


def _load_kirikiri_auto_dump_seed_targets(game_dir: Path) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for name in ("kirikiri_explicit_dump_targets.txt", "kirikiri_dump_targets.txt"):
        path = game_dir / "_translation_meta" / name
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            target = line.strip()
            if not target or target.startswith("#") or target in seen:
                continue
            seen.add(target)
            out.append(target)
    return out


def _load_kirikiri_explicit_dump_targets(game_dir: Path) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for name in ("kirikiri_explicit_dump_targets.txt",):
        path = game_dir / "_translation_meta" / name
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            target = line.strip()
            if not target or target.startswith("#") or target in seen:
                continue
            seen.add(target)
            out.append(target)
    return out


def _save_kirikiri_explicit_dump_targets(game_dir: Path, targets: list[str]) -> None:
    normalized: list[str] = []
    seen: set[str] = set()
    for target in targets:
        if not _kirikiri_is_dump_target(target):
            continue
        if target in seen:
            continue
        seen.add(target)
        normalized.append(target)
    if not normalized:
        return
    out = game_dir / "_translation_meta" / "kirikiri_explicit_dump_targets.txt"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(normalized) + "\n", encoding="utf-8")
    except OSError:
        pass


def _extend_kirikiri_targets_from_dump_refs(
    game_dir: Path,
    dump_dir: Path,
    rels: list[str],
    ref_index: dict,
    target_queue: list[str],
    target_seen: set[str],
) -> int:
    added = 0
    for rel in rels:
        path = dump_dir / Path(rel)
        if path.suffix.lower() != ".scn":
            continue
        refs = _read_kirikiri_dump_storage_refs_indexed(game_dir, dump_dir, path, ref_index)
        for ref in refs:
            before = len(target_seen)
            _append_kirikiri_target_variants(target_queue, target_seen, ref)
            added += max(0, len(target_seen) - before)
    return added


def _append_kirikiri_target_variants(out: list[str], seen: set[str], raw: str) -> None:
    for target in _expand_kirikiri_target_name(raw):
        if not _kirikiri_is_dump_target(target):
            continue
        if target in seen:
            continue
        seen.add(target)
        out.append(target)


def _kirikiri_dump_source_sort_key(dump_dir: Path, path: Path):
    try:
        rel = path.relative_to(dump_dir).as_posix()
    except ValueError:
        rel = path.name
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    return (mtime, _natural_kirikiri_target_sort_key(rel))


def _expand_kirikiri_target_name(raw: str) -> list[str]:
    name = str(raw or "").strip().replace("\\", "/")
    if not name or name.startswith(("#", ";")):
        return []
    lowered = name.lower()
    if lowered.startswith(("http://", "https://", "file://")):
        return []
    out = [name]
    target_part = _kirikiri_target_inner_name(name)
    lowered_target = target_part.lower()
    prefix = name[: len(name) - len(target_part)] if target_part and name.endswith(target_part) else ""
    if lowered_target.endswith(".ks"):
        out.append(name + ".scn")
    elif lowered_target.endswith(".scn") and not lowered_target.endswith(".ks.scn"):
        out.append(prefix + target_part[:-4] + ".ks.scn")
    return out


def _kirikiri_target_inner_name(name: str) -> str:
    value = str(name or "").strip().replace("\\", "/")
    if ">" in value:
        return value.rsplit(">", 1)[-1]
    return value


def _kirikiri_is_active_dump_target(name: str) -> bool:
    lowered = _kirikiri_target_inner_name(name).lower()
    return lowered.endswith((".ks", ".tjs", ".scn"))


def _kirikiri_is_extensionless_dump_target(name: str) -> bool:
    value = str(name or "").strip().replace("\\", "/")
    if not value or value.startswith(("#", ";")):
        return False
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "file://")):
        return False
    leaf = _kirikiri_target_inner_name(value).rsplit("/", 1)[-1]
    return bool(leaf and "." not in leaf)


def _kirikiri_is_dump_target(name: str) -> bool:
    value = str(name or "").strip().replace("\\", "/")
    if not value or value.startswith(("#", ";")):
        return False
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "file://")):
        return False
    if _kirikiri_is_active_dump_target(value):
        return True
    return _kirikiri_is_extensionless_dump_target(value)


def _infer_kirikiri_sequential_targets(existing_rels: list[str], expected: int = 0) -> list[str]:
    inferred: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(r"^(?P<prefix>.*?)(?P<num>\d{2,4})(?P<suffix>\.ks(?:\.scn)?|\.scn)$", re.I)
    groups: dict[tuple[str, str, int], set[int]] = {}
    group_order: list[tuple[str, str, int]] = []
    for rel in existing_rels:
        normalized = rel.replace("\\", "/")
        m = pattern.match(normalized)
        if not m:
            continue
        number = m.group("num")
        key = (m.group("prefix"), m.group("suffix"), len(number))
        if key not in groups:
            group_order.append(key)
        groups.setdefault(key, set()).add(int(number))
    for prefix, suffix, width in group_order:
        nums = groups[(prefix, suffix, width)]
        if not nums:
            continue
        start = min(nums)
        contiguous = start
        while contiguous + 1 in nums:
            contiguous += 1
        for value in range(contiguous + 1, contiguous + 9):
            target = f"{prefix}{value:0{width}d}{suffix}"
            if target in seen:
                continue
            seen.add(target)
            inferred.append(target)
    return inferred


def _missing_kirikiri_active_dump_targets(
    game_dir: Path,
    targets: list[str],
    *,
    exclude: set[str] | None = None,
    active_only: bool = True,
) -> list[str]:
    dump_dir = game_dir / "_translation_meta" / "kirikiri_dump"
    missing: list[str] = []
    target_set = set(targets)
    exclude = exclude or set()
    for target in targets:
        if active_only and not _kirikiri_is_active_dump_target(target):
            continue
        if not active_only and not _kirikiri_is_dump_target(target):
            continue
        if target in exclude:
            continue
        inner = _kirikiri_target_inner_name(target)
        if inner.lower().endswith(".ks") and (target + ".scn") in target_set:
            continue
        rel = inner.replace("/", "\\")
        archive_rel = _kirikiri_archive_prefixed_dump_rel(target)
        if (dump_dir / rel).exists() or (archive_rel and (dump_dir / archive_rel).exists()):
            continue
        if inner.lower().endswith(".ks.scn"):
            ks_inner = inner[:-4]
            ks_rel = ks_inner.replace("/", "\\")
            if (dump_dir / ks_rel).exists():
                continue
            if ">" in target:
                archive_prefix = target.split(">", 1)[0] + ">"
                ks_archive_rel = _kirikiri_archive_prefixed_dump_rel(archive_prefix + ks_inner)
                if ks_archive_rel and (dump_dir / ks_archive_rel).exists():
                    continue
        missing.append(target)
    return missing[:260]


def _kirikiri_archive_prefixed_dump_rel(target: str) -> str:
    value = str(target or "").strip().replace("\\", "/")
    if ">" not in value:
        return ""
    archive, inner = value.split(">", 1)
    stem = Path(archive).stem if archive else ""
    if not stem or not inner:
        return ""
    return f"{stem}/{inner}".replace("/", "\\")


def _kirikiri_dump_target_closure_complete(game_dir: Path) -> bool:
    targets = _kirikiri_primary_dump_targets(game_dir)
    if not targets:
        return False
    return not _missing_kirikiri_active_dump_targets(game_dir, targets, active_only=False)


def _kirikiri_primary_dump_targets(game_dir: Path) -> list[str]:
    targets = _collect_kirikiri_dump_targets(game_dir, include_inferred=False)
    archive_targets = [target for target in targets if ">" in target]
    return archive_targets or targets


def _natural_kirikiri_target_sort_key(value: str):
    parts = re.split(r"(\d+)", str(value).replace("\\", "/").lower())
    key: list[tuple[int, object]] = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
            key.append((1, len(part)))
        else:
            key.append((2, part))
    return key


def _run_kirikiri_external_dump_probe(
    game_path: Path,
    engine,
    targets: list[str],
    *,
    timeout_seconds: int = 90,
) -> dict:
    from core.launcher import _is_pe_x86, _select_launch_exe
    from core import pipeline as _pipeline_mod
    try:
        from engines.kirikiri import import_kirikiri_external_dump
    except Exception as exc:
        return {"attempted": False, "reason": f"import_unavailable:{exc}"}

    game_dir = game_path if game_path.is_dir() else game_path.parent
    exe_path = _select_launch_exe(game_path, engine)
    if not exe_path:
        return {"attempted": False, "reason": "missing_exe"}
    if not _is_pe_x86(exe_path):
        return {"attempted": False, "reason": "non_x86_exe"}

    prepared = _prepare_kirikiri_krkrdump_runtime(game_dir)
    if not prepared.get("ok"):
        return {
            "attempted": False,
            "tool": "krkrdump",
            "reason": prepared.get("reason", "tool_unavailable"),
            "tool_path": prepared.get("tool_path", ""),
        }

    loader = Path(str(prepared["loader"]))
    output_dir = game_dir / "_translation_meta" / "kirikiri_external_dump" / "krkrdump"
    if not _safe_generated_child(output_dir, game_dir / "_translation_meta"):
        return {"attempted": False, "reason": "unsafe_output_dir"}
    try:
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"attempted": False, "reason": f"output_prepare_failed:{exc}"}

    config_path = loader.parent / "KrkrDump.json"
    _write_krkrdump_config(config_path, output_dir)
    _pipeline_mod._stop_processes_under_dir(game_dir)

    before = _count_kirikiri_dump_scripts(game_dir)
    raw_before = _count_files(output_dir)
    command = [str(loader), str(exe_path)]
    proc: subprocess.Popen | None = None
    started = time.monotonic()
    stable_since = started
    last_raw_count = raw_before
    try:
        proc = subprocess.Popen(command, cwd=str(game_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = started + max(15, timeout_seconds)
        while time.monotonic() < deadline:
            raw_count = _count_files(output_dir)
            if raw_count != last_raw_count:
                last_raw_count = raw_count
                stable_since = time.monotonic()
            if targets and raw_count >= len(targets):
                break
            if raw_count > raw_before and time.monotonic() - stable_since >= 10:
                break
            if proc.poll() is not None:
                break
            time.sleep(1)
    except Exception as exc:
        return {
            "attempted": True,
            "tool": "krkrdump",
            "launched": False,
            "reason": str(exc),
            "tool_path": str(loader),
        }
    finally:
        _pipeline_mod._stop_processes_under_dir(game_dir)

    imported = import_kirikiri_external_dump(game_dir, output_dir)
    after = _count_kirikiri_dump_scripts(game_dir)
    raw_after = _count_files(output_dir)
    return {
        "attempted": True,
        "tool": "krkrdump",
        "launched": proc is not None,
        "tool_path": str(loader),
        "config_path": str(config_path),
        "output_dir": str(output_dir),
        "target_count": len(targets),
        "before": before,
        "after": after,
        "added": max(0, after - before),
        "raw_dump_files": raw_after,
        "raw_added": max(0, raw_after - raw_before),
        "process_returncode": proc.poll() if proc is not None else None,
        **{k: v for k, v in imported.items() if k not in {"dump_dir"}},
    }


def _prepare_kirikiri_krkrdump_runtime(game_dir: Path) -> dict[str, object]:
    try:
        from core.tool_manager import ensure_tool
    except Exception as exc:
        return {"ok": False, "reason": f"tool_manager_unavailable:{exc}"}
    try:
        tool = ensure_tool("krkrdump")
    except Exception as exc:
        return {"ok": False, "reason": f"ensure_tool_failed:{exc}"}
    if not tool:
        return {"ok": False, "reason": "krkrdump_not_found"}

    loader_src = Path(tool)
    dll_src = loader_src.parent / "KrkrDump.dll"
    if not dll_src.exists():
        dll_src = next((p for p in loader_src.parent.rglob("KrkrDump.dll") if p.is_file()), None)
    if not dll_src or not dll_src.exists():
        return {"ok": False, "reason": "krkrdump_dll_not_found", "tool_path": str(loader_src)}

    runtime_dir = game_dir / "_translation_meta" / "kirikiri_tools" / "krkrdump"
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        loader_dst = runtime_dir / "KrkrDumpLoader.exe"
        dll_dst = runtime_dir / "KrkrDump.dll"
        shutil.copy2(loader_src, loader_dst)
        shutil.copy2(dll_src, dll_dst)
    except OSError as exc:
        return {"ok": False, "reason": f"deploy_failed:{exc}", "tool_path": str(loader_src)}
    return {"ok": True, "loader": str(loader_dst), "dll": str(dll_dst), "tool_path": str(loader_src)}


def _prepare_kirikiri_krkrpatch_runtime(game_dir: Path, exe_path: Path, patch_archives: list[str]) -> dict[str, object]:
    try:
        from core.tool_manager import ensure_tool
    except Exception as exc:
        return {"ok": False, "reason": f"tool_manager_unavailable:{exc}"}
    try:
        tool = ensure_tool("krkrpatch")
    except Exception as exc:
        return {"ok": False, "reason": f"ensure_tool_failed:{exc}"}
    if not tool:
        return {"ok": False, "reason": "krkrpatch_not_found"}

    loader_src = Path(tool)
    dll_src = loader_src.parent / "KrkrPatch.dll"
    if not dll_src.exists():
        dll_src = next((p for p in loader_src.parent.rglob("KrkrPatch.dll") if p.is_file()), None)
    if not dll_src or not dll_src.exists():
        return {"ok": False, "reason": "krkrpatch_dll_not_found", "tool_path": str(loader_src)}

    runtime_dir = game_dir
    try:
        loader_dst = runtime_dir / "KrkrPatchLoader.exe"
        dll_dst = runtime_dir / "KrkrPatch.dll"
        shutil.copy2(loader_src, loader_dst)
        shutil.copy2(dll_src, dll_dst)
        config_path = runtime_dir / "KrkrPatch.json"
        game_exe_rel = _kirikiri_rel_for_tool_config(exe_path, game_dir)
        config_path.write_text(
            json.dumps(
                {
                    "gameExecutableFile": game_exe_rel,
                    "gameCommandLine": "",
                    "logLevel": 1,
                    "patchProtocols": ["arc://", "archive://"],
                    "patchArchives": patch_archives,
                    "patchNoProtocol": True,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        return {"ok": False, "reason": f"deploy_failed:{exc}", "tool_path": str(loader_src)}
    return {
        "ok": True,
        "loader": str(loader_dst),
        "dll": str(dll_dst),
        "config": str(config_path),
        "deployed_to_game_root": True,
        "tool_path": str(loader_src),
    }


def _kirikiri_rel_for_tool_config(path: Path, base_dir: Path) -> str:
    try:
        rel = path.resolve().relative_to(base_dir.resolve())
        return rel.as_posix()
    except ValueError:
        pass
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _write_krkrdump_config(config_path: Path, output_dir: Path) -> None:
    payload = {
        "logLevel": 1,
        "truncateLog": True,
        "enableExtract": True,
        "outputDirectory": str(output_dir),
        "rules": [
            "file://\\./.+?\\.xp3>(.+)$",
            "file:///.+?\\.xp3>(.+)$",
            "archive://./(.+)",
            "arc://./(.+)",
            "bres://./(.+)",
        ],
        "includeExtensions": [],
        "excludeExtensions": [
            ".ogg", ".wav", ".mp3", ".m4a", ".opus",
            ".png", ".jpg", ".jpeg", ".webp", ".bmp",
            ".avi", ".mp4", ".wmv", ".mpg", ".mpeg",
            ".ttf", ".otf", ".fon",
        ],
        "decryptSimpleCrypt": True,
        "patchSbeam": True,
        "patchSignatureCheck": True,
        "dumpDir": True,
    }
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_generated_child(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
    except OSError:
        return False


def _count_files(path: Path) -> int:
    if not path.is_dir():
        return 0
    count = 0
    try:
        for candidate in path.rglob("*"):
            if candidate.is_file():
                count += 1
    except OSError:
        return count
    return count


def _run_kirikiri_native_dump_probe(
    game_path: Path,
    engine,
    targets: list[str],
    *,
    timeout_seconds: int = 60,
) -> dict:
    from core.launcher import _select_launch_exe
    from core import pipeline as _pipeline_mod

    game_dir = game_path if game_path.is_dir() else game_path.parent
    launcher = prepare_kirikiri_native_runtime(game_path)
    exe_path = _select_launch_exe(game_path, engine)
    if not launcher or not exe_path:
        return {"launched": False, "reason": "missing_launcher_or_exe"}

    _pipeline_mod._stop_processes_under_dir(game_dir)
    before = _count_kirikiri_dump_scripts(game_dir)
    command = [str(launcher), str(exe_path), "--wait"]
    active_target_count = sum(
        1 for target in targets
        if _kirikiri_is_active_dump_target(target) or _kirikiri_is_extensionless_dump_target(target)
    )
    extensionless_target_count = sum(1 for target in targets if _kirikiri_is_extensionless_dump_target(target))
    active_probe_marker = game_dir / "_translation_meta" / "kirikiri_active_extensionless_dump.txt"
    if extensionless_target_count:
        try:
            active_probe_marker.parent.mkdir(parents=True, exist_ok=True)
            active_probe_marker.write_text(
                "This file is created by the extraction pipeline to let the native KiriKiri "
                "dump probe actively enumerate extensionless XP3 script resources. It is "
                "removed after the probe and is not used for normal launches.\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    try:
        proc = subprocess.Popen(command, cwd=str(game_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        try:
            active_probe_marker.unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "launched": False,
            "reason": f"native_probe_launch_failed:{exc}",
            "target_count": len(targets),
            "extensionless_target_count": extensionless_target_count,
            "before": before,
            "after": before,
            "added": 0,
        }
    started = time.monotonic()
    stable_since = time.monotonic()
    last_count = before
    deadline = time.monotonic() + max(5, timeout_seconds)
    try:
        while time.monotonic() < deadline:
            current = _count_kirikiri_dump_scripts(game_dir)
            if current != last_count:
                last_count = current
                stable_since = time.monotonic()
            if active_target_count and current >= before + active_target_count:
                break
            if current > before and time.monotonic() - stable_since >= 12:
                break
            if not active_target_count and current == before and time.monotonic() - started >= 12:
                break
            time.sleep(1)
    finally:
        _pipeline_mod._stop_processes_under_dir(game_dir)
        try:
            active_probe_marker.unlink(missing_ok=True)
        except OSError:
            pass
    after = _count_kirikiri_dump_scripts(game_dir)
    return {
        "launched": True,
        "target_count": len(targets),
        "extensionless_target_count": extensionless_target_count,
        "before": before,
        "after": after,
        "added": max(0, after - before),
        "process_returncode": proc.poll(),
    }
