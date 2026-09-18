from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from engines.base import TextItem
from translators import deepseek as deepseek_module
from translators import cache as cache_module
from translators import deepseek_runtime_mixin
from translators.cache import TranslationCache
from translators.deepseek import DeepSeekTranslator, _Batch, _PendingItem
from translators.deepseek_batch_json import parse_partial_translation_map


def make_cache(tmp_path: Path) -> TranslationCache:
    return TranslationCache(tmp_path / "cache.db")


@pytest.fixture(autouse=True)
def _isolate_deepseek_model_config(monkeypatch):
    """Cache-key tests must not read the developer's live model selection."""
    config = SimpleNamespace(
        deepseek_api_key="",
        openai_api_key="",
        deepseek_model=DeepSeekTranslator.MODEL,
        max_concurrency=1,
    )
    monkeypatch.setattr(deepseek_module, "get_config", lambda: config)
    monkeypatch.setattr(deepseek_runtime_mixin, "get_config", lambda: config)


def test_v2_exact_hit_same_type_and_prompt(tmp_path):
    cache = make_cache(tmp_path)
    cache.set_v2(
        "導入シーンをスキップしますか?",
        "要跳过导入场景吗？",
        "ja",
        "zh-CN",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="game_ja_zh_v2",
        text_type="choice",
    )

    hit = cache.lookup(
        "導入シーンをスキップしますか?",
        "ja",
        "zh-CN",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="game_ja_zh_v2",
        text_type="choice",
    )

    assert hit.hit
    assert hit.translated == "要跳过导入场景吗？"


def test_cache_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_module, "_cache_runtime_settings", lambda: (False, True, 1024 * 1024 * 1024))
    cache = make_cache(tmp_path)

    cache.set_v2(
        "導入シーンをスキップしますか?",
        "要跳过导入场景吗？",
        "ja",
        "zh-CN",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="game_ja_zh_v2",
        text_type="choice",
    )
    hit = cache.lookup(
        "導入シーンをスキップしますか?",
        "ja",
        "zh-CN",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="game_ja_zh_v2",
        text_type="choice",
    )

    assert cache.stats.cache_enabled is False
    assert not hit.hit
    assert hit.translated is None
    assert cache.stats.cache_exact_hit == 0
    assert cache.stats.cache_miss == 0
    assert cache._conn.execute("SELECT COUNT(*) FROM cache_v2").fetchone()[0] == 0


def test_cache_auto_cleanup_prunes_old_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_module, "_cache_runtime_settings", lambda: (True, True, 1))
    cache = make_cache(tmp_path)
    for idx in range(120):
        cache.set_v2(
            f"テキスト{idx}",
            f"译文{idx}",
            "ja",
            "zh-CN",
            provider="deepseek",
            model="deepseek-v4-flash",
            prompt_version="game_ja_zh_v2",
            text_type="message",
        )

    before_rows = cache._conn.execute("SELECT COUNT(*) FROM cache_v2").fetchone()[0]
    cache._maybe_auto_cleanup(force=True)
    after_rows = cache._conn.execute("SELECT COUNT(*) FROM cache_v2").fetchone()[0]

    assert before_rows == 120
    assert after_rows < before_rows
    assert cache.stats.cache_cleanup_count >= 1
    assert cache.stats.cache_pruned_rows > 0


def test_v2_separates_message_and_choice(tmp_path):
    cache = make_cache(tmp_path)
    cache.set_v2(
        "いいですか?",
        "可以吗？",
        "ja",
        "zh-CN",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="game_ja_zh_v2",
        text_type="message",
    )

    miss = cache.lookup(
        "いいですか?",
        "ja",
        "zh-CN",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="game_ja_zh_v2",
        text_type="choice",
        allow_legacy=False,
    )

    assert not miss.hit
    assert miss.translated is None


def test_v2_separates_prompt_and_model(tmp_path):
    cache = make_cache(tmp_path)
    cache.set_v2(
        "月が綺麗ですね。",
        "今晚的月色真美。",
        "ja",
        "zh-CN",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="game_ja_zh_v2",
        text_type="message",
    )

    assert not cache.lookup(
        "月が綺麗ですね。",
        "ja",
        "zh-CN",
        provider="deepseek",
        model="deepseek-chat",
        prompt_version="game_ja_zh_v2",
        text_type="message",
        allow_legacy=False,
    ).hit
    assert not cache.lookup(
        "月が綺麗ですね。",
        "ja",
        "zh-CN",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="game_ja_zh_v3",
        text_type="message",
        allow_legacy=False,
    ).hit


def test_normalization_does_not_mutate_original_storage(tmp_path):
    cache = make_cache(tmp_path)
    original = "  導入シーンを  スキップしますか?\r\n"
    cache.set_v2(
        original,
        "要跳过导入场景吗？",
        "ja",
        "zh-CN",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="game_ja_zh_v2",
        text_type="choice",
    )

    hit = cache.lookup(
        "導入シーンを スキップしますか?\n",
        "ja",
        "zh-CN",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="game_ja_zh_v2",
        text_type="choice",
        allow_legacy=False,
    )

    assert hit.hit
    assert hit.translated == "要跳过导入场景吗？"
    row = cache._conn.execute("SELECT original FROM cache_v2").fetchone()
    assert row[0] == original


