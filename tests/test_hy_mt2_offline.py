from __future__ import annotations

import asyncio
import json

from config import Config
from engines.base import TextItem
from translators.cache import TranslationCache
from translators.factory import (
    create_translator,
    translator_choices,
    translator_model,
)
from translators.hy_mt2 import HyMt2Translator
from translators.hy_mt2_component import (
    _response_resumed_from,
    classify_graphics_adapters,
)
from translators.hy_mt2_quality import (
    finalize_local_translations,
    repair_translation_structure,
)
from translators.hy_mt2_runtime import LocalCompletion
from translators.pricing import pricing_for
from utils import local_translation_bridge
from utils import translation_server


class _FakeRuntime:
    active_backend = "vulkan"

    def __init__(self):
        self.calls = 0

    def complete(self, prompt: str, max_tokens: int) -> LocalCompletion:
        self.calls += 1
        assert max_tokens >= 64
        payload = json.loads(prompt.split("输入JSON:\n", 1)[1])
        translations = {}
        for row_id, source in payload.items():
            translations[row_id] = {
                "こんにちは": "你好",
                "大丈夫？": "没问题吗？",
            }[source]
        content = json.dumps(translations, ensure_ascii=False)
        return LocalCompletion(content, prompt_tokens=32, completion_tokens=12)


def test_hy_mt2_is_registered_as_offline_model():
    assert "hy_mt2" in translator_choices()
    assert create_translator("hy_mt2").name == "hy_mt2"
    assert translator_model("hy_mt2", Config()) == "Hy-MT2-1.8B-Q4_K_M"
    pricing = pricing_for("hy_mt2", "Hy-MT2-1.8B-Q4_K_M")
    assert pricing.input_cny_per_m == 0
    assert pricing.output_cny_per_m == 0


def test_hardware_selection_uses_pci_vendor_id_not_display_name():
    fake_amd = [{
        "name": "NVIDIA GeForce RTX 5090",
        "pnp_device_id": "PCI\\VEN_1002&DEV_15BF",
    }]
    assert classify_graphics_adapters(fake_amd)["runner"] == "vulkan"
    assert classify_graphics_adapters(fake_amd)["vendor"] == "amd"
    assert classify_graphics_adapters(fake_amd)["name"] == "AMD Radeon 780M"

    rtx_50 = [{
        "name": "NVIDIA GeForce RTX 5090",
        "pnp_device_id": "PCI\\VEN_10DE&DEV_2B85",
    }]
    assert classify_graphics_adapters(rtx_50)["runner"] == "cuda-13.3"

    older_nvidia = [{
        "name": "NVIDIA GeForce RTX 3060",
        "pnp_device_id": "PCI\\VEN_10DE&DEV_2503",
    }]
    assert classify_graphics_adapters(older_nvidia)["runner"] == "cuda-12.4"


def test_hy_mt2_translates_batches_with_shared_cache(tmp_path, monkeypatch):
    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    runtime = _FakeRuntime()
    translator = HyMt2Translator(runtime=runtime)
    items = [
        TextItem(file="scene.ks", key="1", original="こんにちは"),
        TextItem(file="scene.ks", key="2", original="大丈夫？"),
        TextItem(file="scene.ks", key="3", original="こんにちは"),
    ]
    progress = []

    result = asyncio.run(translator.translate_batch(
        items,
        "ja",
        "zh-CN",
        on_progress=lambda current, total: progress.append((current, total)),
    ))

    assert [item.translated for item in result] == ["你好", "没问题吗？", "你好"]
    assert progress[-1] == (3, 3)
    assert cache.stats.dedupe_saved == 1
    assert cache.stats.estimated_cost_cny == 0
    assert runtime.calls == 1


def test_hy_mt2_reports_speed_only_through_local_callback(tmp_path, monkeypatch):
    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    runtime = _FakeRuntime()
    translator = HyMt2Translator(runtime=runtime)
    speeds = []
    items = [TextItem(file="scene.ks", key="1", original="こんにちは")]

    asyncio.run(translator.translate_batch(
        items, "ja", "zh-CN", on_speed=speeds.append,
    ))

    assert speeds
    assert all(row["provider"] == "hy_mt2" for row in speeds)
    assert speeds[-1]["generated_tokens"] == 12
    assert speeds[-1]["current_tps"] > 0


