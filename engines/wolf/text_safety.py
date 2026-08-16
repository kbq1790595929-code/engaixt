from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from engines.base import TextItem


_WOLF_CONTROL_RE = re.compile(
    r"\\(?:"
    r"[A-Za-z]+(?:\[(?:[^\[\]\r\n]|\[[^\[\]\r\n]*\])*\])?"
    r"|[-+*/.<>|!^](?:\[[^\[\]\r\n]*\])?"
    r")"
)
_WOLF_RUBY_RE = re.compile(r"\\r\[([^,\]\r\n]+),[^\]\r\n]*\]")
_WOLF_BROKEN_RUBY_RE = re.compile(r"\\r\[([^\]\r\n]+)\]")
_KANA_RE = re.compile(r"[\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fc-\u30ff\uff66-\uff9f]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_BREAK_PUNCTUATION = set("\u3001\u3002\uff0c\uff0e\uff01\uff1f\uff1b\uff1a\u2026\u2014,.!?;: ")


@dataclass
class WolfTextSafetyStats:
    checked: int = 0
    changed: int = 0
    leading_controls_restored: int = 0
    ruby_markup_removed: int = 0
    kana_residuals_cleaned: int = 0
    line_layout_restored: int = 0
    control_mismatch_rejected: int = 0
    residual_kana_rejected: int = 0
    non_chinese_rejected: int = 0
    samples: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "checked": self.checked,
            "changed": self.changed,
            "leading_controls_restored": self.leading_controls_restored,
            "ruby_markup_removed": self.ruby_markup_removed,
            "kana_residuals_cleaned": self.kana_residuals_cleaned,
            "line_layout_restored": self.line_layout_restored,
            "control_mismatch_rejected": self.control_mismatch_rejected,
            "residual_kana_rejected": self.residual_kana_rejected,
            "non_chinese_rejected": self.non_chinese_rejected,
            "samples": list(self.samples),
        }


def strip_wolf_ruby(text: str) -> str:
    """Drop pronunciation markup while retaining the displayed base text."""
    return _WOLF_RUBY_RE.sub(lambda match: match.group(1), str(text or ""))


def extract_wolf_controls(text: str) -> list[str]:
    return _WOLF_CONTROL_RE.findall(str(text or ""))


def sanitize_wolf_items(items: list[TextItem], target_lang: str) -> WolfTextSafetyStats:
    stats = WolfTextSafetyStats()
    for item in items:
        if not item.translated:
            continue
        stats.checked += 1
        before = str(item.translated)
        source = strip_wolf_ruby(item.original)
        translated = _strip_translated_ruby(before)
        if translated != before:
            stats.ruby_markup_removed += 1

        translated, kana_cleaned = _clean_kana_residuals(source, translated)
        if kana_cleaned:
            stats.kana_residuals_cleaned += 1

        translated, restored = _restore_leading_controls(source, translated)
        stats.leading_controls_restored += restored

        translated, layout_restored = _restore_line_layout(source, translated)
        if layout_restored:
            stats.line_layout_restored += 1

        reason = _invalid_reason(source, translated, target_lang)
        if reason:
            _reject_item(item, before, reason)
            if reason == "control_mismatch":
                stats.control_mismatch_rejected += 1
            elif reason == "residual_kana":
                stats.residual_kana_rejected += 1
            elif reason == "non_chinese_output":
                stats.non_chinese_rejected += 1
            if len(stats.samples) < 20:
                stats.samples.append({
                    "reason": reason,
                    "source": item.original[:200],
                    "translation": before[:200],
                    "context": str(item.context or "")[:200],
                })
            continue

        if translated != before:
            item.translated = translated
            stats.changed += 1
    return stats


def _strip_translated_ruby(text: str) -> str:
    text = _WOLF_RUBY_RE.sub(lambda match: match.group(1), text)
    return _WOLF_BROKEN_RUBY_RE.sub(lambda match: match.group(1), text)


def _clean_kana_residuals(source: str, translated: str) -> tuple[str, bool]:
    visible_source = _visible_text(source).strip()
    visible_translated = _visible_text(translated)
    if visible_source == "えっち" and not _CJK_RE.search(visible_translated):
        return translated.replace(visible_translated, "色情"), True
    if not (_CJK_RE.search(visible_translated) and _KANA_RE.search(visible_translated)):
        return translated, False

    # Models occasionally leave Japanese small-tsu or long-vowel marks mixed
    # into an otherwise Chinese line. They are prosody, not a name or runtime
    # token, so normalize them locally instead of discarding a valid sentence.
    cleaned = translated.replace("ー", "—").replace("ｰ", "—")
    cleaned = _KANA_RE.sub("", cleaned)
    return cleaned, cleaned != translated