def test_does_not_cache_empty_or_source_equal(tmp_path):
    cache = make_cache(tmp_path)
    cache.set_v2(
        "はい",
        "はい",
        "ja",
        "zh-CN",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="game_ja_zh_v2",
        text_type="message",
    )
    cache.set_v2(
        "いいえ",
        "",
        "ja",
        "zh-CN",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="game_ja_zh_v2",
        text_type="message",
    )

    count = cache._conn.execute("SELECT COUNT(*) FROM cache_v2").fetchone()[0]
    assert count == 0


def test_infer_text_type_from_item_meta():
    assert TranslationCache.infer_text_type(TextItem(file="", original="", context="choice")) == "choice"
    assert TranslationCache.infer_text_type(TextItem(file="", original="", context="message")) == "message"
    assert TranslationCache.infer_text_type(TextItem(file="", original="", context="", meta={"kind": "name"})) == "name"


@pytest.mark.anyio
async def test_deepseek_all_cache_hits_skip_api(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    items = [
        TextItem(file="a", key="1", original="月が綺麗ですね。", context="message"),
        TextItem(file="a", key="2", original="導入シーンをスキップしますか?", context="choice"),
    ]
    cache.set_v2(
        items[0].original,
        "今晚的月色真美。",
        "ja",
        "zh-CN",
        provider="deepseek",
        model=DeepSeekTranslator.MODEL,
        prompt_version=DeepSeekTranslator.PROMPT_VERSION,
        text_type="message",
    )
    cache.set_v2(
        items[1].original,
        "要跳过导入场景吗？",
        "ja",
        "zh-CN",
        provider="deepseek",
        model=DeepSeekTranslator.MODEL,
        prompt_version=DeepSeekTranslator.PROMPT_VERSION,
        text_type="choice",
    )
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)

    result = await DeepSeekTranslator().translate_batch(items, "ja", "zh-CN")

    assert [item.translated for item in result] == ["今晚的月色真美。", "要跳过导入场景吗？"]
    assert cache.stats.api_request_count == 0
    assert cache.stats.cache_exact_hit == 2


def test_structured_batch_parser_accepts_strict_json():
    translator = DeepSeekTranslator()
    parsed = translator._parse_batch_json('[{"id":1,"t":"你好"},{"id":2,"t":"再见"}]', [1, 2])
    assert parsed == {1: "你好", 2: "再见"}
    parsed_obj = translator._parse_batch_json('{"items":[{"id":1,"t":"你好"},{"id":2,"t":"再见"}]}', [1, 2])
    assert parsed_obj == {1: "你好", 2: "再见"}
    parsed_map = translator._parse_batch_json('{"t":{"1":"你好","2":"再见"}}', [1, 2])
    assert parsed_map == {1: "你好", 2: "再见"}


def test_structured_batch_parser_recovers_complete_rows_from_truncated_map():
    raw = '{"t":{"1":"你好","2":"带有\\\"引号\\\"的译文","3":"未完成'

    assert parse_partial_translation_map(raw, [1, 2, 3]) == {
        1: "你好",
        2: '带有"引号"的译文',
    }


@pytest.mark.parametrize(
    "raw",
    [
        "```json\n[{\"id\":1,\"t\":\"你好\"}]\n```",
        '[{"id":1,"t":"你好","x":1}]',
        '[{"id":1,"t":"你好"},{"id":1,"t":"重复"}]',
        '[{"id":2,"t":"缺失"}]',
        '```json\n{"items":[{"id":1,"t":"你好"}]}\n```',
        '{"t":{"1":"你好"},"extra":true}',
        '{"t":{"x":"你好"}}',
    ],
)
def test_structured_batch_parser_rejects_unstable_output(raw):
    with pytest.raises(ValueError):
        DeepSeekTranslator()._parse_batch_json(raw, [1])


def test_structured_batch_parser_extracts_unique_json_payload():
    raw = '当然可以：{"items":[{"id":1,"t":"你好"},{"id":2,"t":"再见"}]}'
    parsed = DeepSeekTranslator()._parse_batch_json(raw, [1, 2])
    assert parsed == {1: "你好", 2: "再见"}


def test_structured_batch_preserves_order_without_complexity_partition(tmp_path):
    cache = make_cache(tmp_path)
    translator = DeepSeekTranslator()
    pending = []
    for i in range(35):
        item = TextItem(file="arc::entry1", key=str(i), original=f"普通の台詞です。{i}", context="message", meta={"entry": "entry1"})
        pending.append(_PendingItem(item, f"k{i}", "message", "p"))
    complex_item = TextItem(
        file="arc::entry1",
        key="complex",
        original="長い台詞" * 90,
        context="message",
        meta={"entry": "entry1"},
    )
    pending.append(_PendingItem(complex_item, "kc", "message", "p"))
    choice_item = TextItem(file="arc::entry2", key="choice", original="はい", context="choice", meta={"entry": "entry2"})
    pending.append(_PendingItem(choice_item, "choice", "choice", "p"))

    batches = translator._make_batches(pending, cache)

    flattened = [item.cache_key for batch in batches for item in batch.items]
    assert flattened == [item.cache_key for item in pending]
    assert all(not batch.complex for batch in batches)
    assert all(
        translator._estimate_output_tokens(b.items, cache) <= translator.MAX_OUTPUT_TOKENS
        for b in batches
    )
    assert batches[-1].text_type == "mixed"