def test_hy_mt2_uses_streaming_runtime_when_available(tmp_path, monkeypatch):
    class StreamingRuntime(_FakeRuntime):
        def complete_with_progress(self, prompt, max_tokens, on_token):
            self.calls += 1
            on_token(2)
            return LocalCompletion(
                json.dumps({"1:message": "你好"}, ensure_ascii=False),
                prompt_tokens=32,
                completion_tokens=12,
                elapsed_seconds=0.2,
            )

    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    runtime = StreamingRuntime()
    translator = HyMt2Translator(runtime=runtime)
    items = [TextItem(file="scene.ks", key="1", original="こんにちは")]

    result = asyncio.run(translator.translate_batch(items, "ja", "zh-CN"))

    assert result[0].translated == "你好"
    assert runtime.calls == 1


def test_hy_mt2_config_defaults_are_bounded():
    cfg = Config()
    assert cfg.hy_mt2_context_size == 4096
    assert 1 <= cfg.hy_mt2_batch_size <= 16
    assert cfg.hy_mt2_idle_timeout_seconds >= 30


def test_hy_mt2_ignores_cloud_batch_and_concurrency_settings(tmp_path, monkeypatch):
    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    config = Config(
        max_batch_size=999,
        max_concurrency=999,
        hy_mt2_batch_size=1,
    )
    monkeypatch.setattr("translators.hy_mt2.get_config", lambda: config)
    runtime = _FakeRuntime()
    translator = HyMt2Translator(runtime=runtime)
    items = [
        TextItem(file="scene.ks", key="1", original="こんにちは"),
        TextItem(file="scene.ks", key="2", original="大丈夫？"),
    ]

    asyncio.run(translator.translate_batch(items, "ja", "zh-CN"))

    assert translator.MAX_MESSAGE_BATCH == 1
    assert runtime.calls == 2


def test_hy_mt2_realtime_short_text_uses_direct_prompt_and_cache(tmp_path, monkeypatch):
    class RealtimeRuntime:
        active_backend = "vulkan"

        def __init__(self):
            self.calls = 0
            self.prompts = []

        def complete(self, prompt: str, max_tokens: int) -> LocalCompletion:
            self.calls += 1
            self.prompts.append(prompt)
            return LocalCompletion(
                "\u201c\u563f\u563f\uff0c\u5c3c\u5c3c\u662f\u5904\u7537\u5417\uff1f\u201d",
                prompt_tokens=30,
                completion_tokens=12,
                elapsed_seconds=0.2,
            )

    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    runtime = RealtimeRuntime()
    translator = HyMt2Translator(runtime=runtime)
    source = "\u300c\u306d\u30fc\u306d\u30fc\u3002\u306b\u3043\u306b\u3063\u3066\u7ae5\u8c9e\uff1f\u300d"

    first = asyncio.run(translator.translate_realtime_text(source, "ja", "zh-CN"))
    second = asyncio.run(translator.translate_realtime_text(source, "ja", "zh-CN"))

    assert first == "\u300c\u563f\u563f\uff0c\u5c3c\u5c3c\u662f\u5904\u7537\u5417\uff1f\u300d"
    assert second == first
    assert runtime.calls == 1
    assert "JSON" not in runtime.prompts[0]
    assert source in runtime.prompts[0]


def test_hy_mt2_realtime_uses_bilingual_message_context(tmp_path, monkeypatch):
    class ContextRuntime:
        active_backend = "vulkan"

        def __init__(self):
            self.messages = []

        def complete_messages(self, messages, max_tokens):
            self.messages.append(messages)
            return LocalCompletion(
                "\u300c\u597d\u7684\u266a\u300d",
                prompt_tokens=70,
                completion_tokens=5,
                elapsed_seconds=0.3,
            )

        def complete(self, prompt, max_tokens):
            raise AssertionError("context request should use complete_messages")

    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    runtime = ContextRuntime()
    translator = HyMt2Translator(runtime=runtime)
    context = [{
        "source": "\u300c\u308f\u304b\u3063\u305f\u3088\u3002\u6bce\u65e5\u3061\u3083\u3093\u3068\u3059\u308b\u3093\u3060\u3088\uff1f\u300d",
        "translated": "\u300c\u77e5\u9053\u4e86\u3002\u6bcf\u5929\u90fd\u8981\u597d\u597d\u505a\u54e6\uff1f\u300d",
        "speaker": "",
    }]

    result = asyncio.run(translator.translate_realtime_text(
        "\u300c\u306f\u30fc\u3044\u3063\u266a\u300d",
        "ja",
        "zh-CN",
        context=context,
        speaker="\u7eef\u96ea",
    ))

    assert result == "\u300c\u597d\u7684\u266a\u300d"
    assert [message["role"] for message in runtime.messages[0]] == [
        "system", "user", "assistant", "user"
    ]
    assert runtime.messages[0][1]["content"] == context[0]["source"]
    assert runtime.messages[0][2]["content"] == context[0]["translated"]
    assert runtime.messages[0][-1]["content"] == "\u300c\u306f\u30fc\u3044\u3063\u266a\u300d"


