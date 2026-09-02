"""Configuration and cache helpers shared by DeepSeek-compatible translators."""

from __future__ import annotations

from typing import Any

from config import get_config
from engines.base import TextItem
from utils.text_extract import translation_source_for_item


def _get_cache():
    # Resolve through the owning module so existing tests and provider wrappers
    # can replace the cache factory without patching two modules.
    from translators import deepseek

    return deepseek.get_cache()


class DeepSeekRuntimeMixin:
    def _configured_concurrency(self, config) -> int:
        configured = max(1, int(config.max_concurrency or 1))
        # Respect the model's documented concurrency limit (e.g. deepseek-v4-pro
        # caps at 500) so batch storms do not trip the official API rate limit.
        try:
            from translators.pricing import pricing_for
            limit = pricing_for("deepseek", self._model(config)).concurrency_limit
            if limit and int(limit) > 0:
                configured = min(configured, int(limit))
        except Exception:
            pass
        return max(1, configured)

    def _api_key(self, config) -> str:
        for field in self.API_KEY_FIELDS:
            value = str(getattr(config, field, "") or "").strip()
            if value:
                return value
        return ""

    def _model(self, config=None) -> str:
        config = config or get_config()
        field = str(getattr(self, "MODEL_CONFIG_FIELD", "") or "")
        if field:
            value = str(getattr(config, field, "") or "").strip()
            if value:
                return value
        return self.MODEL

    def _chat_extra_body(self) -> dict[str, Any] | None:
        extra = getattr(self, "EXTRA_BODY", None)
        if isinstance(extra, dict):
            return dict(extra)
        return None

    def _cache_lookup_for_item(
        self,
        item: TextItem,
        source_lang: str,
        target_lang: str,
        *,
        count_stats: bool = False,
    ):
        cache = _get_cache()
        text_type = cache.infer_text_type(item)
        return cache.lookup(
            translation_source_for_item(item),
            source_lang,
            target_lang,
            provider=self.name,
            model=self._model(),
            prompt_version=self.PROMPT_VERSION,
            text_type=self._cache_text_type(item, text_type),
            count_stats=count_stats,
        )

    @staticmethod
    def _cache_text_type(item: TextItem, text_type: str) -> str:
        meta = getattr(item, "meta", {}) or {}
        scope = str(meta.get("translation_cache_scope") or "").strip()
        return f"{text_type}@{scope}" if scope else text_type

    def prepare_prompt_for_item(
        self,
        item: TextItem,
        source_lang: str,
        target_lang: str,
    ) -> tuple[str, list[str]]:
        meta = getattr(item, "meta", {}) or {}
        source_text = translation_source_for_item(item)
        prompt, placeholders = self.prepare_prompt(
            source_text,
            source_lang,
            target_lang,
            prev_text=meta.get("prev_text", ""),
            next_text=meta.get("next_text", ""),
        )
        if item.context == "choice" or meta.get("kind") == "choice":
            prompt += "\n\n补充要求：这是剧情分支选项。译文必须短、明确、适合按钮显示，不要扩写成完整对白。"
        return prompt, placeholders

    def _store_success(
        self,
        item: TextItem,
        translated: str,
        source_lang: str,
        target_lang: str,
    ) -> None:
        if (
            not self._target_output_is_valid(item, translated, source_lang, target_lang)
            or not self._should_store_translation(item, translated, source_lang)
        ):
            item.translated = item.original
            return
        cache = _get_cache()
        text_type = cache.infer_text_type(item)
        source_text = translation_source_for_item(item)
        cache.set_v2(
            source_text,
            translated,
            source_lang,
            target_lang,
            provider=self.name,
            model=self._model(),
            prompt_version=self.PROMPT_VERSION,
            text_type=self._cache_text_type(item, text_type),
        )
        item.translated = translated

    def _propagate_by_cache_key(
        self,
        items: list[TextItem],
        pending: list[Any],
        source_lang: str,
        target_lang: str,
    ) -> None:
        cache = _get_cache()
        trans_map = {
            value.cache_key: value.item.translated
            for value in pending
            if self._should_store_translation(value.item, value.item.translated, source_lang)
        }
        if not trans_map:
            return
        for item in items:
            if self._should_store_translation(item, item.translated, source_lang):
                continue
            text_type = cache.infer_text_type(item)
            source_text = translation_source_for_item(item)
            lookup = cache.lookup(
                source_text,
                source_lang,
                target_lang,
                provider=self.name,
                model=self._model(),
                prompt_version=self.PROMPT_VERSION,
                text_type=self._cache_text_type(item, text_type),
                count_stats=False,
            )
            translated = trans_map.get(lookup.key)
            if translated:
                item.translated = translated
                cache.record_saved_request(source_text, translated)
