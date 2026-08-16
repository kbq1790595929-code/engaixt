from __future__ import annotations

import asyncio
from engines.base import TextItem
from core.trial_quota import TrialQuotaExceeded, ensure_translation_quota_available
from translators.base import TranslatorBase, retry_with_backoff
from translators.cache import get_cache
from config import get_config
from utils.logger import info, warning
from utils.text_extract import verify_translation, restore_placeholders


class AnthropicTranslator(TranslatorBase):
    name = "anthropic"
    label = "Anthropic (Claude)"
    MODEL = "claude-sonnet-5"
    MODEL_CONFIG_FIELD = "anthropic_model"

    async def translate_batch(
        self,
        items: list[TextItem],
        source_lang: str,
        target_lang: str,
        on_progress=None,
    ) -> list[TextItem]:
        config = get_config()
        cache = get_cache()

        untranslated = []
        for item in items:
            cached = cache.get(item.original, source_lang, target_lang)
            safe_cached = self.valid_cached_translation(item.original, cached)
            if safe_cached:
                item.translated = safe_cached
            else:
                untranslated.append(item)

        total = len(untranslated)
        if total == 0:
            info(f"全部 {len(items)} 条命中缓存，跳过翻译")
            if on_progress:
                on_progress(len(items), len(items))
            return items

        if not config.anthropic_api_key:
            warning("Anthropic API Key 未配置，跳过 AI 翻译")
            for item in untranslated:
                item.translated = item.original
            return items

        concurrency = max(1, config.max_concurrency)
        info(f"使用 Claude 翻译 {total} 条文本（{concurrency} 并发，{len(items) - total} 条命中缓存）")

        # 立即上报初始进度
        if on_progress:
            try:
                on_progress(0, total)
            except Exception:
                pass

        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=config.anthropic_api_key)

        sem = asyncio.Semaphore(concurrency)
        completed = [0]

        async def translate_one(item: TextItem):
            try:
                async with sem:
                    prompt, placeholders = self.prepare_prompt(item.original, source_lang, target_lang)

                    async def call():
                        ensure_translation_quota_available()
                        resp = await client.messages.create(
                            model=config.anthropic_model,
                            max_tokens=1024,
                            messages=[{"role": "user", "content": prompt}],
                            timeout=30,
                        )
                        content = resp.content[0].text.strip()
                        cache.record_api_call(
                            prompt,
                            content,
                            getattr(resp, "usage", None),
                            provider="anthropic",
                            model=config.anthropic_model,
                        )
                        return content

                    result = await retry_with_backoff(call)
                    result = restore_placeholders(result, placeholders)

                    safe_result, warns = verify_translation(item.original, result)
                    for w in warns:
                        warning(f"[{item.original[:40]}...] {w}")

                    if safe_result == item.original and warns:
                        warning(f"[{item.original[:40]}...] 翻译校验失败，将错误反馈 AI 重试...")
                        feedback = self.prepare_retry_feedback(item.original, result, warns)
                        async def retry_call():
                            ensure_translation_quota_available()
                            return await client.messages.create(
                                model=config.anthropic_model,
                                max_tokens=1024,
                                messages=[{"role": "user", "content": feedback}],
                                timeout=30,
                            )

                        resp2 = await retry_with_backoff(retry_call)
                        result2 = resp2.content[0].text.strip()
                        cache.record_api_call(
                            feedback,
                            result2,
                            getattr(resp2, "usage", None),
                            provider="anthropic",
                            model=config.anthropic_model,
                        )
                        result2 = restore_placeholders(result2, placeholders)
                        safe_result2, warns2 = verify_translation(item.original, result2)
                        for w in warns2:
                            warning(f"[重试][{item.original[:40]}...] {w}")
                        if safe_result2 != item.original:
                            safe_result = safe_result2

                    if self.should_cache_translation(item.original, safe_result):
                        cache.set(item.original, safe_result, source_lang, target_lang)
                    item.translated = safe_result
            except asyncio.CancelledError:
                item.translated = item.original
                raise
            except TrialQuotaExceeded:
                item.translated = item.original
                raise
            except Exception as e:
                warning(f"翻译失败 [{item.original[:30]}...]: {e}")
                item.translated = item.original

            completed[0] += 1
            if on_progress:
                try:
                    on_progress(completed[0], total)
                except Exception:
                    pass
            return item

        tasks = [translate_one(item) for item in untranslated]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid = []
        for r in results:
            if isinstance(r, TrialQuotaExceeded):
                raise r
            if isinstance(r, BaseException):
                continue
            if r is not None:
                valid.append(r)
        return [i for i in items if i not in untranslated] + valid
