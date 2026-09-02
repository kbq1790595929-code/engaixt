from __future__ import annotations

import asyncio
import json
import threading
import time

from config import get_config
from engines.base import TextItem
from translators.cache import get_cache
from translators.deepseek import DeepSeekTranslator, _Batch
from translators.deepseek_batch_json import BatchJsonParseError, BatchShapeError
from translators.hy_mt2_component import MODEL_NAME
from translators.hy_mt2_models import resolve_model_name
from translators.hy_mt2_quality import (
    finalize_local_translations,
    local_translation_quality_issue,
    repair_translation_structure,
)
from translators.hy_mt2_json import (
    parse_local_translation_map,
    parse_partial_local_translation_map,
)
from translators.hy_mt2_runtime import HyMt2Runtime, get_runtime
from utils.text_extract import (
    extract_placeholders,
    protect_placeholders,
    translation_source_for_item,
    validation_source_for_item,
)
from utils.logger import info, warning


class HyMt2Translator(DeepSeekTranslator):
    name = "hy_mt2"
    label = "腾讯 Hy-MT2（离线）"
    MODEL = MODEL_NAME
    MODEL_CONFIG_FIELD = "hy_mt2_model"
    PROMPT_VERSION = "hy_mt2_game_batch_v3"
    REALTIME_PROMPT_VERSION = "hy_mt2_realtime_context_v3"
    MAX_INPUT_TOKENS = 3000
    MAX_MESSAGE_BATCH = 8
    USES_TRIAL_QUOTA = False

    def __init__(self, runtime: HyMt2Runtime | None = None):
        self._runtime = runtime or get_runtime()
        self._speed_started_at = 0.0
        self._speed_last_logged_at = 0.0
        self._speed_generated_tokens = 0
        self._speed_inference_seconds = 0.0
        self._speed_last_instant = 0.0
        self._speed_callback = None
        self._speed_live_tokens = 0
        self._speed_live_started_at = 0.0
        self._speed_lock = threading.Lock()

    def _model(self, config=None) -> str:
        config = config or get_config()
        return resolve_model_name(getattr(config, self.MODEL_CONFIG_FIELD, ""))

    async def translate_realtime_text(
        self,
        text: str,
        source_lang: str = "ja",
        target_lang: str = "zh-CN",
        *,
        context: list[dict[str, str]] | None = None,
        speaker: str = "",
    ) -> str:
        source = str(text or "").strip()
        if not source:
            return ""
        item = TextItem(
            file="__kirikiri_live__.jsonl",
            key="realtime",
            original=source,
            translated="",
            context="message",
            meta={"kind": "message", "runtime_capture": True},
        )
        cache = get_cache()
        cached = cache.lookup(
            source,
            source_lang,
            target_lang,
            provider=self.name,
            model=self._model(),
            prompt_version=self.REALTIME_PROMPT_VERSION,
            text_type="message",
        ).translated
        valid_cached = self._valid_cached_translation_for_item(
            item, cached, source_lang, target_lang
        )
        if valid_cached:
            return valid_cached

        protected, _placeholders = protect_placeholders(source)
        direct_prompt = (
            "Translate the following Japanese game text into Simplified Chinese. "
            "Preserve names, punctuation, line breaks, and placeholders. "
            "Output only the translation, without explanation.\n" + protected
        )
        realtime_context = self._normalize_realtime_context(context or [])
        if not self._should_use_realtime_context(source):
            realtime_context = []
        if realtime_context:
            info(f"Hy-MT2 realtime context pairs={len(realtime_context)}")
        messages = self._build_realtime_messages(protected, realtime_context, speaker)
        max_tokens = max(64, min(512, len(source) * 4 + 32))
        completion, recorded_request = await self._complete_realtime(
            messages, direct_prompt, max_tokens
        )
        self._record_realtime_completion(cache, recorded_request, completion)
        candidate = self._validate_realtime_completion(
            item, completion.content, source_lang, target_lang, realtime_context
        )
        if not candidate and realtime_context:
            info("Hy-MT2 realtime context output invalid; retrying without context")
            completion = await asyncio.to_thread(self._runtime.complete, direct_prompt, max_tokens)
            self._record_realtime_completion(cache, direct_prompt, completion)
            candidate = self._validate_realtime_completion(
                item, completion.content, source_lang, target_lang, []
            )
        if not candidate:
            return ""
        cache.set_v2(
            source,
            candidate,
            source_lang,
            target_lang,
            provider=self.name,
            model=self._model(),
            prompt_version=self.REALTIME_PROMPT_VERSION,
            text_type="message",
        )
        return candidate

    async def _complete_realtime(
        self,
        messages: list[dict[str, str]],
        direct_prompt: str,
        max_tokens: int,
    ):
        complete_messages = getattr(self._runtime, "complete_messages", None)
        if len(messages) > 1 and callable(complete_messages):
            completion = await asyncio.to_thread(complete_messages, messages, max_tokens)
            return completion, messages
        completion = await asyncio.to_thread(self._runtime.complete, direct_prompt, max_tokens)
        return completion, direct_prompt

    def _record_realtime_completion(self, cache, request, completion) -> None:
        elapsed = float(completion.elapsed_seconds or 0.0)
        self._record_speed(completion.completion_tokens, elapsed)
        prompt = request if isinstance(request, str) else json.dumps(
            request, ensure_ascii=False, separators=(",", ":")
        )
        cache.record_api_call(
            prompt,
            completion.content,
            _Usage(completion.prompt_tokens, completion.completion_tokens),
            provider=self.name,
            model=self._model(),
        )

    def _validate_realtime_completion(
        self,
        item: TextItem,
        translated: str,
        source_lang: str,
        target_lang: str,
        context: list[dict[str, str]],
    ) -> str | None:
        repaired = repair_translation_structure(item.original, translated)
        normalized = repaired.strip()
        if any(
            normalized in {entry["source"].strip(), entry["translated"].strip()}
            for entry in context
        ):
            return None
        return self._validate_batch_translation(
            item,
            repaired,
            source_lang,
            target_lang,
        )

    @staticmethod
    def _normalize_realtime_context(context: list[dict[str, str]]) -> list[dict[str, str]]:
        normalized = []
        for raw in list(context)[-3:]:
            if not isinstance(raw, dict):
                continue
            source = str(raw.get("source") or "").strip()[:300]
            translated = str(raw.get("translated") or "").strip()[:300]
            speaker = str(raw.get("speaker") or "").strip()[:80]
            if not source or not translated or source == translated:
                continue
            normalized.append({
                "source": source,
                "translated": translated,
                "speaker": speaker,
            })
        while len(normalized) > 1 and sum(
            len(entry["source"]) + len(entry["translated"])
            for entry in normalized
        ) > 400:
            normalized.pop(0)
        return normalized

    @staticmethod
    def _build_realtime_messages(
        source: str,
        context: list[dict[str, str]],
        speaker: str,
    ) -> list[dict[str, str]]:
        if not context:
            return [{"role": "user", "content": source}]
        messages = [{
            "role": "system",
            "content": (
                "Translate only the latest Japanese user message into natural Simplified Chinese. "
                "Earlier user/assistant pairs are context only. Preserve names, tone, punctuation, "
                "line breaks, and placeholders. Prefer idiomatic conversational Chinese over "
                "word-for-word translation. Output only the translation."
            ),
        }]
        for entry in context:
            messages.append({"role": "user", "content": entry["source"]})
            messages.append({"role": "assistant", "content": entry["translated"]})
        # Hy-MT2 1.8B tends to echo explicit "Speaker:" metadata into the
        # translation. Keep it in the rolling state, but do not put it in the
        # text-bearing message until the model can consume it reliably.
        _ = speaker
        messages.append({"role": "user", "content": source})
        return messages

    @staticmethod
    def _should_use_realtime_context(source: str) -> bool:
        text = str(source or "").strip()
        if len(text) > 48:
            return False
        text = text.lstrip(" \t\u3000\u300c\u300e\uff08(")
        reply_markers = (
            "\u306f\u30fc\u3044", "\u306f\u3044", "\u3046\u3093", "\u3046\u3046\u3093", "\u3048\u3048", "\u305d\u3046",
            "\u308f\u304b\u3063\u305f", "\u5206\u304b\u3063\u305f", "\u4e86\u89e3", "\u306a\u308b\u307b\u3069", "\u3082\u3061\u308d\u3093",
            "\u304a\u9858\u3044", "\u3058\u3083\u3042", "\u3058\u3083", "\u305d\u308c\u3058\u3083", "\u3067\u3082", "\u3060\u304b\u3089",
            "\u3084\u3063\u3071\u308a", "\u3084\u3063\u3071", "\u307b\u3093\u3068", "\u672c\u5f53", "\u307e\u3055\u304b", "\u3044\u3084",
        )
        return any(text.startswith(marker) for marker in reply_markers)

    async def translate_batch(
        self,
        items: list[TextItem],
        source_lang: str,
        target_lang: str,
        on_progress=None,
        on_speed=None,
    ) -> list[TextItem]:
        self._speed_callback = on_speed
        heartbeat = asyncio.create_task(self._speed_heartbeat()) if on_speed else None
        try:
            return await self._translate_batch_impl(items, source_lang, target_lang, on_progress)
        finally:
            if heartbeat:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
            self._speed_callback = None

    async def _translate_batch_impl(
        self,
        items: list[TextItem],
        source_lang: str,
        target_lang: str,
        on_progress=None,
    ) -> list[TextItem]:
        if not items:
            return items
        config = get_config()
        self._speed_started_at = time.perf_counter()
        self._speed_last_logged_at = 0.0
        self._speed_generated_tokens = 0
        self._speed_inference_seconds = 0.0
        self._speed_last_instant = 0.0
        self._speed_live_tokens = 0
        self._speed_live_started_at = 0.0
        self.MAX_MESSAGE_BATCH = max(1, min(16, int(getattr(config, "hy_mt2_batch_size", 8) or 8)))
        self.MAX_BATCH_ITEMS = self.MAX_MESSAGE_BATCH
        started = time.perf_counter()
        cache = get_cache()
        pending, duplicates = self._collect_pending(items, source_lang, target_lang)
        cache.stats.dedupe_saved += duplicates
        completed = len(items) - sum(item.occurrences for item in pending)
        total = len(items)
        if on_progress:
            on_progress(completed, total)
        if not pending:
            self._finalize(items, source_lang, target_lang)
            info(f"Hy-MT2：全部 {len(items)} 条已翻译或命中缓存")
            return items

        batches = self._make_batches(pending, cache, source_lang, target_lang)
        cache.stats.produced_batch_count = len(batches)
        cache.stats.first_batch_ready_seconds = round(time.perf_counter() - started, 3)
        info(
            f"使用 {self.label} 翻译 {len(pending)} 条唯一文本"
            f"（{len(batches)} 批，最多 {self.MAX_MESSAGE_BATCH} 条/批，"
            f"合并 {duplicates} 条重复文本，费用 ¥0）"
        )
        info("Hy-MT2 本地调度：单实例串行；云端每批条数与云端并发数不参与本地推理")
        for index, batch in enumerate(batches, 1):
            await self._translate_local_batch(batch, source_lang, target_lang)
            completed += sum(item.occurrences for item in batch.items)
            if on_progress:
                on_progress(min(completed, total), total)
            if index == 1:
                info(f"Hy-MT2 首批翻译完成，后端={self._runtime.active_backend}")

        self._propagate_by_cache_key(items, pending, source_lang, target_lang)
        self._finalize(items, source_lang, target_lang)
        self._log_speed(final=True)
        info(f"Hy-MT2 翻译完成: {len(batches)} 批，耗时 {time.perf_counter() - started:.1f}s")
        return items

    def _make_batches(
        self,
        pending,
        cache,
        source_lang: str = "ja",
        target_lang: str = "zh-CN",
        *,
        concurrency: int | None = None,
    ) -> list:
        """本地模型按条数上限切批，避免大批次超出上下文被截断。

        DeepSeek 的 token 预算分批适合云端 API，但 Hy-MT2 本地运行时上下文
        有限（hy_mt2_context_size，默认 4096）。几百条一批时提示词+输出
        会超出上下文，llama.cpp 截断提示词后模型输出无法解析（JSON 解析
        失败→反复拆批重试全部失败）。这里在 token 预算基础上再按
        MAX_MESSAGE_BATCH（配置 hy_mt2_batch_size）切成小批。
        """
        batches = super()._make_batches(
            pending,
            cache,
            source_lang,
            target_lang,
            concurrency=concurrency,
        )
        limit = self.MAX_MESSAGE_BATCH
        if limit <= 0:
            return batches
        capped: list = []
        for batch in batches:
            items = batch.items
            for start in range(0, len(items), limit):
                capped.append(
                    _Batch(
                        list(items[start:start + limit]),
                        batch.text_type,
                        batch.complex,
                    )
                )
        return capped

    def _finalize(self, items: list[TextItem], source_lang: str, target_lang: str) -> None:
        before = {id(item): item.translated for item in items}
        structure_fixed, speaker_fixed, inconsistent_groups = finalize_local_translations(items)
        changed_items = [
            item
            for item in items
            if item.translated
            and item.translated != before.get(id(item), "")
            and self._should_store_translation(item, item.translated, source_lang)
        ]
        stored_count = 0
        if changed_items:
            cache = get_cache()
            stored_count = cache.set_many_v2(
                [
                    (
                        translation_source_for_item(item),
                        item.translated,
                        cache.infer_text_type(item),
                    )
                    for item in changed_items
                ],
                source_lang,
                target_lang,
                provider=self.name,
                model=self._model(),
                prompt_version=self.PROMPT_VERSION,
            )
        if structure_fixed or speaker_fixed:
            info(
                "Hy-MT2 确定性后处理: "
                f"结构修复 {structure_fixed} 条，译名统一 {speaker_fixed} 条/"
                f"{inconsistent_groups} 组，缓存更新 {stored_count} 条"
            )

    async def _translate_local_batch(
        self,
        batch: _Batch,
        source_lang: str,
        target_lang: str,
        *,
        depth: int = 0,
    ) -> None:
        if not batch.items:
            return
        cache = get_cache()
        prompt, expected_keys = self._build_local_prompt(
            batch, source_lang, target_lang, strict=depth > 0
        )
        max_tokens = self._batch_max_tokens(batch, cache)
        self._record_batch_request(batch, cache)
        try:
            request_started = time.perf_counter()
            self._begin_live_speed()
            completion = await asyncio.to_thread(
                self._complete_local_prompt,
                prompt,
                max_tokens,
            )
            request_elapsed = time.perf_counter() - request_started
            live_tokens = self._finish_live_speed()
            completion_tokens = int(completion.completion_tokens or live_tokens)
            self._record_speed(
                completion_tokens,
                completion.elapsed_seconds or request_elapsed,
            )
            cache.record_api_call(
                prompt,
                completion.content,
                _Usage(completion.prompt_tokens, completion_tokens),
                provider=self.name,
                model=self._model(),
            )
            if str(getattr(completion, "finish_reason", "") or "").lower() in {
                "length",
                "max_tokens",
            }:
                cache.stats.local_truncation_count += 1
                raise BatchJsonParseError(
                    "local response reached max_tokens; retry with a smaller batch"
                )
            mapping = self._parse_local_json(completion.content, expected_keys)
        except (BatchJsonParseError, BatchShapeError) as exc:
            cache.stats.json_parse_fail_count += 1
            partial = parse_partial_local_translation_map(completion.content, expected_keys)
            truncated = str(getattr(completion, "finish_reason", "") or "").lower() in {
                "length",
                "max_tokens",
            }
            if partial and not truncated:
                cache.stats.partial_batch_recovered_count += 1
                cache.stats.partial_batch_recovered_item_count += len(partial)
                invalid = self._apply_local_mapping(
                    batch.items,
                    partial,
                    source_lang,
                    target_lang,
                )
                warning(
                    f"Hy-MT2 本地 JSON 不完整，已安全回收 {len(partial)}/{len(batch.items)} 条，"
                    f"只重试剩余 {len(invalid)} 条"
                )
                if invalid:
                    cache.stats.batch_retry_count += 1
                    await self._translate_local_batch(
                        _Batch(invalid, batch.text_type, batch.complex),
                        source_lang,
                        target_lang,
                        depth=depth + 1,
                    )
                return
            if depth < 3:
                cache.stats.batch_split_count += 1
                if len(batch.items) > 1:
                    middle = len(batch.items) // 2
                    parts = (batch.items[:middle], batch.items[middle:])
                else:
                    parts = (batch.items,)
                for part in parts:
                    await self._translate_local_batch(
                        _Batch(list(part), batch.text_type, batch.complex),
                        source_lang,
                        target_lang,
                        depth=depth + 1,
                    )
                return
            warning(f"Hy-MT2 批量 JSON 解析失败: {exc}")
            for pending in batch.items:
                pending.item.translated = pending.item.original
            return

        invalid = self._apply_local_mapping(
            batch.items,
            mapping,
            source_lang,
            target_lang,
        )

        if invalid and depth < 2:
            if depth == 0:
                cache.stats.validation_fail_count += len(invalid)
            retry_size = min(4, self.MAX_MESSAGE_BATCH) if depth == 0 else 1
            for start in range(0, len(invalid), retry_size):
                retry_items = invalid[start:start + retry_size]
                if len(retry_items) == 1:
                    cache.stats.single_fallback_count += 1
                else:
                    cache.stats.batch_retry_count += 1
                await self._translate_local_batch(
                    _Batch(retry_items, batch.text_type, True),
                    source_lang,
                    target_lang,
                    depth=depth + 1,
                )
        else:
            for pending in invalid:
                pending.item.translated = pending.item.original

    def _complete_local_prompt(self, prompt: str, max_tokens: int):
        complete_with_progress = getattr(self._runtime, "complete_with_progress", None)
        if callable(complete_with_progress):
            return complete_with_progress(prompt, max_tokens, self._on_stream_chunk)
        return self._runtime.complete(prompt, max_tokens)

    def _begin_live_speed(self) -> None:
        with self._speed_lock:
            self._speed_live_tokens = 0
            self._speed_live_started_at = time.perf_counter()

    def _on_stream_chunk(self, count: int = 1) -> None:
        with self._speed_lock:
            self._speed_live_tokens += max(0, int(count or 0))

    def _finish_live_speed(self) -> int:
        with self._speed_lock:
            count = self._speed_live_tokens
            self._speed_live_tokens = 0
            self._speed_live_started_at = 0.0
            return count

    def _record_speed(self, generated_tokens: int, elapsed_seconds: float) -> None:
        generated = max(0, int(generated_tokens or 0))
        self._speed_generated_tokens += generated
        self._speed_inference_seconds += max(0.0, elapsed_seconds)
        now = time.perf_counter()
        if not self._speed_started_at:
            self._speed_started_at = now - max(0.0, elapsed_seconds)
        self._speed_last_instant = generated / max(0.001, elapsed_seconds)
        self._emit_speed(instant=self._speed_last_instant)
        if not self._speed_last_logged_at or now - self._speed_last_logged_at >= 2.0:
            self._log_speed(instant=self._speed_last_instant)
            self._speed_last_logged_at = now

    async def _speed_heartbeat(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self._emit_speed()

    def _emit_speed(self, *, instant: float | None = None, final: bool = False) -> None:
        callback = self._speed_callback
        if callback is None:
            return
        if not self._speed_started_at:
            return
        with self._speed_lock:
            live_tokens = self._speed_live_tokens
            live_started_at = self._speed_live_started_at
        live_elapsed = max(0.0, time.perf_counter() - live_started_at) if live_started_at else 0.0
        visible_tokens = self._speed_generated_tokens + live_tokens
        visible_seconds = self._speed_inference_seconds + live_elapsed
        average = visible_tokens / max(0.001, visible_seconds)
        live_current = live_tokens / max(0.001, live_elapsed) if live_started_at else 0.0
        current = live_current if live_started_at and live_tokens else self._speed_last_instant
        if instant is not None:
            current = float(instant)
        payload = {
            "provider": self.name,
            "model": self._model(),
            "current_tps": round(max(0.0, current), 1),
            "average_tps": round(max(0.0, average), 1),
            "generated_tokens": visible_tokens,
            "backend": str(getattr(self._runtime, "active_backend", "") or "unknown"),
            "streaming": bool(live_started_at),
            "final": bool(final),
        }
        try:
            callback(payload)
        except Exception:
            pass

    def _log_speed(self, *, instant: float | None = None, final: bool = False) -> None:
        if not self._speed_generated_tokens or not self._speed_started_at:
            return
        average = self._speed_generated_tokens / max(0.001, self._speed_inference_seconds)
        if final:
            self._emit_speed(instant=instant, final=True)
            info(
                f"Hy-MT2 平均速度: {average:.1f} tokens/s，"
                f"累计生成 {self._speed_generated_tokens} tokens"
            )
            return
        info(
            f"Hy-MT2 实时速度: {float(instant or 0.0):.1f} tokens/s，"
            f"累计平均 {average:.1f} tokens/s，"
            f"已生成 {self._speed_generated_tokens} tokens"
        )

    def _build_local_prompt(
        self,
        batch: _Batch,
        source_lang: str,
        target_lang: str,
        *,
        strict: bool = False,
    ) -> tuple[str, list[str]]:
        records: dict[str, str] = {}
        for index, pending in enumerate(batch.items, 1):
            source = translation_source_for_item(pending.item)
            protected, _placeholders = protect_placeholders(source)
            records[f"{index}:{pending.text_type}"] = protected
        keys = list(records)
        rules = [
            f"将下面JSON对象中的每个值从{source_lang}翻译为{target_lang}。",
            "键是不可修改的ID；输出必须保留完全相同的键集合和顺序。",
            "name只翻译名字，choice保持简短，ui/system保持简洁，message使用自然游戏口语。",
            "保留控制符、变量、占位符和原文换行语义。",
            "只输出翻译后的单个JSON对象，不要解释、Markdown、代码围栏或原文。",
        ]
        if strict:
            rules.insert(
                0,
                "上次输出格式不正确，请重新翻译。保持键与输入JSON完全一致（如 1:message），"
                "每条之间用逗号分隔，只输出合法的JSON对象。",
            )
        prompt = "\n".join(rules) + "\n输入JSON:\n" + json.dumps(
            records, ensure_ascii=False, separators=(",", ":")
        )
        return prompt, keys

    @staticmethod
    def _parse_local_json(raw: str, expected_keys: list[str]) -> dict[int, str]:
        return parse_local_translation_map(raw, expected_keys)

    def _apply_local_mapping(
        self,
        pending_items: list,
        mapping: dict[int, str],
        source_lang: str,
        target_lang: str,
    ) -> list:
        """Apply explicit response IDs and return only items needing retry."""
        invalid = []
        for row_id, pending in enumerate(pending_items, 1):
            raw_candidate = repair_translation_structure(
                pending.item.original,
                mapping.get(row_id, ""),
            )
            candidate = self._validate_batch_translation(
                pending.item,
                raw_candidate,
                source_lang,
                target_lang,
            )
            if candidate:
                self._store_success(pending.item, candidate, source_lang, target_lang)
            else:
                invalid.append(pending)
        return invalid

    def _validate_batch_translation(
        self,
        item: TextItem,
        translated: str,
        source_lang: str = "",
        target_lang: str = "",
    ) -> str | None:
        """Add strict local-only control and collapse checks after shared validation."""
        candidate = super()._validate_batch_translation(
            item,
            translated,
            source_lang,
            target_lang,
        )
        if candidate is None:
            return None

        source = validation_source_for_item(item)
        expected_controls = extract_placeholders(source)
        actual_controls = extract_placeholders(candidate)
        if actual_controls != expected_controls:
            get_cache().stats.local_control_fail_count += 1
            warning(
                f"Hy-MT2 本地译文控制符不完整或顺序改变，拒绝写入缓存："
                f"期望 {len(expected_controls)} 个，实际 {len(actual_controls)} 个"
            )
            return None

        issue = local_translation_quality_issue(
            item,
            candidate,
            source_lang=source_lang,
            target_lang=target_lang,
        )
        if issue:
            get_cache().stats.local_quality_fail_count += 1
            warning(f"Hy-MT2 本地译文质量门禁：{issue}，拒绝写入缓存")
            return None
        return candidate

    @staticmethod
    def _restore_line_dialogue_wrappers(source: str, translated: str) -> str:
        return repair_translation_structure(source, translated)


class _Usage:
    def __init__(self, prompt_tokens: int, completion_tokens: int):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
