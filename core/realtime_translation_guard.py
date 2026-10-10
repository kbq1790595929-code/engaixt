"""Validation shared by realtime overlay translation and its retry loop."""

from __future__ import annotations

from utils.text_extract import (
    contains_kana,
    is_acceptable_same_as_source,
    is_punctuation_only,
    verify_translation,
)


_DATABASE_TOKEN_SYMBOLS = "+-×÷%#&*/·"


def _same_as_source_is_database_token(text: str) -> bool:
    """Allow unchanged Japanese-source tokens only when they are data keys."""
    visible = str(text or "").strip()
    if not visible or contains_kana(visible):
        return False
    return any(char.isdigit() or char in _DATABASE_TOKEN_SYMBOLS for char in visible)


def realtime_translation_issue(
    source: str,
    translated: str | None,
    source_lang: str = "ja",
    target_lang: str = "zh-CN",
) -> str | None:
    """Return a reason when a realtime result is still an untranslated item."""
    source_text = str(source or "").strip()
    candidate = str(translated or "").strip()
    if not candidate:
        return "empty"
    if not source_text:
        return None
    if is_punctuation_only(source_text):
        return None if candidate == source_text else "punctuation_only"

    source_is_japanese = str(source_lang or "").lower() in {"ja", "jp", "japanese", "auto"}
    target_is_chinese = str(target_lang or "").lower().startswith("zh")
    if source_is_japanese and target_is_chinese and candidate != source_text and contains_kana(candidate):
        return "japanese_output"

    safe, _warnings = verify_translation(source_text, candidate)
    if not safe:
        return "invalid_output"
    if safe.strip() == source_text:
        if source_is_japanese and target_is_chinese:
            if not _same_as_source_is_database_token(source_text):
                return "unchanged_source"
        elif not is_acceptable_same_as_source(source_text, safe):
            return "unchanged_source"

    if source_is_japanese and target_is_chinese:
        # Speaker names are handled on their own channel and never pass
        # through this guard. Any kana in a Chinese realtime result therefore
        # signals that Japanese text was copied through.
        if contains_kana(safe):
            return "japanese_output"
    return None


def is_missing_realtime_translation(
    source: str,
    translated: str | None,
    source_lang: str = "ja",
    target_lang: str = "zh-CN",
) -> bool:
    return realtime_translation_issue(source, translated, source_lang, target_lang) is not None
