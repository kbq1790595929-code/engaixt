"""RPG Maker grouped-message expansion and runtime-map safety checks."""

from __future__ import annotations

import re
from collections.abc import Callable

from core.rpgmaker_event_extraction import RPGMAKER_MESSAGE_SEPARATOR
from engines.base import TextItem
from utils.logger import debug, info, warning
from utils.text_extract import verify_translation


_LEADING_SPEAKER_RE = re.compile(
    r"^(?P<prefix>(?:\\n|\n)?<(?P<name>[^<>\r\n]{1,256})>)"
)


def leading_speaker(text: str) -> tuple[str, str] | None:
    match = _LEADING_SPEAKER_RE.match(str(text or ""))
    if not match:
        return None
    return match.group("prefix"), match.group("name")


def sanitize_translation_map(
    trans_map: dict[str, str],
    *,
    should_skip: Callable[[str], bool],
) -> dict[str, str]:
    """Validate the final per-line map, including translated speaker tags."""
    cleaned: dict[str, str] = {}
    dropped = 0
    for source, translated in (trans_map or {}).items():
        source_s = str(source or "")
        translated_s = str(translated or "")
        if should_skip(source_s):
            dropped += 1
            continue
        source_speaker = leading_speaker(source_s)
        translated_speaker = leading_speaker(translated_s)
        if not source_speaker and translated_speaker:
            dropped += 1
            debug(f"[RPGMaker 清洗] 丢弃续行中泄露的说话人标签: {source_s[:30]}")
            continue

        validation_target = translated_s
        translated_prefix = ""
        if source_speaker and translated_speaker:
            source_prefix, _source_name = source_speaker
            translated_prefix, _translated_name = translated_speaker
            validation_target = source_prefix + translated_s[len(translated_prefix):]

        safe, warns = verify_translation(source_s, validation_target)
        if warns:
            debug(f"[RPGMaker 清洗] {source_s[:30]}: {'; '.join(warns)}")
        if safe and safe.strip() and safe != source_s:
            if translated_prefix and source_speaker:
                source_prefix, _source_name = source_speaker
                if safe.startswith(source_prefix):
                    safe = translated_prefix + safe[len(source_prefix):]
            cleaned[source_s] = safe
        else:
            dropped += 1
    if dropped:
        info(f"RPGMaker 运行时译文表已清洗，丢弃 {dropped} 条无效/污染条目")
    return cleaned


def build_runtime_translation_map(
    items: list[TextItem],
) -> tuple[dict[str, str], dict[str, int]]:
    """Expand grouped RPG Maker messages back to per-``401`` runtime keys."""
    trans_map: dict[str, str] = {}
    stats = {
        "translated_items": 0,
        "grouped_messages": 0,
        "expanded_lines": 0,
        "segment_mismatch": 0,
        "duplicate_sources": 0,
    }
    speaker_names: dict[str, str] = {}

    for item in items:
        meta = getattr(item, "meta", {}) or {}
        if (
            meta.get("rpgmaker_speaker_name")
            and item.translated
            and item.translated != item.original
        ):
            speaker_names[str(item.original)] = str(item.translated).strip()

    for item in items:
        if not item.translated or item.translated == item.original:
            continue
        stats["translated_items"] += 1
        meta = getattr(item, "meta", {}) or {}
        segments = meta.get("rpgmaker_segments")
        if not isinstance(segments, list) or not segments:
            if item.original in trans_map:
                stats["duplicate_sources"] += 1
            else:
                trans_map[item.original] = item.translated
            continue

        translated_segments = str(item.translated).split(RPGMAKER_MESSAGE_SEPARATOR)
        if len(translated_segments) != len(segments):
            stats["segment_mismatch"] += 1
            warning(
                "RPGMaker 消息分段校验失败，跳过该对话框: "
                f"原文 {len(segments)} 行 / 译文 {len(translated_segments)} 行"
            )
            continue

        stats["grouped_messages"] += 1
        speaker = str(meta.get("rpgmaker_speaker") or "")
        translated_speaker = speaker_names.get(speaker, "")
        for index, (source_segment, translated_segment) in enumerate(
            zip(segments, translated_segments)
        ):
            source_segment = str(source_segment).strip()
            translated_segment = str(translated_segment).strip()
            if index == 0 and speaker and translated_speaker:
                leading = leading_speaker(translated_segment)
                if leading:
                    prefix, _name = leading
                    translated_segment = (
                        prefix[:prefix.find("<") + 1]
                        + translated_speaker
                        + ">"
                        + translated_segment[len(prefix):]
                    )
            if not source_segment or not translated_segment or source_segment == translated_segment:
                continue
            if source_segment in trans_map:
                stats["duplicate_sources"] += 1
                continue
            trans_map[source_segment] = translated_segment
            stats["expanded_lines"] += 1

    return trans_map, stats
