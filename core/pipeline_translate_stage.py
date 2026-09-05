"""translate 阶段：翻译覆盖率门槛、缓存尾巴跳过、API/缓存统计、
试用配额校验、检查点式 AI 翻译与质量校验重译。

模块级函数收 pipeline 作第一参数（Pipeline 类保留委托薄壳），
与 core/pipeline_stage_runtime.py 的拆分模式一致。
"""
from __future__ import annotations

from pathlib import Path

from config import get_config
from core.translation_cache_db import load_translations_from_cache
from core.translator_callbacks import translate_batch_with_callbacks
from translators.cache import get_cache_stats, reset_cache_stats
from utils.logger import info, warning


def _get_translator(name: str):
    from translators.factory import create_translator

    return create_translator(name)


def required_translation_coverage_ratio(pipeline) -> float:
    try:
        percent = int(getattr(get_config(), "minimum_translation_coverage", 80))
    except Exception:
        percent = 80
    percent = max(0, min(100, percent))
    return percent / 100.0

def has_required_translation_coverage(pipeline, items: list, min_ratio: float | None = None) -> bool:
    from core import pipeline as _pipeline_mod
    if min_ratio is None:
        min_ratio = pipeline._required_translation_coverage_ratio()
    if min_ratio <= 0:
        return True
    core_items = [item for item in items if _pipeline_mod._is_core_translation_item(item)]
    if not core_items:
        core_items = items
    if not core_items:
        return True
    translated = sum(1 for item in core_items if _pipeline_mod._has_effective_translation(item))
    if translated == 0:
        return False
    return translated / max(1, len(core_items)) >= min_ratio

def fail_insufficient_translation_coverage(pipeline, items: list) -> None:
    from core import pipeline as _pipeline_mod
    core_items = [item for item in items if _pipeline_mod._is_core_translation_item(item)] or list(items)
    translated = sum(1 for item in core_items if _pipeline_mod._has_effective_translation(item))
    total = len(core_items)
    message = (
        f"翻译覆盖不足：核心文本仅 {translated}/{total} 条。"
        "请检查 API Key 是否配置，或查看 DeepSeek 调用日志。"
    )
    warning(message)
    if hasattr(pipeline, "_fail_stage_code"):
        pipeline._fail_stage_code(
            "translate",
            "translation_coverage_low",
            detail=f"translated={translated}; total={total}; required={pipeline._required_translation_coverage_ratio():.3f}",
            rollback=True,
            next_actions=("检查翻译器、API Key 和网络后重试。",),
        )
        return
    if pipeline.diagnostics:
        min_ratio = pipeline._required_translation_coverage_ratio()
        pipeline.diagnostics.warn(
            "翻译覆盖不足，已停止回填，避免生成半成品汉化",
            translated=translated,
            total=total,
            min_ratio=min_ratio,
        )
        pipeline.diagnostics.finish(False)

def should_skip_cached_tail_translation(pipeline, items: list, cached_count: int) -> tuple[bool, int, float]:
    """Avoid spending minutes on a tiny already-high-coverage cache tail.

    This only applies to reruns/incremental runs where the game already has
    almost all translations cached. First-time translations still go through
    the normal AI path.
    """
    from core import pipeline as _pipeline_mod
    total = len(items)
    if total <= 0 or cached_count <= 0:
        return False, 0, 0.0
    remaining = sum(1 for item in items if not _pipeline_mod._has_effective_translation(item))
    if remaining <= 0:
        return False, 0, cached_count / max(total, 1)
    cached_ratio = cached_count / max(total, 1)
    if cached_ratio >= 0.98 and remaining <= 50:
        return True, remaining, cached_ratio
    return False, remaining, cached_ratio

def skip_cached_tail_translation(pipeline, items: list, cached_count: int) -> bool:
    from core import pipeline as _pipeline_mod
    should_skip, remaining, cached_ratio = pipeline._should_skip_cached_tail_translation(items, cached_count)
    if not should_skip:
        return False
    for item in items:
        if not _pipeline_mod._has_effective_translation(item) and not getattr(item, "translated", ""):
            item.translated = item.original
    percent = cached_ratio * 100
    info(
        f"缓存覆盖率 {percent:.2f}%，仅剩 {remaining} 条尾巴文本；"
        "跳过 AI 补尾并继续回填，避免少量难译文本拖住流程"
    )
    if pipeline.diagnostics:
        pipeline.diagnostics.set("tail_translation_skipped", {
            "cached_count": cached_count,
            "total": len(items),
            "remaining": remaining,
            "cached_ratio": round(cached_ratio, 6),
            "reason": "cached_coverage_ge_98_percent_and_remaining_le_50",
        })
    pipeline._update_item_progress(len(items) - remaining, len(items))
    return True

