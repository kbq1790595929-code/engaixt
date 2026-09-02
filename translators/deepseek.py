from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Iterator

from config import get_config
from engines.base import TextItem
from translators.base import TranslatorBase, retry_with_backoff
from translators.cache import CacheLookupRequest, get_cache
from translators.deepseek_runtime_mixin import DeepSeekRuntimeMixin
from translators.deepseek_batch_json import (
    BatchJsonParseError as _BatchJsonParseError,
    BatchShapeError as _BatchShapeError,
    parse_batch_json,
    parse_partial_translation_map,
)
from utils.logger import info, warning
from utils.text_extract import (
    contains_kana,
    is_acceptable_same_as_source,
    is_bgi_ruby_text,
    protect_placeholders,
    restore_placeholders,
    translation_source_for_item,
    validation_source_for_item,
    verify_translation,
)


_CONTROL_RE = re.compile(
    r"(\\[A-Za-z]+\[[^\]]+\]|%[sdif]|{{PH\d+}}|\{[^{}]+\}|\$[A-Za-z_]\w*|<[^>]+>)"
)


class _FatalApiError(RuntimeError):
    pass


class _RetryableApiError(RuntimeError):
    pass


@dataclass
class _PendingItem:
    item: TextItem
    cache_key: str
    text_type: str
    prompt: str
    occurrences: int = 1


@dataclass
class _Batch:
    items: list[_PendingItem]
    text_type: str
    complex: bool


