import struct
from pathlib import Path

from core.translation_overlay_window import (
    append_dialogue_history,
    decode_overlay_payload,
    format_dialogue_history_entry,
    runtime_dialogue_parts,
    split_speaker_text,
)


def test_split_speaker_text_from_kag_visible_quote() -> None:
    assert split_speaker_text("外村善治「今日はいい天気だ」") == ("外村善治", "今日はいい天気だ")


def test_split_speaker_text_from_colon_line() -> None:
    assert split_speaker_text("メルヴィ：もう大丈夫") == ("メルヴィ", "もう大丈夫")


def test_split_speaker_text_leaves_plain_dialogue_unchanged() -> None:
    assert split_speaker_text("「これは普通の台詞」") == ("", "「これは普通の台詞」")


def test_runtime_dialogue_parts_translate_body_only_for_dialogue_lines() -> None:
    assert runtime_dialogue_parts("外村善治「今日はいい天気だ」") == (
        "外村善治",
        "今日はいい天気だ",
        "今日はいい天気だ",
    )


def test_runtime_dialogue_parts_prefers_explicit_native_speaker() -> None:
    assert runtime_dialogue_parts("外村善治「今日はいい天気だ」", "真　鈴") == (
        "真鈴",
        "今日はいい天気だ",
        "今日はいい天気だ",
    )


def test_runtime_dialogue_parts_keeps_plain_dialogue_as_translation_key() -> None:
    assert runtime_dialogue_parts("「これは普通の台詞」", "翼") == (
        "翼",
        "「これは普通の台詞」",
        "「これは普通の台詞」",
    )


def _payload(seq: int, original: str, translated: str = "", speaker: str = "") -> bytes:
    original_bytes = original.encode("utf-8")
    translated_bytes = translated.encode("utf-8")
    speaker_bytes = speaker.encode("utf-8")
    data = bytearray(65536)
    offset = 0
    struct.pack_into("I", data, offset, seq)
    offset += 4
    struct.pack_into("I", data, offset, len(original_bytes))
    offset += 4
    data[offset:offset + len(original_bytes)] = original_bytes
    offset += len(original_bytes)
    struct.pack_into("I", data, offset, len(translated_bytes))
    offset += 4
    data[offset:offset + len(translated_bytes)] = translated_bytes
    offset += len(translated_bytes)
    if speaker:
        struct.pack_into("I", data, offset, len(speaker_bytes))
        offset += 4
        data[offset:offset + len(speaker_bytes)] = speaker_bytes
    return bytes(data)


def test_decode_overlay_payload_v1_without_speaker() -> None:
    assert decode_overlay_payload(_payload(7, "こんにちは", "你好")) == (7, "こんにちは", "你好", "")


def test_decode_overlay_payload_v2_with_speaker() -> None:
    assert decode_overlay_payload(_payload(8, "こんにちは", "你好", "真　鈴")) == (8, "こんにちは", "你好", "真鈴")


def test_dialogue_history_updates_pending_line_with_translation() -> None:
    history: list[dict] = []
    assert append_dialogue_history(
        history,
        {"speaker": "真　鈴", "original": "こんにちは", "translated": "", "timestamp": 1},
        limit=10,
    )
    assert append_dialogue_history(
        history,
        {"speaker": "真鈴", "original": "こんにちは", "translated": "你好", "timestamp": 2},
        limit=10,
    )

    assert len(history) == 1
    assert history[0]["speaker"] == "真鈴"
    assert history[0]["translated"] == "你好"


def test_dialogue_history_skips_immediate_duplicate_capture() -> None:
    history: list[dict] = []
    first = {"speaker": "", "original": "同じ行", "translated": "同一行", "timestamp": 1}
    duplicate = {"speaker": "", "original": "同じ行", "translated": "同一行", "timestamp": 2}

    assert append_dialogue_history(history, first, limit=10)
    assert not append_dialogue_history(history, duplicate, limit=10)

    assert len(history) == 1
    assert history[0]["timestamp"] == 2


def test_format_dialogue_history_entry_contains_original_and_translation() -> None:
    text = format_dialogue_history_entry(
        {"speaker": "真鈴", "original": "こんにちは", "translated": "你好", "timestamp": 1}
    )

    assert "真鈴" in text
    assert "原文：こんにちは" in text
    assert "译文：你好" in text


def test_overlay_live_translation_uses_active_translator_before_deepseek_fallback() -> None:
    source = Path("core/translation_overlay_window.py").read_text(encoding="utf-8")

    assert "active_translator" in source
    assert "create_translator(active)" in source
    assert 'create_translator("deepseek")' in source
    assert "live_translator_provider" in source
    assert "translate_realtime_text" in source
    assert "RollingTranslationContext" in source
    assert '"context": self._translation_context.snapshot()' in source