def _restore_leading_controls(source: str, translated: str) -> tuple[str, int]:
    source_prefix, source_tokens = _leading_control_prefix(source)
    if not source_tokens:
        return translated, 0

    source_counts = Counter(source_tokens)
    translated_counts = Counter(extract_wolf_controls(translated))
    missing = source_counts - translated_counts
    if not missing:
        return translated, 0

    # Rebuild only the original leading display prefix. Inline variables are
    # allowed to move for Chinese grammar and must not be pulled to the front.
    translated_prefix, translated_tokens = _leading_control_prefix(translated)
    residual_prefix = translated_prefix
    consumed = Counter()
    for token in translated_tokens:
        if consumed[token] < source_counts[token]:
            residual_prefix = residual_prefix.replace(token, "", 1)
            consumed[token] += 1

    restored = sum(missing.values())
    return source_prefix + residual_prefix + translated[len(translated_prefix):], restored


def _leading_control_prefix(text: str) -> tuple[str, list[str]]:
    position = 0
    tokens: list[str] = []
    last_control_end = 0
    while position < len(text):
        while position < len(text) and text[position] in " \t\u3000":
            position += 1
        match = _WOLF_CONTROL_RE.match(text, position)
        if not match:
            break
        tokens.append(match.group(0))
        position = match.end()
        last_control_end = position
    return text[:last_control_end], tokens


def _restore_line_layout(source: str, translated: str) -> tuple[str, bool]:
    source_lines = source.split("\n")
    translated_lines = translated.split("\n")
    if len(source_lines) <= 1 or len(source_lines) == len(translated_lines):
        return translated, False
    if any(not _visible_text(line).strip() for line in source_lines):
        return translated, False

    units = _translation_units(translated.replace("\n", ""))
    visible_total = sum(1 for _prefix, char in units if char)
    if visible_total < len(source_lines):
        return translated, False

    source_weights = [max(1, len(_visible_text(line).strip())) for line in source_lines]
    weight_total = sum(source_weights)
    desired: list[int] = []
    running = 0
    for weight in source_weights[:-1]:
        running += weight
        desired.append(round(visible_total * running / weight_total))

    boundaries: list[int] = []
    minimum = 1
    for index, target in enumerate(desired):
        maximum = visible_total - (len(desired) - index)
        target = max(minimum, min(maximum, target))
        boundary = _nearest_break(units, target, minimum, maximum)
        boundaries.append(boundary)
        minimum = boundary + 1

    lines: list[str] = []
    start = 0
    for boundary in boundaries + [visible_total]:
        lines.append("".join(prefix + char for prefix, char in units[start:boundary]).strip())
        start = boundary
    return "\n".join(lines), True


def _translation_units(text: str) -> list[tuple[str, str]]:
    units: list[tuple[str, str]] = []
    pending = ""
    position = 0
    for match in _WOLF_CONTROL_RE.finditer(text):
        for char in text[position:match.start()]:
            units.append((pending, char))
            pending = ""
        pending += match.group(0)
        position = match.end()
    for char in text[position:]:
        units.append((pending, char))
        pending = ""
    if pending:
        if units:
            prefix, char = units[-1]
            units[-1] = (prefix, char + pending)
        else:
            units.append((pending, ""))
    return units


def _nearest_break(
    units: list[tuple[str, str]], target: int, minimum: int, maximum: int,
) -> int:
    candidates = [
        index
        for index in range(minimum, maximum + 1)
        if units[index - 1][1] in _BREAK_PUNCTUATION
    ]
    if not candidates:
        return target
    nearby = [index for index in candidates if abs(index - target) <= 6]
    return min(nearby or candidates, key=lambda index: (abs(index - target), index))


def _visible_text(text: str) -> str:
    return _WOLF_CONTROL_RE.sub("", _strip_translated_ruby(text))


def _invalid_reason(source: str, translated: str, target_lang: str) -> str:
    if Counter(extract_wolf_controls(source)) != Counter(extract_wolf_controls(translated)):
        return "control_mismatch"

    if not str(target_lang or "").lower().startswith("zh"):
        return ""

    source_visible = _visible_text(source).strip()
    translated_visible = _visible_text(translated).strip()
    if _KANA_RE.search(source_visible) and _KANA_RE.search(translated_visible):
        return "residual_kana"
    if (
        (_KANA_RE.search(source_visible) or _CJK_RE.search(source_visible))
        and not _CJK_RE.search(translated_visible)
        and not _may_disappear_in_chinese(source_visible, translated_visible)
    ):
        return "non_chinese_output"
    return ""


def _may_disappear_in_chinese(source_visible: str, translated_visible: str) -> bool:
    kana = "".join(_KANA_RE.findall(source_visible))
    translated_content = re.sub(r"[\W_]+", "", translated_visible, flags=re.UNICODE)
    return len(kana) <= 1 and not translated_content


def _reject_item(item: TextItem, failed: str, reason: str) -> None:
    if not item.meta:
        item.meta = {}
    item.meta["validation_failed_translation"] = failed
    item.meta["wolf_validation_reason"] = reason
    if _KANA_RE.search(item.original):
        item.translated = ""
    else:
        # Pure Han source strings remain readable Chinese when an old cache
        # contains an English translation such as "right hand" or "defense".
        item.translated = item.original