def reset_api_cache_stats(pipeline, translator_name: str):
    try:
        from translators.factory import translator_model, translator_prompt_version

        model = translator_model(translator_name, get_config())
        prompt_version = translator_prompt_version(translator_name)
    except Exception:
        model = "unknown"
        prompt_version = "legacy_v1"
    reset_cache_stats(
        provider=translator_name or "unknown",
        model=model,
        prompt_version=prompt_version,
        run_id=getattr(pipeline, "usage_run_id", ""),
    )

def record_api_cache_stats(pipeline, game_path: Path, total_texts: int):
    from core import pipeline as _pipeline_mod
    import json as _json

    stats = get_cache_stats()
    stats["total_texts"] = int(total_texts)
    if pipeline.diagnostics:
        pipeline.diagnostics.set("api_cache_stats", stats)
    try:
        out = _pipeline_mod._game_meta_path(game_path, "api_cache_stats.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        warning(f"API 缓存统计写入失败: {e}")
    info(
        "API 缓存统计: "
        f"总文本 {stats.get('total_texts', 0)}，"
        f"exact hit {stats.get('cache_exact_hit', 0)}，"
        f"miss {stats.get('cache_miss', 0)}，"
        f"API 请求 {stats.get('api_request_count', 0)}，"
        f"批请求 {stats.get('batch_request_count', 0)}，"
        f"批条目 {stats.get('batch_item_count', 0)}，"
        f"紧凑批 {stats.get('compact_batch_request_count', 0)}/{stats.get('compact_batch_item_count', 0)}，"
        f"上下文批 {stats.get('contextual_batch_request_count', 0)}/{stats.get('contextual_batch_item_count', 0)}，"
        f"生成批次 {stats.get('produced_batch_count', 0)}，"
        f"首批 {stats.get('first_batch_ready_seconds', 0)}s，"
        f"首请求 {stats.get('first_api_request_seconds', 0)}s，"
        f"单条兜底 {stats.get('single_fallback_count', 0)}，"
        f"解析失败 {stats.get('json_parse_fail_count', 0)}，"
        f"截断回收 {stats.get('partial_batch_recovered_count', 0)}/"
        f"{stats.get('partial_batch_recovered_item_count', 0)}，"
        f"去重省 {stats.get('dedupe_saved', 0)}，"
        f"估算费用 ¥{stats.get('estimated_cost_cny', 0)}，"
        f"估算节省 ¥{stats.get('estimated_saved_cost_cny', 0)}"
    )
async def translate_with_checkpoint(pipeline, items: list, checkpoint: Path,
                                    source_lang: str, target_lang: str,
                                    game_path: Path | None = None) -> list:
    """AI 翻译，完成后保存到 JSON 检查点。"""
    from core import pipeline as _pipeline_mod

    translator = _pipeline_mod._get_translator(get_config().active_translator)
    if not translator:
        warning("未配置翻译器，跳过翻译")
        return items

    # 只翻译还未翻译的条目（translated 为空 = 从未尝试）
    # 注意：translated == original 表示翻译结果与原文相同（标点/数字等），
    # 已缓存，无需重试
    untranslated = [it for it in items if not it.translated]
    already = len(items) - len(untranslated)
    if already > 0:
        info(f"已有 {already} 条翻译（来自缓存/JSON），跳过")

    if not untranslated:
        info("全部已翻译，跳过")
        pipeline._sync_checkpoint_json(checkpoint, items)
        return items

    # 从 SQLite 缓存尝试加载
    if game_path:
        cached = load_translations_from_cache(game_path, untranslated)
        # 重新计算未翻译条目
        untranslated = [it for it in untranslated if not it.translated]
        if not untranslated:
            info("SQLite 缓存全命中，跳过翻译")
            pipeline._sync_checkpoint_json(checkpoint, items)
            return items

    info(f"待翻译: {len(untranslated)}/{len(items)} 条")

    # 使用现有的稳定翻译器
    translated = await translate_batch_with_callbacks(
        translator,
        untranslated, source_lang, target_lang,
        on_progress=pipeline._update_item_progress,
        on_speed=lambda data: pipeline._meta("local_speed", data),
    )

    # 保存到 SQLite 缓存
    if game_path:
        _pipeline_mod.save_translations_to_cache(game_path, translated)

    # 合并结果：保持 items 顺序，更新 translation
    result = list(items)
    trans_map = {(it.file, it.key, it.original): it.translated
                 for it in translated if _pipeline_mod._has_effective_translation(it)}
    for it in result:
        t = trans_map.get((it.file, it.key, it.original))
        if t:
            it.translated = t

    pipeline._sync_checkpoint_json(checkpoint, result)
    return result


async def retry_invalid_translations(pipeline, items: list, checkpoint: Path,
                                     source_lang: str, target_lang: str,
                                     game_path: Path | None = None) -> list:
    """Retry items that were pushed back to untranslated by validation."""
    from core import pipeline as _pipeline_mod
    validation_failures = [
        it for it in items
        if not _pipeline_mod._has_effective_translation(it)
        and _pipeline_mod._contains_japanese_kana(it.original)
        and getattr(it, "meta", {}).get("validation_failed_translation")
    ]
    validation_failure_ids = {id(it) for it in validation_failures}
    untranslated_kana = [
        it for it in items
        if not _pipeline_mod._has_effective_translation(it)
        and _pipeline_mod._contains_japanese_kana(it.original)
        and id(it) not in validation_failure_ids
    ]
    effective_count = sum(1 for it in items if _pipeline_mod._has_effective_translation(it))
    coverage = effective_count / max(len(items), 1)
    retry_small_tail = coverage >= 0.95 and 0 < len(untranslated_kana) <= 200
    invalid = validation_failures + (untranslated_kana if retry_small_tail else [])
    if not invalid:
        return items

    translator = _pipeline_mod._get_translator(get_config().active_translator)
    if not translator:
        return items

    info(
        f"质量校验后重译: {len(invalid)} 条"
        f"（校验失败 {len(validation_failures)}，高覆盖尾部漏译 "
        f"{len(untranslated_kana) if retry_small_tail else 0}）"
    )
    if pipeline.diagnostics:
        pipeline.diagnostics.set("translation_tail_retry", {
            "requested": len(invalid),
            "validation_failures": len(validation_failures),
            "untranslated_kana_candidates": len(untranslated_kana),
            "small_tail_enabled": retry_small_tail,
            "coverage_before": round(coverage, 6),
        })
    for it in invalid:
        it.translated = ""

    retried = await translate_batch_with_callbacks(
        translator,
        invalid, source_lang, target_lang,
        on_progress=pipeline._update_item_progress,
        on_speed=lambda data: pipeline._meta("local_speed", data),
    )
    if game_path:
        _pipeline_mod.save_translations_to_cache(game_path, retried)

    trans_map = {
        (it.file, it.key, it.original): it.translated
        for it in retried
        if _pipeline_mod._has_effective_translation(it)
    }
    for it in items:
        t = trans_map.get((it.file, it.key, it.original))
        if t:
            it.translated = t

    items = pipeline._validate_all(items)
    pipeline._sync_checkpoint_json(checkpoint, items)
    if pipeline.diagnostics:
        remaining_kana = [
            it for it in items
            if not _pipeline_mod._has_effective_translation(it)
            and _pipeline_mod._contains_japanese_kana(it.original)
        ]
        pipeline.diagnostics.set("translation_tail_retry", {
            "requested": len(invalid),
            "validation_failures": len(validation_failures),
            "untranslated_kana_candidates": len(untranslated_kana),
            "small_tail_enabled": retry_small_tail,
            "coverage_before": round(coverage, 6),
            "remaining_untranslated_kana": len(remaining_kana),
            "remaining_samples": [
                {
                    "source": str(it.original)[:200],
                    "context": str(getattr(it, "context", "") or "")[:200],
                }
                for it in remaining_kana[:10]
            ],
        })
    return items
