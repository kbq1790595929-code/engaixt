from __future__ import annotations

import asyncio
import json
import time

from config import get_config
from engines.base import TextItem
from translators.cache import get_cache
from translators.deepseek import DeepSeekTranslator, _Batch
from translators.deepseek_batch_json import BatchJsonParseError, BatchShapeError
from translators.hy_mt2_component import MODEL_NAME
from translators.hy_mt2_quality import (
    finalize_local_translations,
    repair_translation_structure,
)
from translators.hy_mt2_runtime import HyMt2Runtime, get_runtime
from utils.text_extract import protect_placeholders, translation_source_for_item
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
    ) -> list[TextItem]:
        if not items:
            return items
        config = get_config()
        self._speed_started_at = time.perf_counter()
        self._speed_last_logged_at = 0.0
        self._speed_generated_tokens = 0
        self._speed_inference_seconds = 0.0
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
            completion = await asyncio.to_thread(self._runtime.complete, prompt, max_tokens)
            request_elapsed = time.perf_counter() - request_started
            self._record_speed(
                completion.completion_tokens,
                completion.elapsed_seconds or request_elapsed,
            )
            cache.record_api_call(
                prompt,
                completion.content,
                _Usage(completion.prompt_tokens, completion.completion_tokens),
                provider=self.name,
                model=self._model(),
            )
            mapping = self._parse_local_json(completion.content, expected_keys)
        except (BatchJsonParseError, BatchShapeError) as exc:
            cache.stats.json_parse_fail_count += 1
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

        invalid = []
        for row_id, pending in enumerate(batch.items, 1):
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

    def _record_speed(self, generated_tokens: int, elapsed_seconds: float) -> None:
        generated = max(0, int(generated_tokens or 0))
        self._speed_generated_tokens += generated
        self._speed_inference_seconds += max(0.0, elapsed_seconds)
        now = time.perf_counter()
        if not self._speed_started_at:
            self._speed_started_at = now - max(0.0, elapsed_seconds)
        if not self._speed_last_logged_at or now - self._speed_last_logged_at >= 2.0:
            instant = generated / max(0.001, elapsed_seconds)
            self._log_speed(instant=instant)
            self._speed_last_logged_at = now

    def _log_speed(self, *, instant: float | None = None, final: bool = False) -> None:
        if not self._speed_generated_tokens or not self._speed_started_at:
            return
        average = self._speed_generated_tokens / max(0.001, self._speed_inference_seconds)
        if final:
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
            rules.insert(0, "上次输出格式不正确。严格只返回可被json.loads解析的JSON对象。")
        prompt = "\n".join(rules) + "\n输入JSON:\n" + json.dumps(
            records, ensure_ascii=False, separators=(",", ":")
        )
        return prompt, keys

    @staticmethod
    def _parse_local_json(raw: str, expected_keys: list[str]) -> dict[int, str]:
        text = str(raw or "").strip()
        if text.startswith("```"):
            raise BatchJsonParseError("response contains markdown fence")
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise BatchJsonParseError("response does not contain json object")
        try:
            payload = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise BatchJsonParseError(f"invalid json: {exc}") from exc
        if not isinstance(payload, dict) or list(payload) != expected_keys:
            raise BatchShapeError("id set mismatch")
        result: dict[int, str] = {}
        for key, value in payload.items():
            if not isinstance(value, str):
                raise BatchShapeError("translation is not string")
            result[int(key.split(":", 1)[0])] = value.strip()
        return result

    @staticmethod
    def _restore_line_dialogue_wrappers(source: str, translated: str) -> str:
        return repair_translation_structure(source, translated)


class _Usage:
    def __init__(self, prompt_tokens: int, completion_tokens: int):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