class DeepSeekTranslator(DeepSeekRuntimeMixin, TranslatorBase):
    name = "deepseek"
    label = "DeepSeek (兼容 OpenAI)"

    _BASE_URL = "https://api.deepseek.com/v1"
    MODEL = "deepseek-v4-flash"
    API_KEY_FIELDS = ("deepseek_api_key", "openai_api_key")
    MODEL_CONFIG_FIELD = "deepseek_model"
    EXTRA_BODY = {"thinking": {"type": "disabled"}}
    PROMPT_VERSION = "game_ja_zh_batch_map_v3"

    # 批次预算基于 deepseek-v4-flash 服务器端实际能力：
    # 上下文 1M tokens、输出上限 384K（max_tokens 可配 1..393216）。
    # 输入预算取服务器上下文的保守比例；输出预算决定每批能装多少内容。
    # 实测发现纯靠 token 预算时短文本批次会撑到 300+ 条，超过模型单次
    # 稳定输出完整 JSON 的可靠上限（id set mismatch → 二分降级 → 单条兜底
    # 风暴，请求数 20→835、成本翻倍）。因此加一条 MAX_BATCH_ITEMS 兜底：
    # 条数由内容量动态决定，但不超过可靠输出边界，token 预算继续约束。
    MAX_INPUT_TOKENS = 32768
    MAX_OUTPUT_TOKENS = 16384
    MAX_BATCH_ITEMS = 150
    # 验证失败（假名残留/控制符破坏等）条目的聚合重试深度上限。
    # 失败条目会重新聚合批处理，而不是逐条单发（单发有固定请求开销，
    # 全量重翻 3123 次单条兜底直接拖慢整体速度）。超过深度上限的
    # 顽固失败条目才降级单条，保证收敛不递归爆炸。
    MAX_FAILURE_RETRY_DEPTH = 3
    MAX_CONCURRENCY = 2500
    REQUEST_TIMEOUT_SECONDS = 45
    RETRY_MAX_RETRIES = 3
    RETRY_BASE_DELAY_SECONDS = 2.0

    async def translate_batch(
        self,
        items: list[TextItem],
        source_lang: str,
        target_lang: str,
        on_progress=None,
    ) -> list[TextItem]:
        if not items:
            return items

        self._translation_started_at = time.perf_counter()
        self._first_api_request_recorded = False
        config = get_config()
        cache = get_cache()
        pending, pending_duplicates = self._collect_pending(items, source_lang, target_lang)
        cache.stats.dedupe_saved += pending_duplicates

        if not pending:
            info(f"全部 {len(items)} 条已翻译或命中缓存，跳过")
            if on_progress:
                on_progress(len(items), len(items))
            return items

        api_key = self._api_key(config)
        if not api_key:
            warning(f"{self.label} API Key 未配置，跳过 AI 翻译")
            for item in items:
                if not item.translated:
                    item.translated = item.original
            self._propagate_by_cache_key(items, pending, source_lang, target_lang)
            return items

        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, base_url=self._BASE_URL)
        concurrency = self._configured_concurrency(config)
        worker_count = min(concurrency, max(1, len(pending)))
        abort_event = asyncio.Event()
        completed = len(items) - sum(p.occurrences for p in pending)
        total = len(items)
        produced_batches = 0
        info(
            f"使用 {self.label} 流式结构化批处理翻译 {len(pending)} 条唯一文本"
            f"（按内容量自然分批，输入≤{self.MAX_INPUT_TOKENS}/输出≤{self.MAX_OUTPUT_TOKENS} tokens，"
            f"≤{self.MAX_BATCH_ITEMS} 条/批，{worker_count} 并发，合并 {pending_duplicates} 条重复文本）"
        )
        info(f"DeepSeek 批量配置: 顺序组批，由输入/输出 token 预算 + 条数上限决定批次大小")
        if on_progress:
            try:
                on_progress(completed, total)
            except Exception:
                pass

        queue: asyncio.Queue[_Batch | None] = asyncio.Queue(maxsize=max(2, worker_count * 2))
        results: list[list[TextItem] | BaseException] = []

        async def produce_batches() -> None:
            nonlocal produced_batches
            try:
                for batch in self._iter_batches(pending, cache, source_lang, target_lang):
                    if abort_event.is_set():
                        break
                    produced_batches += 1
                    if produced_batches == 1:
                        elapsed = round(time.perf_counter() - self._translation_started_at, 3)
                        cache.stats.first_batch_ready_seconds = elapsed
                        info(f"DeepSeek 首批已就绪: {elapsed:.3f}s，开始发送 API 请求")
                    await queue.put(batch)
                    if produced_batches == 1:
                        await asyncio.sleep(0)
            finally:
                cache.stats.produced_batch_count = produced_batches
                for _ in range(worker_count):
                    await queue.put(None)

        def mark_batch_done(batch: _Batch):
            nonlocal completed
            completed += sum(item.occurrences for item in batch.items)
            if on_progress:
                try:
                    on_progress(min(completed, total), total)
                except Exception:
                    pass

        async def worker(_worker_id: int):
            while True:
                batch = await queue.get()
                if batch is None:
                    queue.task_done()
                    return
                if abort_event.is_set():
                    for p in batch.items:
                        if not p.item.translated:
                            p.item.translated = p.item.original
                    mark_batch_done(batch)
                    queue.task_done()
                    continue
                try:
                    translated = await self._translate_structured_batch(
                        batch,
                        client,
                        source_lang,
                        target_lang,
                    )
                except _FatalApiError:
                    abort_event.set()
                    for p in batch.items:
                        if not p.item.translated:
                            p.item.translated = p.item.original
                    raise
                except Exception as e:
                    results.append(e)
                else:
                    results.append(translated)
                finally:
                    mark_batch_done(batch)
                    queue.task_done()

        producer_task = asyncio.create_task(produce_batches())
        worker_results = await asyncio.gather(
            *(worker(i) for i in range(worker_count)),
            return_exceptions=True,
        )
        await producer_task
        results.extend(r for r in worker_results if isinstance(r, BaseException))
        info(f"DeepSeek 流式分批完成: {produced_batches} 批")
        for result in results:
            if isinstance(result, BaseException):
                warning(f"批处理任务异常: {result}")
        if abort_event.is_set():
            for p in pending:
                if not p.item.translated:
                    p.item.translated = p.item.original

        self._propagate_by_cache_key(items, pending, source_lang, target_lang)
        return items

    async def translate_group(
        self,
        items: list[TextItem],
        source_lang: str,
        target_lang: str,
    ) -> list[TextItem]:
        return await self.translate_batch(items, source_lang, target_lang)

    def _collect_pending(
        self,
        items: list[TextItem],
        source_lang: str,
        target_lang: str,
    ) -> tuple[list[_PendingItem], int]:
        cache = get_cache()
        pending: list[_PendingItem] = []
        pending_by_key: dict[str, _PendingItem] = {}
        duplicates = 0
        candidates: list[tuple[TextItem, str, str]] = []

        for item in items:
            if self.should_cache_translation(item.original, item.translated):
                continue
            text_type = cache.infer_text_type(item)
            source_text = translation_source_for_item(item)
            candidates.append((item, text_type, source_text))

        lookups = cache.lookup_many(
            [
                CacheLookupRequest(
                    text=source_text,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    provider=self.name,
                    model=self._model(),
                    prompt_version=self.PROMPT_VERSION,
                    text_type=self._cache_text_type(_item, text_type),
                )
                for _item, text_type, source_text in candidates
            ]
        )

        for (item, text_type, source_text), lookup in zip(candidates, lookups):
            safe_cached = self._valid_cached_translation_for_item(
                item, lookup.translated, source_lang, target_lang
            )
            if safe_cached:
                item.translated = safe_cached
                cache.record_saved_request(source_text, safe_cached)
                continue
            if lookup.key in pending_by_key:
                duplicates += 1
                pending_by_key[lookup.key].occurrences += 1
                continue
            p = _PendingItem(item=item, cache_key=lookup.key, text_type=text_type, prompt="")
            pending_by_key[lookup.key] = p
            pending.append(p)

        return pending, duplicates

    def _make_batches(
        self,
        pending: list[_PendingItem],
        cache,
        source_lang: str = "ja",
        target_lang: str = "zh-CN",
        *,
        concurrency: int | None = None,
    ) -> list[_Batch]:
        return list(self._iter_batches(pending, cache, source_lang, target_lang))

    def _iter_batches(
        self,
        pending: list[_PendingItem],
        cache,
        source_lang: str = "ja",
        target_lang: str = "zh-CN",
    ) -> Iterator[_Batch]:
        """Yield API batches in source order with one O(n) pass.

        批次大小由内容量动态决定（短文本一批装得多、长文本一批装得少），
        受输入 token（MAX_INPUT_TOKENS）、预估输出 token（MAX_OUTPUT_TOKENS）
        与条数上限（MAX_BATCH_ITEMS）三道约束。条数上限保证批次不超过模型
        单次稳定输出完整 JSON 的可靠边界，避免 id 缺失→二分降级→单条兜底
        风暴。
        """
        del source_lang, target_lang
        current: list[_PendingItem] = []
        estimated_input_tokens = 256
        # 增量维护输出预算：与 _estimate_output_tokens 公式同口径，
        # O(1) 更新，保证切分精确、前 N-1 批大小稳定一致。
        output_base = 0
        output_count = 0

        for pending_item in pending:
            item_tokens = self._estimated_batch_item_tokens(pending_item, cache)
            source_tokens = cache.estimate_tokens(translation_source_for_item(pending_item.item))
            if current and (
                len(current) >= self.MAX_BATCH_ITEMS
                or estimated_input_tokens + item_tokens > self.MAX_INPUT_TOKENS
                or int((output_base + source_tokens) * 1.7 + (output_count + 1) * 12 + 192)
                > self.MAX_OUTPUT_TOKENS
            ):
                yield self._new_primary_batch(current)
                current = []
                estimated_input_tokens = 256
                output_base = 0
                output_count = 0
            current.append(pending_item)
            estimated_input_tokens += item_tokens
            output_base += source_tokens
            output_count += 1

        if current:
            yield self._new_primary_batch(current)

    def _estimate_output_tokens(self, items: list[_PendingItem], cache) -> int:
        """预估翻译输出的 token 量，与 _batch_max_tokens 的计算保持一致。

        用同样的基数（1.7×输入 + 条数×12 + 192）估算，保证每批请求的
        max_tokens 足够容纳译文，不会在模型侧被截断。
        """
        if not items:
            return 0
        base = sum(cache.estimate_tokens(translation_source_for_item(p.item)) for p in items)
        return int(base * 1.7 + len(items) * 12 + 192)

    def _estimated_batch_item_tokens(self, pending: _PendingItem, cache) -> int:
        item = pending.item
        meta = getattr(item, "meta", {}) or {}
        source = translation_source_for_item(item)
        context = ""
        if pending.text_type in {"choice", "ui/system"}:
            context = str(meta.get("prev_text") or "") + str(meta.get("next_text") or "")
        return cache.estimate_tokens(source) + cache.estimate_tokens(context) + 16

    @staticmethod
    def _new_primary_batch(items: list[_PendingItem]) -> _Batch:
        text_types = {item.text_type for item in items}
        text_type = next(iter(text_types)) if len(text_types) == 1 else "mixed"
        return _Batch(list(items), text_type=text_type, complex=False)

    def _is_complex_item(self, item: TextItem, cache) -> bool:
        return self._complexity_risk_score(item, cache) > 0

    def _complexity_risk_score(self, item: TextItem, cache) -> int:
        text = translation_source_for_item(item)
        score = 0
        tokens = cache.estimate_tokens(text)
        if tokens > 240:
            score += 5
        elif tokens > 160:
            score += 4
        elif tokens > 80:
            score += 2
        if "\n" in text or "\r" in text:
            score += 3
        _protected, placeholders = protect_placeholders(text)
        if placeholders:
            score += min(2, len(placeholders))
        elif _CONTROL_RE.search(text):
            score += 2
        return score

    async def _translate_structured_batch(
        self,
        batch: _Batch,
        client: Any,
        source_lang: str,
        target_lang: str,
        *,
        depth: int = 0,
    ) -> list[TextItem]:
        cache = get_cache()
        if not batch.items:
            return []
        if len(batch.items) == 1 and depth > 0:
            cache.stats.single_fallback_count += 1
            return await self._translate_single_items([batch.items[0]], client, source_lang, target_lang)

        prompt = self._build_batch_prompt(batch, source_lang, target_lang)
        max_tokens = self._batch_max_tokens(batch, cache)
        self._record_batch_request(batch, cache)

        try:
            result = await self._call_chat_json(client, prompt, max_tokens)
            parsed = self._parse_batch_json(result, [i + 1 for i in range(len(batch.items))])
        except _FatalApiError:
            raise
        except _RetryableApiError as e:
            warning(f"API 临时失败，跳过当前批次等待下次断点/缓存续跑: {e}")
            for p in batch.items:
                if not p.item.translated:
                    p.item.translated = p.item.original
            return [p.item for p in batch.items]
        except _BatchJsonParseError as e:
            cache.stats.json_parse_fail_count += 1
            partial = parse_partial_translation_map(result, [i + 1 for i in range(len(batch.items))])
            if partial:
                cache.stats.partial_batch_recovered_count += 1
                cache.stats.partial_batch_recovered_item_count += len(partial)
                warning(
                    f"批处理 JSON 尾部不完整，已回收 {len(partial)}/{len(batch.items)} 条，"
                    "只重试剩余条目"
                )
                return await self._recover_partial_batch(
                    batch, partial, client, source_lang, target_lang, depth
                )
            warning(f"批处理 JSON 解析失败且无可回收条目，尝试严格重试: {e}")
            try:
                cache.stats.batch_retry_count += 1
                self._record_batch_request(batch, cache)
                retry_prompt = self._build_batch_prompt(batch, source_lang, target_lang, strict=True)
                result = await self._call_chat_json(client, retry_prompt, max_tokens)
                parsed = self._parse_batch_json(result, [i + 1 for i in range(len(batch.items))])
            except _FatalApiError:
                raise
            except Exception as retry_error:
                return await self._split_or_single(batch, client, source_lang, target_lang, depth, retry_error)
        except _BatchShapeError as e:
            # id set mismatch / 形状异常：先从响应里尽力回收完整条目，只对
            # 缺失条目发补齐请求，避免整批二分降级触发单条兜底风暴。
            partial = parse_partial_translation_map(
                result, [i + 1 for i in range(len(batch.items))]
            )
            if partial:
                cache.stats.partial_batch_recovered_count += 1
                cache.stats.partial_batch_recovered_item_count += len(partial)
                warning(
                    f"批处理 id 集合不完整，已回收 {len(partial)}/{len(batch.items)} 条，"
                    "只补齐缺失条目"
                )
                return await self._recover_partial_batch(
                    batch, partial, client, source_lang, target_lang, depth
                )
            return await self._split_or_single(batch, client, source_lang, target_lang, depth, e)
        except Exception as e:
            return await self._split_or_single(batch, client, source_lang, target_lang, depth, e)

        failures: list[_PendingItem] = []
        translated_items: list[TextItem] = []
        for idx, pending in enumerate(batch.items, 1):
            raw = parsed.get(idx, "")
            safe = self._validate_batch_translation(pending.item, raw, source_lang, target_lang)
            if safe is None:
                if self._is_same_as_source_refusal(pending.item, raw, source_lang):
                    pending.item.translated = pending.item.original
                    translated_items.append(pending.item)
                    continue
                cache.stats.validation_fail_count += 1
                failures.append(pending)
                continue
            self._store_success(pending.item, safe, source_lang, target_lang)
            translated_items.append(pending.item)

        if failures:
            if depth < self.MAX_FAILURE_RETRY_DEPTH and len(failures) >= 2:
                # 失败条目重新聚合批处理重试：一次请求覆盖一批，而非逐条单发
                # （单发有固定 prompt/往返开销，批量失败时数百条会拖慢整体速度）。
                # depth 上限防止顽固失败条目无限递归。
                for retry_batch in self._iter_batches(
                    failures, cache, source_lang, target_lang
                ):
                    translated_items.extend(await self._translate_structured_batch(
                        retry_batch,
                        client,
                        source_lang,
                        target_lang,
                        depth=depth + 1,
                    ))
            else:
                cache.stats.single_fallback_count += len(failures)
                translated_items.extend(await self._translate_single_items(
                    failures,
                    client,
                    source_lang,
                    target_lang,
                    count_validation_fail=False,
                ))
        return translated_items

    async def _recover_partial_batch(
        self,
        batch: _Batch,
        parsed: dict[int, str],
        client: Any,
        source_lang: str,
        target_lang: str,
        depth: int,
    ) -> list[TextItem]:
        cache = get_cache()
        translated_items: list[TextItem] = []
        remaining: list[_PendingItem] = []
        for idx, pending in enumerate(batch.items, 1):
            raw = parsed.get(idx)
            if raw is None:
                remaining.append(pending)
                continue
            safe = self._validate_batch_translation(pending.item, raw, source_lang, target_lang)
            if safe is None:
                cache.stats.validation_fail_count += 1
                remaining.append(pending)
                continue
            self._store_success(pending.item, safe, source_lang, target_lang)
            translated_items.append(pending.item)

        if remaining:
            retry_batch = _Batch(remaining, batch.text_type, batch.complex)
            translated_items.extend(await self._translate_structured_batch(
                retry_batch,
                client,
                source_lang,
                target_lang,
                depth=depth + 1,
            ))
        return translated_items

    async def _split_or_single(
        self,
        batch: _Batch,
        client: Any,
        source_lang: str,
        target_lang: str,
        depth: int,
        error: Exception,
    ) -> list[TextItem]:
        cache = get_cache()
        warning(f"批处理失败，准备降级: {error}")
        if len(batch.items) <= 1:
            cache.stats.single_fallback_count += len(batch.items)
            return await self._translate_single_items(batch.items, client, source_lang, target_lang)
        mid = len(batch.items) // 2
        cache.stats.batch_split_count += 1
        left = _Batch(batch.items[:mid], batch.text_type, batch.complex)
        right = _Batch(batch.items[mid:], batch.text_type, batch.complex)
        translated = await self._translate_structured_batch(left, client, source_lang, target_lang, depth=depth + 1)
        translated.extend(await self._translate_structured_batch(right, client, source_lang, target_lang, depth=depth + 1))
        return translated

    async def _call_chat(self, client: Any, prompt: str, max_tokens: int) -> str:
        return await self._call_chat_raw(client, prompt, max_tokens, json_mode=False)

    async def _call_chat_json(self, client: Any, prompt: str, max_tokens: int) -> str:
        return await self._call_chat_raw(client, prompt, max_tokens, json_mode=True)

    async def _call_chat_raw(self, client: Any, prompt: str, max_tokens: int, *, json_mode: bool) -> str:
        cache = get_cache()

        async def call():
            kwargs = {
                "model": self._model(),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": max_tokens,
                "timeout": self.REQUEST_TIMEOUT_SECONDS,
            }
            extra_body = self._chat_extra_body()
            if extra_body:
                kwargs["extra_body"] = extra_body
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            self._record_first_api_request(cache)
            try:
                resp = await client.chat.completions.create(**kwargs)
            except Exception as e:
                if json_mode and self._should_retry_without_response_format(e):
                    kwargs.pop("response_format", None)
                    resp = await client.chat.completions.create(**kwargs)
                elif self._is_fatal_api_error(e):
                    raise _FatalApiError(self._summarize_fatal_api_error(e)) from e
                else:
                    raise
            content = resp.choices[0].message.content.strip()
            cache.record_api_call(
                prompt,
                content,
                getattr(resp, "usage", None),
                provider=self.name,
                model=self._model(),
            )
            return content

        try:
            return await retry_with_backoff(
                call,
                max_retries=self.RETRY_MAX_RETRIES,
                base_delay=self.RETRY_BASE_DELAY_SECONDS,
            )
        except _FatalApiError:
            raise
        except Exception as e:
            if self._is_retryable_api_error(e):
                raise _RetryableApiError(str(e)) from e
            raise

    def _record_first_api_request(self, cache) -> None:
        if getattr(self, "_first_api_request_recorded", False) or not hasattr(self, "_translation_started_at"):
            return
        self._first_api_request_recorded = True
        elapsed = round(time.perf_counter() - self._translation_started_at, 3)
        cache.stats.first_api_request_seconds = elapsed
        info(f"DeepSeek 首个 API 请求已发送: {elapsed:.3f}s")

    def _is_fatal_api_error(self, error: Exception) -> bool:
        msg = str(error).lower()
        return any(token in msg for token in (
            "401",
            "unauthorized",
            "authentication",
            "invalid api key",
            "api key is invalid",
            "invalid_request_error",
            "insufficient_balance",
            "insufficient balance",
            "billing",
        ))

    def _should_retry_without_response_format(self, error: Exception) -> bool:
        msg = str(error).lower()
        return "response_format" in msg or "json_object" in msg

    def _is_retryable_api_error(self, error: Exception) -> bool:
        msg = str(error).lower()
        return any(token in msg for token in (
            "429",
            "rate",
            "limit",
            "too many requests",
            "timeout",
            "timed out",
            "overloaded",
            "503",
            "service unavailable",
            "connection",
            "temporarily",
        ))

    def _summarize_fatal_api_error(self, error: Exception) -> str:
        msg = str(error)
        if "401" in msg or "authentication" in msg.lower() or "invalid api key" in msg.lower():
            return f"{self.label} API 鉴权失败，请检查 key 是否有效"
        if "balance" in msg.lower() or "billing" in msg.lower():
            return f"{self.label} API 余额或计费不可用，请检查账户"
        return f"{self.label} API 返回不可恢复错误，请检查配置"

    def _batch_mode(self, batch: _Batch) -> str:
        if batch.text_type in {"choice", "ui/system"}:
            return "contextual"
        return "compact"

    def _record_batch_request(self, batch: _Batch, cache) -> None:
        count = len(batch.items)
        cache.stats.batch_request_count += 1
        cache.stats.batch_item_count += count
        if self._batch_mode(batch) == "compact":
            cache.stats.compact_batch_request_count += 1
            cache.stats.compact_batch_item_count += count
        else:
            cache.stats.contextual_batch_request_count += 1
            cache.stats.contextual_batch_item_count += count

    def _build_batch_prompt(
        self,
        batch: _Batch,
        source_lang: str,
        target_lang: str,
        *,
        strict: bool = False,
    ) -> str:
        include_context = self._batch_mode(batch) == "contextual"
        records = []
        for idx, pending in enumerate(batch.items, 1):
            item = pending.item
            meta = getattr(item, "meta", {}) or {}
            source_text = translation_source_for_item(item)
            protected_text, _ = protect_placeholders(source_text)
            if include_context:
                row = {
                    "i": idx,
                    "s": protected_text,
                    "k": pending.text_type,
                }
                prev_text = meta.get("prev_text", "")
                next_text = meta.get("next_text", "")
                if prev_text:
                    row["p"] = prev_text
                if next_text:
                    row["n"] = next_text
                records.append(row)
            else:
                if batch.text_type == "mixed":
                    records.append([idx, protected_text, pending.text_type])
                else:
                    records.append([idx, protected_text])

        rules = [
            "只输出JSON对象，不要Markdown/解释/原文。",
            "格式必须是{\"t\":{\"1\":\"译文\",\"2\":\"译文\"}}，键为输入id字符串，每个id必须返回一次。",
            "值只能是中文译文；保留控制符、变量、占位符和换行语义。",
        ]
        if batch.text_type == "choice":
            rules.append("这些是剧情选项，译文要短，适合按钮显示。")
        elif batch.text_type == "name":
            rules.append("这些是角色名/说话人名，只输出名字本身；不要括号解释、百科说明、读音说明或询问用户补充文本。")
        elif batch.text_type == "ui/system":
            rules.append("这些是界面/系统文本，译文要简洁。")
        elif batch.text_type == "mixed":
            rules.append(
                "每条输入的第三项是文本类型：name 只译名字本身，choice 要简短，"
                "ui/system 要简洁，message 使用自然口语。"
            )
        else:
            rules.append("对白要自然口语化。")
        if strict:
            rules.insert(0, "上次格式不合格。这次只能输出可被json.loads解析的JSON对象，顶层只能有t字段。")

        return (
            f"将以下{source_lang}游戏文本翻译为{target_lang}。\n"
            + "\n".join(rules)
            + "\n输入JSON:\n"
            + json.dumps(records, ensure_ascii=False, separators=(",", ":"))
            + "\n输出JSON:"
        )

    def _batch_max_tokens(self, batch: _Batch, cache) -> int:
        base = sum(cache.estimate_tokens(translation_source_for_item(p.item)) for p in batch.items)
        estimated = int(base * 1.7 + len(batch.items) * 12 + 192)
        # max_tokens 上限对齐输出预算（deepseek 服务器支持到 393216），
        # 保证请求不会因输出超长被截断。complex 批（含占位符/控制符）仍保守收敛。
        upper = 2048 if batch.complex else self.MAX_OUTPUT_TOKENS
        return max(256, min(upper, estimated))

    def _batch_input_tokens(self, batch: _Batch, cache, source_lang: str, target_lang: str) -> int:
        return cache.estimate_tokens(self._build_batch_prompt(batch, source_lang, target_lang))

    def _single_max_tokens(self, item: TextItem, cache) -> int:
        estimated = int(cache.estimate_tokens(translation_source_for_item(item)) * 1.4 + 128)
        upper = 2048 if self._is_complex_item(item, cache) else 1024
        return max(256, min(upper, estimated))

    def _parse_batch_json(self, raw: str, expected_ids: list[int]) -> dict[int, str]:
        return parse_batch_json(raw, expected_ids)

    def _is_same_as_source_refusal(self, item: TextItem, raw: str, source_lang: str) -> bool:
        """批结果与原文一致、且原文是数据库 token（数字/符号混合体）时采纳原样。

        典型：日语游戏数据库里本就是中文/数字/符号的条目（"城镇2"、"攻击+3"）。
        单条重译只会得到同样结果再回退原文，白烧一个请求；数据库型游戏
        几百条一起触发时会形成请求风暴，撞上限流把整轮翻译拖慢数倍。

        严格规则：原文必须含数字或数学/符号字符（+、-、×、÷、%、#、&、*、/、·），
        且无假名。纯汉字词（勝利、敗北、情報提供）必须走重译，避免漏翻日文。
        """
        if str(source_lang or "").lower() not in {"ja", "jp", "japanese"}:
            return False
        source_text = validation_source_for_item(item)
        candidate = str(raw or "")
        if not candidate.strip():
            return False
        _protected, placeholders = protect_placeholders(source_text)
        if placeholders:
            candidate = restore_placeholders(candidate, placeholders)
        if candidate.strip() != source_text.strip():
            return False
        if contains_kana(source_text):
            return False
        # 必须含数字或常见数据库符号，纯汉字词不放行
        visible = source_text.strip()
        has_digit = any(c.isdigit() for c in visible)
        has_db_symbol = any(c in visible for c in "+-×÷%#&*/·")
        return has_digit or has_db_symbol

    def _is_same_as_source_allowed(self, item: TextItem, translated: str, source_lang: str) -> bool:
        source_text = validation_source_for_item(item)
        candidate = str(translated or "").strip()
        if candidate not in {str(item.original or "").strip(), source_text.strip()}:
            return True
        if str(source_lang or "").lower() in {"ja", "jp", "japanese"}:
            return self._is_same_as_source_refusal(item, translated, source_lang)
        return is_acceptable_same_as_source(source_text, translated)

    def _should_store_translation(self, item: TextItem, translated: str | None, source_lang: str) -> bool:
        source_text = validation_source_for_item(item)
        candidate = str(translated or "").strip()
        if not candidate:
            return False
        if candidate in {str(item.original or "").strip(), source_text.strip()}:
            if not self._is_same_as_source_allowed(item, translated or "", source_lang):
                return False
        return self.should_cache_translation(source_text, translated)

    def _target_output_is_valid(
        self,
        item: TextItem,
        translated: str,
        source_lang: str,
        target_lang: str,
    ) -> bool:
        if not str(target_lang or "").lower().startswith("zh"):
            return True
        if str(source_lang or "").lower() not in {"ja", "jp", "japanese", "auto"}:
            return True

        source_text = validation_source_for_item(item)
        source_visible = self._visible_language_text(source_text)
        translated_visible = self._visible_language_text(translated)
        source_has_language = contains_kana(source_visible) or bool(
            re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", source_visible)
        )
        if not source_has_language:
            return True
        if re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", translated_visible):
            return True

        # A lone Japanese particle can legitimately disappear around a WOLF
        # runtime variable, for example ``\cself[7]が`` -> ``\cself[7]``.
        kana = re.findall(
            r"[\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fc-\u30ff\uff66-\uff9f]",
            source_visible,
        )
        translated_content = re.sub(r"[\W_]+", "", translated_visible, flags=re.UNICODE)
        return len(kana) <= 1 and not translated_content

    @staticmethod
    def _visible_language_text(text: str) -> str:
        protected, _placeholders = protect_placeholders(str(text or ""))
        return re.sub(r"\{\{PH\d+\}\}", "", protected)

    def _valid_cached_translation_for_item(
        self,
        item: TextItem,
        cached: str | None,
        source_lang: str,
        target_lang: str = "",
    ) -> str | None:
        if not cached or not cached.strip():
            return None
        if not self._target_output_is_valid(item, cached, source_lang, target_lang):
            return None
        source_text = validation_source_for_item(item)
        safe, _warns = verify_translation(source_text, cached)
        if safe != cached and safe == source_text and cached != source_text:
            return None
        if not self._should_store_translation(item, safe, source_lang):
            return None
        return safe

    def _validate_batch_translation(
        self,
        item: TextItem,
        translated: str,
        source_lang: str = "",
        target_lang: str = "",
    ) -> str | None:
        source_text = validation_source_for_item(item)
        if not translated:
            return None
        _protected, placeholders = protect_placeholders(source_text)
        if placeholders:
            translated = restore_placeholders(translated, placeholders)
        if translated in {item.original, source_text} and not self._is_same_as_source_allowed(item, translated, source_lang):
            return None
        if len(translated) > max(20, int(len(source_text) * 2.5) + 20):
            return None
        if not self._target_output_is_valid(item, translated, source_lang, target_lang):
            return None
        safe, warns = verify_translation(source_text, translated)
        for w in warns:
            warning(f"[批处理][{item.original[:30]}...] {w}")
        if self._should_store_translation(item, safe, source_lang):
            return safe
        return None

    async def _translate_single_items(
        self,
        pending: list[_PendingItem],
        client: Any,
        source_lang: str,
        target_lang: str,
        *,
        count_validation_fail: bool = True,
    ) -> list[TextItem]:
        if not pending:
            return []
        config = get_config()
        cache = get_cache()
        concurrency = self._configured_concurrency(config)
        worker_count = min(concurrency, len(pending))
        queue: asyncio.Queue[_PendingItem] = asyncio.Queue()
        for pending_item in pending:
            queue.put_nowait(pending_item)
        results: list[TextItem | BaseException] = []

        async def translate_one(pending_item: _PendingItem) -> TextItem:
            item = pending_item.item
            try:
                prompt, placeholders = self.prepare_prompt_for_item(item, source_lang, target_lang)
                max_tokens = self._single_max_tokens(item, cache)
                result = await self._call_chat(client, prompt, max_tokens)
                result = restore_placeholders(result, placeholders)
                source_text = translation_source_for_item(item)
                safe_result, warns = verify_translation(source_text, result)
                if not self._target_output_is_valid(item, safe_result, source_lang, target_lang):
                    warns.append("目标为中文，但译文没有可显示的中文字符")
                    safe_result = source_text
                for w in warns:
                    warning(f"[{item.original[:40]}...] {w}")
                if not self._should_store_translation(item, safe_result, source_lang):
                    retry_warnings = warns or ["译文为空、等于原文或未通过缓存写入条件"]
                    feedback = self.prepare_retry_feedback(source_text, result, retry_warnings)
                    result2 = await self._call_chat(client, feedback, max_tokens)
                    result2 = restore_placeholders(result2, placeholders)
                    safe_result2, warns2 = verify_translation(source_text, result2)
                    if not self._target_output_is_valid(item, safe_result2, source_lang, target_lang):
                        warns2.append("目标为中文，但重试译文仍没有可显示的中文字符")
                        safe_result2 = source_text
                    for w in warns2:
                        warning(f"[重试][{item.original[:40]}...] {w}")
                    if self._should_store_translation(item, safe_result2, source_lang):
                        safe_result = safe_result2
                if self._should_store_translation(item, safe_result, source_lang):
                    self._store_success(item, safe_result, source_lang, target_lang)
                else:
                    if count_validation_fail:
                        cache.stats.validation_fail_count += 1
                    item.translated = item.original
            except asyncio.CancelledError:
                item.translated = item.original
                raise
            except Exception as e:
                warning(f"单条兜底翻译失败 [{item.original[:30]}...]: {e}")
                item.translated = item.original
            return item

        async def worker():
            while True:
                try:
                    pending_item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    results.append(await translate_one(pending_item))
                except Exception as e:
                    results.append(e)
                finally:
                    queue.task_done()

        await asyncio.gather(*(worker() for _ in range(worker_count)), return_exceptions=True)
        valid = []
        for r in results:
            if isinstance(r, BaseException):
                continue
            valid.append(r)
        return valid