def test_hy_mt2_realtime_context_echo_falls_back_to_direct_prompt(tmp_path, monkeypatch):
    class EchoRuntime:
        active_backend = "vulkan"

        def __init__(self):
            self.context_calls = 0
            self.direct_calls = 0

        def complete_messages(self, messages, max_tokens):
            self.context_calls += 1
            return LocalCompletion(
                "\u300c\u77e5\u9053\u4e86\u3002\u300d",
                prompt_tokens=60,
                completion_tokens=5,
                elapsed_seconds=0.2,
            )

        def complete(self, prompt, max_tokens):
            self.direct_calls += 1
            return LocalCompletion(
                "\u300c\u597d\u7684\u3002\u300d",
                prompt_tokens=30,
                completion_tokens=5,
                elapsed_seconds=0.2,
            )

    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    runtime = EchoRuntime()
    translator = HyMt2Translator(runtime=runtime)
    context = [{
        "source": "\u300c\u308f\u304b\u3063\u305f\u3002\u300d",
        "translated": "\u300c\u77e5\u9053\u4e86\u3002\u300d",
        "speaker": "",
    }]

    result = asyncio.run(translator.translate_realtime_text(
        "\u300c\u3058\u3083\u3042\u3001\u304a\u9858\u3044\u3002\u300d",
        context=context,
    ))

    assert result == "\u300c\u597d\u7684\u3002\u300d"
    assert runtime.context_calls == 1
    assert runtime.direct_calls == 1


def test_hy_mt2_realtime_skips_context_for_standalone_question(tmp_path, monkeypatch):
    class StandaloneRuntime:
        active_backend = "vulkan"

        def __init__(self):
            self.direct_calls = 0

        def complete_messages(self, messages, max_tokens):
            raise AssertionError("standalone line should not use dialogue context")

        def complete(self, prompt, max_tokens):
            self.direct_calls += 1
            return LocalCompletion(
                "\u300c\u554a\uff1f\u6362\u6d17\u8863\u670d\u5462\uff1f\u300d",
                prompt_tokens=30,
                completion_tokens=8,
                elapsed_seconds=0.2,
            )

    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    runtime = StandaloneRuntime()
    translator = HyMt2Translator(runtime=runtime)

    result = asyncio.run(translator.translate_realtime_text(
        "\u300c\u3048\uff1f \u7740\u66ff\u3048\u306f\uff1f\u300d",
        context=[{
            "source": "\u300c\u304a\u306f\u3088\u30fc\uff01\u300d",
            "translated": "\u300c\u65e9\u4e0a\u597d\uff01\u300d",
            "speaker": "",
        }],
    ))

    assert result == "\u300c\u554a\uff1f\u6362\u6d17\u8863\u670d\u5462\uff1f\u300d"
    assert runtime.direct_calls == 1


def test_hy_mt2_preserves_per_line_japanese_dialogue_wrappers():
    source = "「大丈夫？」\n「心配しないで」"
    translated = "“没事吧？”\n“别担心。”"

    assert HyMt2Translator._restore_line_dialogue_wrappers(source, translated) == (
        "「没事吧？」\n「别担心。」"
    )


def test_hy_mt2_repairs_multiline_dialogue_and_orphan_brace():
    source = "【スカーレット】\n「一行目\n　二行目\\i[126]」"
    translated = "【斯嘉特】\n「第一行\n 第二行\\i[126]}"

    assert repair_translation_structure(source, translated) == (
        "【斯嘉特】\n「第一行\n 第二行\\i[126]」"
    )