def test_placeholder_text_does_not_trigger_complex_batch(tmp_path):
    cache = make_cache(tmp_path)
    translator = DeepSeekTranslator()
    pending = [
        _PendingItem(
            TextItem(file="arc::entry", key=str(i), original=f"value %s {i}", context="message"),
            f"k{i}",
            "message",
            "p",
        )
        for i in range(18)
    ]

    batches = translator._make_batches(pending, cache)

    assert len(batches) == 1
    assert batches[0].complex is False
    assert len(batches[0].items) == 18


def test_control_and_multiline_text_stays_in_normal_batch(tmp_path):
    cache = make_cache(tmp_path)
    translator = DeepSeekTranslator()
    pending = [
        _PendingItem(
            TextItem(file="arc::entry", key=str(i), original=f"line %s\nnext {{name}} {i}", context="message"),
            f"k{i}",
            "message",
            "p",
        )
        for i in range(21)
    ]

    batches = translator._make_batches(pending, cache)

    assert [len(batch.items) for batch in batches] == [21]
    assert all(not batch.complex for batch in batches)


def test_structured_batch_packs_small_entries_by_type_not_entry(tmp_path):
    cache = make_cache(tmp_path)
    translator = DeepSeekTranslator()
    pending = []
    for i in range(100):
        item = TextItem(
            file=f"arc::entry{i:03d}",
            key=str(i),
            original=f"短い台詞です。{i}",
            context="message",
            meta={"entry": f"entry{i:03d}"},
        )
        pending.append(_PendingItem(item, f"k{i}", "message", "p"))

    batches = translator._make_batches(pending, cache)

    assert len(batches) <= 4
    assert sum(len(b.items) for b in batches) == 100
    assert all(b.text_type == "message" for b in batches)
    assert all(
        translator._estimate_output_tokens(b.items, cache) <= translator.MAX_OUTPUT_TOKENS
        for b in batches
    )
    assert all(
        translator._batch_input_tokens(b, cache, "ja", "zh-CN") <= translator.MAX_INPUT_TOKENS
        for b in batches
    )


def test_short_items_batch_by_content_not_fixed_count(tmp_path):
    cache = make_cache(tmp_path)
    translator = DeepSeekTranslator()
    pending = [
        _PendingItem(
            TextItem(file="arc::entry", key=str(i), original=f"Line {i}.", context="message"),
            f"k{i}",
            "message",
            "p",
        )
        for i in range(220)
    ]

    batches = translator._make_batches(pending, cache, "en", "zh-CN")

    # 按内容量动态分批：不再固定 100 条/批，但受 MAX_BATCH_ITEMS 兜底。
    # 220 条短文本 → 150 + 70 两批，输入/输出预算都不应被突破。
    assert [len(b.items) for b in batches] == [150, 70]
    assert translator.MAX_INPUT_TOKENS == 32768
    assert translator.MAX_OUTPUT_TOKENS == 16384
    assert translator.MAX_BATCH_ITEMS == 150
    assert all(
        translator._batch_input_tokens(b, cache, "en", "zh-CN") <= translator.MAX_INPUT_TOKENS
        for b in batches
    )
    assert all(
        translator._estimate_output_tokens(b.items, cache) <= translator.MAX_OUTPUT_TOKENS
        for b in batches
    )


def test_mixed_batch_prompt_keeps_source_order_and_type_markers(tmp_path):
    cache = make_cache(tmp_path)
    translator = DeepSeekTranslator()
    pending = [
        _PendingItem(TextItem(file="a", key="1", original="こんにちは", context="message"), "k1", "message", ""),
        _PendingItem(TextItem(file="a", key="2", original="はい", context="choice"), "k2", "choice", ""),
        _PendingItem(TextItem(file="a", key="3", original="春香", context="name"), "k3", "name", ""),
    ]

    batch = translator._make_batches(pending, cache)[0]
    prompt = translator._build_batch_prompt(batch, "ja", "zh-CN")

    assert batch.text_type == "mixed"
    assert '[[1,"こんにちは","message"],[2,"はい","choice"],[3,"春香","name"]]' in prompt
    assert "第三项是文本类型" in prompt


