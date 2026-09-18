"""Pipeline retry budget regressions."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.pipeline as pipeline_mod
from core.pipeline import Pipeline
from engines.base import TextItem


class _RetryTranslator:
    def __init__(self):
        self.seen = []

    async def translate_batch(self, items, source_lang, target_lang, on_progress=None):
        self.seen.append([item.original for item in items])
        for item in items:
            item.translated = "已修正"
        return items


def test_retry_invalid_translations_only_retries_validation_failures(monkeypatch=None):
    retry_original = "\u3082\u3046\u4e00\u5ea6"
    untouched_original = "\u307e\u3060\u672a\u7ffb\u8a33"
    done_original = "\u7ffb\u8a33\u6e08\u307f"
    failed_translation = "\u518d\u6765\u3082"

    translator = _RetryTranslator()
    old_get_translator = pipeline_mod._get_translator
    old_save_cache = pipeline_mod.save_translations_to_cache
    pipeline_mod._get_translator = lambda name: translator
    pipeline_mod.save_translations_to_cache = lambda game_path, items: None

    items = [
        TextItem(
            file="TimeLine/test.dtl",
            original=retry_original,
            translated=retry_original,
            meta={"validation_failed_translation": failed_translation},
        ),
        TextItem(file="TimeLine/test.dtl", original=untouched_original, translated="", meta={}),
        TextItem(file="TimeLine/test.dtl", original=done_original, translated="已翻译", meta={}),
    ]

    try:
        pipe = Pipeline()
        pipe._sync_checkpoint_json = lambda checkpoint, items: None
        result = asyncio.run(pipe._retry_invalid_translations(
            items,
            Path("checkpoint.json"),
            "ja",
            "zh-CN",
            Path("game"),
        ))
    finally:
        pipeline_mod._get_translator = old_get_translator
        pipeline_mod.save_translations_to_cache = old_save_cache

    assert translator.seen == [[retry_original]]
    assert result[0].translated == "已修正"
    assert result[1].translated == ""
    assert result[2].translated == "已翻译"


def test_retry_invalid_translations_retries_small_high_coverage_kana_tail():
    missing_original = "\u4e00\u56de\u4ee5\u4e0a\u9078\u3093\u3067\u304f\u3060\u3055\u3044\u3002"
    translator = _RetryTranslator()
    old_get_translator = pipeline_mod._get_translator
    old_save_cache = pipeline_mod.save_translations_to_cache
    pipeline_mod._get_translator = lambda name: translator
    pipeline_mod.save_translations_to_cache = lambda game_path, items: None

    items = [
        TextItem(
            file="hook",
            original=f"\u672c\u6587{i}",
            translated=f"\u8bd1\u6587{i}",
            context="message",
        )
        for i in range(19)
    ]
    items.append(TextItem(file="hook", original=missing_original, translated="", context="choice_help"))

    try:
        pipe = Pipeline()
        pipe._sync_checkpoint_json = lambda checkpoint, items: None
        result = asyncio.run(pipe._retry_invalid_translations(
            items,
            Path("checkpoint.json"),
            "ja",
            "zh-CN",
            Path("game"),
        ))
    finally:
        pipeline_mod._get_translator = old_get_translator
        pipeline_mod.save_translations_to_cache = old_save_cache

    assert translator.seen == [[missing_original]]
    assert result[-1].translated == "已修正"


def test_retry_invalid_translations_does_not_retry_large_tail():
    items = [
        TextItem(
            file="hook",
            original=f"source-{index}",
            translated=f"译文-{index}",
            context="message",
        )
        for index in range(3820)
    ]
    items.extend(
        TextItem(
            file="hook",
            original=f"\u307e\u3060\u672a\u7ffb\u8a33-{index}",
            translated="",
            context="message",
        )
        for index in range(201)
    )

    pipe = Pipeline()
    pipe._sync_checkpoint_json = lambda checkpoint, current_items: None
    result = asyncio.run(pipe._retry_invalid_translations(
        items,
        Path("checkpoint.json"),
        "ja",
        "zh-CN",
        Path("game"),
    ))

    assert result[-1].translated == ""


def test_pipeline_rejects_low_core_translation_coverage():
    pipe = Pipeline()
    items = [
        TextItem(file="script", original="本文1", translated="正文1", context="message"),
        TextItem(file="script", original="本文2", translated="", context="message"),
        TextItem(file="script", original="本文3", translated="", context="choice"),
        TextItem(file="script", original="名前", translated="", context="name"),
    ]

    assert not pipe._has_required_translation_coverage(items)


def test_pipeline_coverage_ignores_untranslated_names():
    pipe = Pipeline()
    items = [
        TextItem(file="script", original="本文1", translated="正文1", context="message"),
        TextItem(file="script", original="本文2", translated="正文2", context="choice"),
        TextItem(file="script", original="名前1", translated="", context="name"),
        TextItem(file="script", original="名前2", translated="", context="name"),
    ]

    assert pipe._has_required_translation_coverage(items)


def test_pipeline_skips_high_cache_tail_translation():
    pipe = Pipeline()
    items = [
        TextItem(file="script", original=f"本文{i}", translated=f"正文{i}", context="message")
        for i in range(99)
    ]
    items.append(TextItem(file="script", original="尾巴文本", translated="", context="message"))

    assert pipe._skip_cached_tail_translation(items, cached_count=99)
    assert items[-1].translated == "尾巴文本"


def test_pipeline_does_not_skip_first_run_tail_translation():
    pipe = Pipeline()
    items = [
        TextItem(file="script", original=f"本文{i}", translated="", context="message")
        for i in range(100)
    ]

    assert not pipe._skip_cached_tail_translation(items, cached_count=0)