def test_hy_mt2_unifies_repeated_speaker_names_by_majority():
    items = [
        TextItem(file="a", original="【スカーレット】\n「一」", translated="【斯嘉丽】\n「一」"),
        TextItem(file="a", original="【スカーレット】\n「二」", translated="【斯嘉丽】\n「二」"),
        TextItem(file="a", original="【スカーレット】\n「三」", translated="【斯嘉特】\n「三」"),
    ]

    structure_fixed, speaker_fixed, groups = finalize_local_translations(items)

    assert structure_fixed == 0
    assert speaker_fixed == 1
    assert groups == 1
    assert all(item.translated.startswith("【斯嘉丽】") for item in items)


def test_hy_mt2_postprocess_updates_changed_cache_rows(tmp_path, monkeypatch):
    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    translator = HyMt2Translator(runtime=_FakeRuntime())
    items = [
        TextItem(file="scene", original="【スカーレット】\n「一」", translated="【斯嘉丽】\n「一」"),
        TextItem(file="scene", original="【スカーレット】\n「二」", translated="【斯嘉特】\n「二」"),
    ]

    translator._finalize(items, "ja", "zh-CN")

    assert items[1].translated == "【斯嘉丽】\n「二」"
    lookup = cache.lookup(
        items[1].original,
        "ja",
        "zh-CN",
        provider="hy_mt2",
        model=translator._model(),
        prompt_version=translator.PROMPT_VERSION,
        text_type="message",
    )
    assert lookup.translated == items[1].translated


def test_hy_mt2_retries_invalid_results_in_small_batches(tmp_path, monkeypatch):
    class RetryRuntime:
        active_backend = "vulkan"

        def __init__(self):
            self.calls = 0

        def complete(self, prompt: str, max_tokens: int) -> LocalCompletion:
            self.calls += 1
            payload = json.loads(prompt.split("输入JSON:\n", 1)[1])
            if self.calls == 1:
                result = payload
            else:
                result = {key: f"中文译文{index}" for index, key in enumerate(payload, 1)}
            return LocalCompletion(json.dumps(result, ensure_ascii=False), 10, 10)

    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    runtime = RetryRuntime()
    translator = HyMt2Translator(runtime=runtime)
    items = [
        TextItem(file="scene", key=str(index), original=f"テキスト{index}")
        for index in range(8)
    ]

    result = asyncio.run(translator.translate_batch(items, "ja", "zh-CN"))

    assert all(item.translated.startswith("中文译文") for item in result)
    assert runtime.calls == 3
    assert cache.stats.batch_retry_count == 2
    assert cache.stats.single_fallback_count == 0


def test_hy_mt2_keeps_local_placeholder_before_control_validation(tmp_path, monkeypatch):
    class PlaceholderRuntime(_FakeRuntime):
        def complete(self, prompt: str, max_tokens: int) -> LocalCompletion:
            self.calls += 1
            payload = json.loads(prompt.split("输入JSON:\n", 1)[1])
            result = {key: "中文 {{PH0}}" for key in payload}
            return LocalCompletion(json.dumps(result, ensure_ascii=False), 32, 12)

    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    translator = HyMt2Translator(runtime=PlaceholderRuntime())
    item = TextItem(
        file="Data.wolf",
        original="台词\\i[126]",
        meta={"wolf_role": "command"},
    )

    result = asyncio.run(translator.translate_batch([item], "ja", "zh-CN"))

    assert result[0].translated == "中文 \\i[126]"


def test_hy_mt2_batches_are_capped_by_max_message_batch(tmp_path, monkeypatch):
    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    translator = HyMt2Translator(runtime=_FakeRuntime())
    translator.MAX_MESSAGE_BATCH = 8
    items = [
        TextItem(file="scene", key=str(index), original=f"テキスト{index}")
        for index in range(1, 101)
    ]
    pending, _ = translator._collect_pending(items, "ja", "zh-CN")
    batches = translator._make_batches(pending, cache, "ja", "zh-CN")

    assert batches
    assert all(len(batch.items) <= translator.MAX_MESSAGE_BATCH for batch in batches)
    assert sum(len(batch.items) for batch in batches) == len(pending)