def test_primary_batching_never_calls_complexity_classifier(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    translator = DeepSeekTranslator()
    pending = [
        _PendingItem(
            TextItem(file="a", key=str(idx), original=f"短い台詞{idx}", context="message"),
            f"k{idx}",
            "message",
            "",
        )
        for idx in range(250)
    ]

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("primary batching must not run complexity analysis")

    monkeypatch.setattr(translator, "_is_complex_item", fail_if_called)

    batches = translator._make_batches(pending, cache)

    # 主批次不做复杂度分析；250 条短文本由内容量动态分批（不固定 100 条），
    # 受 MAX_BATCH_ITEMS 兜底 → 150 + 100 两批
    assert [len(batch.items) for batch in batches] == [150, 100]
    assert all(
        translator._estimate_output_tokens(b.items, cache) <= translator.MAX_OUTPUT_TOKENS
        for b in batches
    )


def test_45888_short_items_batch_in_one_pass_under_ten_seconds(tmp_path):
    cache = make_cache(tmp_path)
    translator = DeepSeekTranslator()
    pending = [
        _PendingItem(
            TextItem(file="Map001.json", key=str(idx), original=f"会話テキスト{idx}", context="message"),
            f"k{idx}",
            "message",
            "",
        )
        for idx in range(45_888)
    ]

    started = time.perf_counter()
    iterator = translator._iter_batches(pending, cache)
    first_batch = next(iterator)
    first_batch_seconds = time.perf_counter() - started
    batches = [first_batch, *iterator]
    total_seconds = time.perf_counter() - started

    # 批次大小由内容量动态决定（短文本批大、略长文本批小），受 MAX_BATCH_ITEMS
    # 兜底（150 条/批），不再固定 100 条、也不再撑到 300+ 条的不可靠区间
    first_size = len(first_batch.items)
    assert first_size == 150
    assert all(
        translator._estimate_output_tokens(b.items, cache) <= translator.MAX_OUTPUT_TOKENS
        for b in batches
    )
    # 内容量变化会反映为批次大小变化：150 为主，末批 138（尾部数字长度差异）
    assert len({len(b.items) for b in batches}) > 1
    assert sum(len(batch.items) for batch in batches) == 45_888
    assert first_batch_seconds < 1.0
    assert total_seconds < 10.0


@pytest.mark.anyio
async def test_streaming_translation_starts_before_all_batches_are_produced(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    config = SimpleNamespace(
        deepseek_api_key="test-key",
        openai_api_key="",
        deepseek_model="deepseek-v4-flash",
        max_concurrency=2,
    )
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    monkeypatch.setattr(deepseek_module, "get_config", lambda: config)
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **_kwargs: object())

    translator = DeepSeekTranslator()
    items = [
        TextItem(file="Map001.json", key=str(idx), original=f"台詞{idx}です", context="message")
        for idx in range(3000)
    ]
    original_iter_batches = translator._iter_batches
    state = {"produced": 0, "produced_when_first_request_started": None}

    def tracked_batches(*args, **kwargs):
        for batch in original_iter_batches(*args, **kwargs):
            state["produced"] += 1
            yield batch

    async def fake_translate(batch, _client, _source_lang, _target_lang):
        if state["produced_when_first_request_started"] is None:
            state["produced_when_first_request_started"] = state["produced"]
        await __import__("asyncio").sleep(0)
        for pending in batch.items:
            pending.item.translated = f"译文{pending.item.key}"
        return [pending.item for pending in batch.items]

    monkeypatch.setattr(translator, "_iter_batches", tracked_batches)
    monkeypatch.setattr(translator, "_translate_structured_batch", fake_translate)

    translated = await translator.translate_batch(items, "ja", "zh-CN")

    # 3000 条短文本按输出预算分成多批（不再固定 100 条/批），
    # 且首个 API 请求在全部批次产出前就开始（流式生产语义保留）。
    assert state["produced"] > 1
    assert state["produced_when_first_request_started"] < state["produced"]
    assert cache.stats.produced_batch_count == state["produced"]
    assert cache.stats.first_batch_ready_seconds < 1.0
    assert all(item.translated for item in translated)


@pytest.mark.anyio
async def test_translation_progress_counts_cache_hits_and_duplicate_occurrences(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    config = SimpleNamespace(
        deepseek_api_key="test-key",
        openai_api_key="",
        deepseek_model="deepseek-v4-flash",
        max_concurrency=1,
    )
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    monkeypatch.setattr(deepseek_module, "get_config", lambda: config)
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **_kwargs: object())
    cache.set_v2(
        "キャッシュ済みです。",
        "已缓存。",
        "ja",
        "zh-CN",
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version=DeepSeekTranslator.PROMPT_VERSION,
        text_type="message",
    )
    items = [
        TextItem(file="a", key="1", original="キャッシュ済みです。", context="message"),
        TextItem(file="a", key="2", original="同じ台詞です。", context="message"),
        TextItem(file="a", key="3", original="同じ台詞です。", context="message"),
    ]
    progress: list[tuple[int, int]] = []
    translator = DeepSeekTranslator()

    async def fake_translate(batch, _client, _source_lang, _target_lang):
        for pending in batch.items:
            pending.item.translated = "相同的台词。"
        return [pending.item for pending in batch.items]

    monkeypatch.setattr(translator, "_translate_structured_batch", fake_translate)
    await translator.translate_batch(items, "ja", "zh-CN", on_progress=lambda cur, total: progress.append((cur, total)))

    assert progress[0] == (1, 3)
    assert progress[-1] == (3, 3)


def test_streaming_timing_stats_serialize_and_reset(tmp_path):
    cache = make_cache(tmp_path)
    cache.stats.produced_batch_count = 459
    cache.stats.first_batch_ready_seconds = 0.125
    cache.stats.first_api_request_seconds = 0.25

    data = cache.stats_dict()

    assert data["produced_batch_count"] == 459
    assert data["first_batch_ready_seconds"] == 0.125
    assert data["first_api_request_seconds"] == 0.25

    cache.reset_stats(
        provider="deepseek",
        model="deepseek-v4-flash",
        prompt_version="test",
        run_id="run-cache-reset-test",
    )

    assert cache.stats.produced_batch_count == 0
    assert cache.stats.first_batch_ready_seconds == 0.0
    assert cache.stats.first_api_request_seconds == 0.0
    assert cache.stats.run_id == "run-cache-reset-test"


def test_high_concurrency_does_not_force_complexity_batches(tmp_path):
    cache = make_cache(tmp_path)
    translator = DeepSeekTranslator()
    pending = [
        _PendingItem(
            TextItem(file="arc::entry", key=str(i), original=f"line %s\nnext {{name}} {i}", context="message"),
            f"k{i}",
            "message",
            "p",
        )
        for i in range(25)
    ]

    batches = translator._make_batches(pending, cache, "en", "zh-CN", concurrency=100)

    assert [len(b.items) for b in batches] == [25]
    assert all(not b.complex for b in batches)


def test_batch_prompt_uses_compact_mode_for_plain_dialogue(tmp_path):
    cache = make_cache(tmp_path)
    translator = DeepSeekTranslator()
    item = TextItem(
        file="arc::entry",
        key="1",
        original="普通の台詞です。",
        context="message",
        meta={"entry": "entry", "prev_text": "前の行", "next_text": "次の行"},
    )
    batch = _Batch([_PendingItem(item, "k", "message", "p")], text_type="message", complex=False)

    prompt = translator._build_batch_prompt(batch, "ja", "zh-CN")

    assert translator._batch_mode(batch) == "compact"
    assert '[[1,"普通の台詞です。"]]' in prompt
    assert "前の行" not in prompt
    assert "次の行" not in prompt


def test_batch_prompt_keeps_context_for_choices_not_complexity(tmp_path):
    translator = DeepSeekTranslator()
    complex_item = TextItem(
        file="arc::entry",
        key="1",
        original="名前は%sです。",
        context="message",
        meta={"prev_text": "前の行", "next_text": "次の行"},
    )
    choice_item = TextItem(
        file="arc::choice",
        key="2",
        original="はい",
        context="choice",
        meta={"prev_text": "質問", "next_text": "分岐"},
    )
    complex_batch = _Batch([_PendingItem(complex_item, "k1", "message", "p")], text_type="message", complex=True)
    choice_batch = _Batch([_PendingItem(choice_item, "k2", "choice", "p")], text_type="choice", complex=False)

    complex_prompt = translator._build_batch_prompt(complex_batch, "ja", "zh-CN")
    choice_prompt = translator._build_batch_prompt(choice_batch, "ja", "zh-CN")

    assert translator._batch_mode(complex_batch) == "compact"
    assert translator._batch_mode(choice_batch) == "contextual"
    assert "前の行" not in complex_prompt
    assert "次の行" not in complex_prompt
    assert '"p":"質問"' in choice_prompt
    assert '"n":"分岐"' in choice_prompt
    assert "剧情选项" in choice_prompt


def test_rpgmaker_message_contract_uses_a_separate_api_cache_key(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)
    translator = DeepSeekTranslator()
    original = "\\n<\u304a\u3058\u3058>\u300c\u5916\u306e\u4e16\u754c\u306e\u8a71\u300d"
    cache.set_v2(
        original,
        "\u65e7\u7248\u9519\u4f4d\u8bd1\u6587",
        "ja",
        "zh-CN",
        provider=translator.name,
        model=translator._model(),
        prompt_version=translator.PROMPT_VERSION,
        text_type="message",
    )
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    item = TextItem(
        file="hook",
        original=original,
        context="CmEv.1.line",
        meta={"translation_cache_scope": "rpgmaker_message_v2"},
    )

    lookup = translator._cache_lookup_for_item(item, "ja", "zh-CN")

    assert lookup.translated is None
    assert translator._cache_text_type(item, "message") == "message@rpgmaker_message_v2"


def test_bgi_ruby_batch_prompt_uses_visible_text_for_quality_and_cost(tmp_path):
    cache = make_cache(tmp_path)
    translator = DeepSeekTranslator()
    item = TextItem(
        file="data01110.arc::pg00_com",
        key="1",
        original="『<Rあき>安芸</R> かのこ』小学校のときからずっと同じクラスな上、家族ぐるみでも親交のある幼馴染。",
        context="message",
        meta={"arc": "data01110.arc", "archive_kind": "arc20", "entry": "pg00_com"},
    )
    assert not translator._is_complex_item(item, cache)
    batch = _Batch([_PendingItem(item, "k", "message", "p")], text_type="message", complex=False)

    prompt = translator._build_batch_prompt(batch, "ja", "zh-CN")

    assert translator._batch_mode(batch) == "compact"
    assert "<R" not in prompt
    assert "</R>" not in prompt


def test_short_bgi_ruby_text_can_use_normal_message_batch(tmp_path):
    cache = make_cache(tmp_path)
    translator = DeepSeekTranslator()
    item = TextItem(
        file="data01000.arc::scene",
        key="1",
        original="<Rはるか>遥か</R>の声が聞こえる。",
        context="message",
        meta={"arc": "data01000.arc", "entry": "scene"},
    )

    pending = [_PendingItem(item, "k", "message", "p")]
    batches = translator._make_batches(pending, cache, "ja", "zh-CN")

    assert len(batches) == 1
    assert batches[0].complex is False
    prompt = translator._build_batch_prompt(batches[0], "ja", "zh-CN")
    assert "<R" not in prompt
    assert "</R>" not in prompt
    assert "はるか" not in prompt
    assert "遥かの声が聞こえる。" in prompt


def test_bgi_ruby_cache_key_uses_visible_translation_source(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    item = TextItem(
        file="data01110.arc::pg00_com",
        key="1",
        original="送信者の名前は、『<rギフトアップル>ＧｉｆｔＡｐｆｅｌ</r>』。",
        context="message",
        meta={"arc": "data01110.arc", "archive_kind": "arc20", "entry": "pg00_com"},
    )
    cache.set_v2(
        "送信者の名前は、『ＧｉｆｔＡｐｆｅｌ』。",
        "发信人的名字是『ＧｉｆｔＡｐｆｅｌ』。",
        "ja",
        "zh-CN",
        provider="deepseek",
        model=DeepSeekTranslator.MODEL,
        prompt_version=DeepSeekTranslator.PROMPT_VERSION,
        text_type="message",
    )

    pending, duplicates = DeepSeekTranslator()._collect_pending([item], "ja", "zh-CN")

    assert pending == []
    assert duplicates == 0
    assert item.translated == "发信人的名字是『ＧｉｆｔＡｐｆｅｌ』。"
    assert cache.stats.cache_exact_hit == 1


def test_structured_batch_dynamic_max_tokens(tmp_path):
    cache = make_cache(tmp_path)
    translator = DeepSeekTranslator()
    short = _Batch(
        [_PendingItem(TextItem(file="", original="短い"), "k", "message", "p")],
        text_type="message",
        complex=False,
    )
    long_complex = _Batch(
        [_PendingItem(TextItem(file="", original="長い" * 1000), "k", "message", "p")],
        text_type="message",
        complex=True,
    )

    assert translator._batch_max_tokens(short, cache) == 256
    assert translator._batch_max_tokens(long_complex, cache) == 2048


class _FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


class _FakeCompletions:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self.outputs.pop(0))


class _FakeChat:
    def __init__(self, outputs):
        self.completions = _FakeCompletions(outputs)


class _FakeClient:
    def __init__(self, outputs):
        self.chat = _FakeChat(outputs)


class _FailingCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        raise RuntimeError("Error code: 401 - authentication_error invalid api key")


class _FailingChat:
    def __init__(self):
        self.completions = _FailingCompletions()


class _FailingClient:
    def __init__(self):
        self.chat = _FailingChat()


class _RateLimitCompletions:
    def __init__(self):
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        raise RuntimeError("Error code: 429 - rate limit exceeded")


class _RateLimitChat:
    def __init__(self):
        self.completions = _RateLimitCompletions()


class _RateLimitClient:
    def __init__(self):
        self.chat = _RateLimitChat()


@pytest.mark.anyio
async def test_structured_batch_translates_and_propagates_duplicates(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    items = [
        TextItem(file="a::e", key="1", original="月が綺麗ですね。", context="message", meta={"entry": "e"}),
        TextItem(file="a::e", key="2", original="月が綺麗ですね。", context="message", meta={"entry": "e"}),
        TextItem(file="a::e", key="3", original="また明日。", context="message", meta={"entry": "e"}),
    ]
    client = _FakeClient(['[{"id":1,"t":"今晚的月色真美。"},{"id":2,"t":"明天见。"}]'])
    pending, duplicates = DeepSeekTranslator()._collect_pending(items, "ja", "zh-CN")
    batches = DeepSeekTranslator()._make_batches(pending, cache)

    translated = await DeepSeekTranslator()._translate_structured_batch(batches[0], client, "ja", "zh-CN")
    DeepSeekTranslator()._propagate_by_cache_key(items, pending, "ja", "zh-CN")

    assert duplicates == 1
    assert len(translated) == 2
    assert [i.translated for i in items] == ["今晚的月色真美。", "今晚的月色真美。", "明天见。"]
    assert cache.stats.batch_request_count == 1
    assert cache.stats.compact_batch_request_count == 1
    assert cache.stats.compact_batch_item_count == 2
    assert cache.stats.contextual_batch_request_count == 0
    assert cache.stats.api_request_count == 1
    assert client.chat.completions.calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.anyio
async def test_structured_batch_retries_strict_json_once(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    items = [
        TextItem(file="a::e", key="1", original="月が綺麗ですね。", context="message", meta={"entry": "e"}),
        TextItem(file="a::e", key="2", original="また明日。", context="message", meta={"entry": "e"}),
    ]
    batch = _Batch(
        [_PendingItem(item, f"k{i}", "message", "p") for i, item in enumerate(items)],
        text_type="message",
        complex=False,
    )
    client = _FakeClient([
        "```json\n[]\n```",
        '[{"id":1,"t":"今晚的月色真美。"},{"id":2,"t":"明天见。"}]',
    ])

    await DeepSeekTranslator()._translate_structured_batch(batch, client, "ja", "zh-CN")

    assert [i.translated for i in items] == ["今晚的月色真美。", "明天见。"]
    assert cache.stats.json_parse_fail_count == 1
    assert cache.stats.batch_retry_count == 1
    assert cache.stats.batch_request_count == 2
    assert cache.stats.api_request_count == 2


@pytest.mark.anyio
async def test_structured_batch_reuses_truncated_prefix_and_only_retries_tail(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    items = [
        TextItem(file="a::e", key="1", original="月が綺麗ですね。", context="message"),
        TextItem(file="a::e", key="2", original="また明日。", context="message"),
        TextItem(file="a::e", key="3", original="おやすみ。", context="message"),
    ]
    batch = _Batch(
        [_PendingItem(item, f"k{i}", "message", "p") for i, item in enumerate(items)],
        text_type="message",
        complex=False,
    )
    client = _FakeClient([
        '{"t":{"1":"今晚的月色真美。","2":"明天见。","3":"未完成',
        "晚安。",
    ])

    await DeepSeekTranslator()._translate_structured_batch(batch, client, "ja", "zh-CN")

    assert [item.translated for item in items] == ["今晚的月色真美。", "明天见。", "晚安。"]
    assert cache.stats.partial_batch_recovered_count == 1
    assert cache.stats.partial_batch_recovered_item_count == 2
    assert cache.stats.batch_retry_count == 0
    assert cache.stats.api_request_count == 2


@pytest.mark.anyio
async def test_complex_flag_no_longer_forces_contextual_batch_stats(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    item = TextItem(
        file="a::e",
        key="1",
        original="名前は%sです。",
        context="message",
        meta={"entry": "e", "prev_text": "自己紹介", "next_text": "よろしく"},
    )
    batch = _Batch([_PendingItem(item, "k", "message", "p")], text_type="message", complex=True)
    client = _FakeClient(['{"t":{"1":"名字是%s。"}}'])

    await DeepSeekTranslator()._translate_structured_batch(batch, client, "ja", "zh-CN")

    assert item.translated == "名字是%s。"
    assert cache.stats.batch_request_count == 1
    assert cache.stats.compact_batch_request_count == 1
    assert cache.stats.compact_batch_item_count == 1
    assert cache.stats.contextual_batch_request_count == 0
    assert cache.stats.contextual_batch_item_count == 0


@pytest.mark.anyio
async def test_structured_batch_splits_then_falls_back_to_single(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    items = [
        TextItem(file="a::e", key="1", original="月が綺麗ですね。", context="message", meta={"entry": "e"}),
        TextItem(file="a::e", key="2", original="また明日。", context="message", meta={"entry": "e"}),
    ]
    batch = _Batch(
        [_PendingItem(item, f"k{i}", "message", "p") for i, item in enumerate(items)],
        text_type="message",
        complex=False,
    )
    client = _FakeClient(["not json", "still not json", "今晚的月色真美。", "明天见。"])

    await DeepSeekTranslator()._translate_structured_batch(batch, client, "ja", "zh-CN")

    assert [i.translated for i in items] == ["今晚的月色真美。", "明天见。"]
    assert cache.stats.batch_split_count == 1
    assert cache.stats.single_fallback_count == 2
    assert cache.stats.api_request_count == 4


@pytest.mark.anyio
async def test_structured_batch_fatal_api_error_does_not_split(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    items = [
        TextItem(file="a::e", key="1", original="月が綺麗ですね。", context="message", meta={"entry": "e"}),
        TextItem(file="a::e", key="2", original="また明日。", context="message", meta={"entry": "e"}),
    ]
    batch = _Batch(
        [_PendingItem(item, f"k{i}", "message", "p") for i, item in enumerate(items)],
        text_type="message",
        complex=False,
    )
    client = _FailingClient()

    with pytest.raises(RuntimeError, match="鉴权失败"):
        await DeepSeekTranslator()._translate_structured_batch(batch, client, "ja", "zh-CN")

    assert client.chat.completions.calls == 1
    assert cache.stats.batch_request_count == 1
    assert cache.stats.batch_split_count == 0
    assert cache.stats.single_fallback_count == 0


@pytest.mark.anyio
async def test_structured_batch_rate_limit_does_not_split_or_single_fallback(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    items = [
        TextItem(file="a::e", key="1", original="月が綺麗ですね。", context="message", meta={"entry": "e"}),
        TextItem(file="a::e", key="2", original="また明日。", context="message", meta={"entry": "e"}),
    ]
    batch = _Batch(
        [_PendingItem(item, f"k{i}", "message", "p") for i, item in enumerate(items)],
        text_type="message",
        complex=False,
    )
    client = _RateLimitClient()

    await DeepSeekTranslator()._translate_structured_batch(batch, client, "ja", "zh-CN")

    assert [i.translated for i in items] == [i.original for i in items]
    assert client.chat.completions.calls == DeepSeekTranslator.RETRY_MAX_RETRIES + 1
    assert cache.stats.batch_request_count == 1
    assert cache.stats.batch_split_count == 0
    assert cache.stats.single_fallback_count == 0
    assert cache.stats.api_request_count == 0


@pytest.mark.anyio
async def test_structured_batch_shape_error_splits_without_strict_retry(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    items = [
        TextItem(file="a::e", key="1", original="月が綺麗ですね。", context="message", meta={"entry": "e"}),
        TextItem(file="a::e", key="2", original="また明日。", context="message", meta={"entry": "e"}),
    ]
    batch = _Batch(
        [_PendingItem(item, f"k{i}", "message", "p") for i, item in enumerate(items)],
        text_type="message",
        complex=False,
    )
    client = _FakeClient(['[{"id":1,"t":"今晚的月色真美。"}]', "今晚的月色真美。", "明天见。"])

    await DeepSeekTranslator()._translate_structured_batch(batch, client, "ja", "zh-CN")

    assert [i.translated for i in items] == ["今晚的月色真美。", "明天见。"]
    assert cache.stats.batch_retry_count == 0
    assert cache.stats.json_parse_fail_count == 0
    assert cache.stats.batch_split_count == 1
    assert cache.stats.single_fallback_count == 2


@pytest.mark.anyio
async def test_structured_batch_shape_mismatch_gap_fills_missing_ids(monkeypatch, tmp_path):
    """对象格式 id set mismatch 时回收已解析条目，只补齐缺失 id，不整批二分。"""
    cache = make_cache(tmp_path)
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    items = [
        TextItem(file="a::e", key="1", original="月が綺麗ですね。", context="message", meta={"entry": "e"}),
        TextItem(file="a::e", key="2", original="また明日。", context="message", meta={"entry": "e"}),
        TextItem(file="a::e", key="3", original="おやすみ。", context="message", meta={"entry": "e"}),
    ]
    batch = _Batch(
        [_PendingItem(item, f"k{i}", "message", "p") for i, item in enumerate(items)],
        text_type="message",
        complex=False,
    )
    # 首个请求缺 id 3 → 缺口补齐只重发缺失条目；数组格式无 "t":{ 时仍走二分
    client = _FakeClient(['{"t":{"1":"今晚的月色真美。","2":"明天见。"}}', "晚安。"])

    await DeepSeekTranslator()._translate_structured_batch(batch, client, "ja", "zh-CN")

    assert [i.translated for i in items] == ["今晚的月色真美。", "明天见。", "晚安。"]
    assert cache.stats.partial_batch_recovered_count == 1
    assert cache.stats.partial_batch_recovered_item_count == 2
    assert cache.stats.batch_split_count == 0
    assert cache.stats.json_parse_fail_count == 0
    assert cache.stats.api_request_count == 2


@pytest.mark.anyio
async def test_structured_batch_single_item_failure_uses_single_fallback(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    item = TextItem(file="a::e", key="1", original="月が綺麗ですね。", context="message", meta={"entry": "e"})
    batch = _Batch([_PendingItem(item, "k", "message", "p")], text_type="message", complex=False)
    client = _FakeClient(["not json", "still not json", "今晚的月色真美。"])

    await DeepSeekTranslator()._translate_structured_batch(batch, client, "ja", "zh-CN")

    assert item.translated == "今晚的月色真美。"
    assert cache.stats.batch_split_count == 0
    assert cache.stats.single_fallback_count == 1
    assert cache.stats.api_request_count == 3


@pytest.mark.anyio
async def test_structured_batch_validation_failure_only_retries_failed_item(monkeypatch, tmp_path):
    cache = make_cache(tmp_path)
    monkeypatch.setattr(deepseek_module, "get_cache", lambda: cache)
    items = [
        TextItem(file="a::e", key="1", original="月が綺麗ですね。", context="message", meta={"entry": "e"}),
        TextItem(file="a::e", key="2", original="名前は%sです。", context="message", meta={"entry": "e"}),
    ]
    batch = _Batch(
        [_PendingItem(item, f"k{i}", "message", "p") for i, item in enumerate(items)],
        text_type="message",
        complex=True,
    )
    client = _FakeClient([
        '[{"id":1,"t":"今晚的月色真美。"},{"id":2,"t":"名字是。"}]',
        "名字是%s。",
    ])

    await DeepSeekTranslator()._translate_structured_batch(batch, client, "ja", "zh-CN")

    assert [i.translated for i in items] == ["今晚的月色真美。", "名字是%s。"]
    assert cache.stats.validation_fail_count == 1
    assert cache.stats.single_fallback_count == 1
    assert cache.stats.api_request_count == 2
    assert cache.lookup(
        items[0].original,
        "ja",
        "zh-CN",
        provider="deepseek",
        model=DeepSeekTranslator.MODEL,
        prompt_version=DeepSeekTranslator.PROMPT_VERSION,
        text_type="message",
        allow_legacy=False,
        count_stats=False,
    ).translated == "今晚的月色真美。"
    assert cache.lookup(
        items[1].original,
        "ja",
        "zh-CN",
        provider="deepseek",
        model=DeepSeekTranslator.MODEL,
        prompt_version=DeepSeekTranslator.PROMPT_VERSION,
        text_type="message",
        allow_legacy=False,
        count_stats=False,
    ).translated == "名字是%s。"
