from core.realtime_translation_guard import realtime_translation_issue


def test_realtime_guard_rejects_empty_and_unchanged_japanese() -> None:
    assert realtime_translation_issue("こんにちは", "", "ja", "zh-CN") == "empty"
    assert realtime_translation_issue("こんにちは", "こんにちは", "ja", "zh-CN") == "unchanged_source"
    assert realtime_translation_issue("勝利", "勝利", "ja", "zh-CN") == "unchanged_source"


def test_realtime_guard_allows_database_tokens_and_translated_short_text() -> None:
    assert realtime_translation_issue("HP+10", "HP+10", "ja", "zh-CN") is None
    assert realtime_translation_issue("え？", "嗯？", "ja", "zh-CN") is None
    assert realtime_translation_issue("今日はいい天気です", "今天天气很好", "ja", "zh-CN") is None


def test_realtime_guard_rejects_mixed_japanese_residue() -> None:
    assert realtime_translation_issue("今日はいい天気です", "今天天気は很好", "ja", "zh-CN") == "japanese_output"


def test_realtime_guard_requires_punctuation_to_remain_unchanged() -> None:
    assert realtime_translation_issue("『…………』", "『嗯嗯嗯』") == "punctuation_only"
    assert realtime_translation_issue("『…………』", "『…………』") is None
    assert realtime_translation_issue("あれは……。", "那是……。") is None