def test_hy_mt2_strict_retry_prompt_keeps_key_format(tmp_path, monkeypatch):
    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    translator = HyMt2Translator(runtime=_FakeRuntime())
    translator.MAX_MESSAGE_BATCH = 8
    items = [
        TextItem(file="scene", key=str(index), original=f"テキスト{index}")
        for index in range(1, 9)
    ]
    pending, _ = translator._collect_pending(items, "ja", "zh-CN")
    batches = translator._make_batches(pending, cache, "ja", "zh-CN")
    prompt, _keys = translator._build_local_prompt(
        batches[0], "ja", "zh-CN", strict=True
    )

    # 旧提示词会让 7B 输出丢失键中的冒号（如 "38":"message"），必须不再使用；
    # 新提示词明确要求键与输入完全一致、逗号分隔。
    assert "上次输出格式不正确，请重新翻译" in prompt
    assert "严格只返回可被json.loads解析的JSON对象" not in prompt


def test_hy_mt2_reordered_response_ids_cannot_shift_translations(tmp_path, monkeypatch):
    class ReorderedRuntime(_FakeRuntime):
        def complete(self, prompt: str, max_tokens: int) -> LocalCompletion:
            self.calls += 1
            payload = json.loads(prompt.split("输入JSON:\n", 1)[1])
            keys = list(payload)
            result = {keys[-1]: "第二条", keys[0]: "第一条"}
            return LocalCompletion(json.dumps(result, ensure_ascii=False), 10, 10)

    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    translator = HyMt2Translator(runtime=ReorderedRuntime())
    items = [
        TextItem(file="scene", key="1", original="一つ目の文章です"),
        TextItem(file="scene", key="2", original="二つ目の文章です"),
    ]

    result = asyncio.run(translator.translate_batch(items, "ja", "zh-CN"))

    assert [item.translated for item in result] == ["第一条", "第二条"]


def test_hy_mt2_rejects_collapsed_local_translation_and_retries(tmp_path, monkeypatch):
    class CollapseRuntime(_FakeRuntime):
        def complete(self, prompt: str, max_tokens: int) -> LocalCompletion:
            self.calls += 1
            payload = json.loads(prompt.split("输入JSON:\n", 1)[1])
            if self.calls == 1:
                result = {key: "呢。" for key in payload}
            else:
                result = {key: "这是完整的中文译文" for key in payload}
            return LocalCompletion(json.dumps(result, ensure_ascii=False), 10, 10)

    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    runtime = CollapseRuntime()
    translator = HyMt2Translator(runtime=runtime)
    items = [
        TextItem(file="scene", key="1", original="これは十分に長い文章のテストです"),
        TextItem(file="scene", key="2", original="こちらも十分に長い文章のテストです"),
    ]

    result = asyncio.run(translator.translate_batch(items, "ja", "zh-CN"))

    assert all(item.translated == "这是完整的中文译文" for item in result)
    assert cache.stats.local_quality_fail_count == 2
    assert runtime.calls == 2


def test_hy_mt2_requires_local_controls_in_original_order(tmp_path, monkeypatch):
    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    translator = HyMt2Translator(runtime=_FakeRuntime())
    item = TextItem(file="scene", original="台词\\.\\|段落")

    assert translator._validate_batch_translation(item, "中文", "ja", "zh-CN") is None
    assert translator._validate_batch_translation(item, "中文 \\.\\|段落", "ja", "zh-CN") == "中文 \\.\\|段落"
    assert cache.stats.local_control_fail_count == 1


def test_hy_mt2_does_not_accept_generation_cut_off_at_max_tokens(tmp_path, monkeypatch):
    class TruncatedRuntime(_FakeRuntime):
        def complete(self, prompt: str, max_tokens: int) -> LocalCompletion:
            self.calls += 1
            payload = json.loads(prompt.split("输入JSON:\n", 1)[1])
            result = {key: "这是完整的中文译文" for key in payload}
            if self.calls == 1:
                return LocalCompletion(
                    json.dumps(result, ensure_ascii=False),
                    10,
                    10,
                    finish_reason="length",
                )
            return LocalCompletion(json.dumps(result, ensure_ascii=False), 10, 10)

    cache = TranslationCache(tmp_path / "translations.db")
    monkeypatch.setattr("translators.deepseek.get_cache", lambda: cache)
    monkeypatch.setattr("translators.hy_mt2.get_cache", lambda: cache)
    runtime = TruncatedRuntime()
    translator = HyMt2Translator(runtime=runtime)
    items = [TextItem(file="scene", key="1", original="これは長い文章のテストです")]

    result = asyncio.run(translator.translate_batch(items, "ja", "zh-CN"))

    assert result[0].translated == "这是完整的中文译文"
    assert cache.stats.local_truncation_count == 1
    assert runtime.calls == 2


def test_xunity_proxy_routes_hy_mt2_to_local_provider(monkeypatch):
    calls = []

    def fake_translate(text, source_lang, target_lang, provider):
        calls.append((text, source_lang, target_lang, provider))
        return "本地译文"

    monkeypatch.setattr(
        local_translation_bridge,
        "translate_with_local_provider",
        fake_translate,
    )

    result = translation_server._translate_via_ai("原文", "ja", "zh-CN", "hy_mt2")

    assert result == "本地译文"
    assert calls == [("原文", "ja", "zh-CN", "hy_mt2")]


def test_local_bridge_reuses_translator_instance(monkeypatch):
    class FakeTranslator:
        async def translate_batch(self, items, source_lang, target_lang):
            items[0].translated = f"译文:{items[0].original}"
            return items

    created = []

    def fake_create(provider):
        created.append(provider)
        return FakeTranslator()

    local_translation_bridge._reset_for_tests()
    monkeypatch.setattr(local_translation_bridge, "create_translator", fake_create)

    first = local_translation_bridge.translate_with_local_provider(
        "一行", "ja", "zh-CN", "hy_mt2"
    )
    second = local_translation_bridge.translate_with_local_provider(
        "二行", "ja", "zh-CN", "hy_mt2"
    )

    assert first == "译文:一行"
    assert second == "译文:二行"
    assert created == ["hy_mt2"]
    local_translation_bridge._reset_for_tests()


def test_hy_mt2_proxy_bypasses_cloud_rate_limit(monkeypatch):
    translation_server._translation_cache.clear()
    translation_server._cache_access_order.clear()
    monkeypatch.setattr(
        translation_server,
        "_check_rate_limit",
        lambda: (_ for _ in ()).throw(AssertionError("cloud limiter was used")),
    )
    monkeypatch.setattr(
        translation_server,
        "_translate_via_ai",
        lambda text, source_lang, target_lang, provider: "离线译文",
    )
    monkeypatch.setattr(translation_server, "_mark_persistent_dirty", lambda: None)

    assert translation_server._cached_translate(
        "速度测试", "ja", "zh-CN", "hy_mt2"
    ) == "离线译文"


class _FakeHttpResponse:
    """只暴露 _response_resumed_from 会读到的两个属性。"""

    def __init__(self, status: int, content_range: str | None = None) -> None:
        self.status = status
        self.headers = {} if content_range is None else {"Content-Range": content_range}


def test_resume_accepts_200_with_matching_content_range():
    """ModelScope 用 200 而不是 206 回复 Range 请求，但区间正确，应视为续传。

    只看 206 会把这种情况误判为"服务器不支持续传"，于是丢弃已下载的分片从头再来，
    4.3 GB 的模型在弱网下每次断线都要重下。
    """
    response = _FakeHttpResponse(200, "bytes 1000000-4624648799/4624648800")
    assert _response_resumed_from(response, 1_000_000) is True


def test_resume_accepts_standard_206():
    response = _FakeHttpResponse(206, "bytes 512-999/1000")
    assert _response_resumed_from(response, 512) is True


def test_resume_rejects_server_that_ignored_range():
    """服务器忽略 Range 并回传整个文件时必须重下，否则会把两份数据拼成坏文件。"""
    assert _response_resumed_from(_FakeHttpResponse(200), 1_000_000) is False
    assert _response_resumed_from(
        _FakeHttpResponse(200, "bytes 0-999/1000"), 1_000_000
    ) is False


def test_resume_rejects_content_range_with_wrong_offset():
    assert _response_resumed_from(
        _FakeHttpResponse(206, "bytes 0-999/1000"), 512
    ) is False
