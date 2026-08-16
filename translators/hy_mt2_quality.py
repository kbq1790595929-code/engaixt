from __future__ import annotations

import re
from collections import Counter, defaultdict

from engines.base import TextItem


_DIALOGUE_PAIRS = {"「": "」", "『": "』"}
_EDGE_QUOTES_RE = re.compile(r'^[「『“”"\s]+|[」』“”"\s]+$')
_SPEAKER_RE = re.compile(r"^【([^】\r\n]+)】(?:\r?\n|$)")
_PH_TOKEN_RE = re.compile(r"\{\{?\s*PH\s*\d+\s*\}?\}", re.IGNORECASE)


def repair_translation_structure(source: str, translated: str) -> str:
    """Restore deterministic dialogue punctuation that a small model may drop."""
    source = str(source or "")
    translated = str(translated or "")
    if not translated:
        return translated

    # Hy-MT2 occasionally appends an orphan brace after a restored control code.
    if "{" not in source and "}" not in source:
        protected_tokens: list[str] = []

        def _protect_token(match: re.Match[str]) -> str:
            protected_tokens.append(match.group(0))
            return f"\x00PH{len(protected_tokens) - 1}\x00"

        translated = _PH_TOKEN_RE.sub(_protect_token, translated)
        translated = translated.replace("{", "").replace("}", "")
        for index, token in enumerate(protected_tokens):
            translated = translated.replace(f"\x00PH{index}\x00", token)

    source_lines = source.splitlines()
    translated_lines = translated.splitlines()
    if len(source_lines) == len(translated_lines):
        repaired_lines = []
        for source_line, translated_line in zip(source_lines, translated_lines):
            opener = source_line.strip()[:1]
            closer = _DIALOGUE_PAIRS.get(opener)
            if closer and source_line.strip().endswith(closer):
                translated_line = _wrap_dialogue(translated_line, opener, closer)
            repaired_lines.append(translated_line)
        translated = "\n".join(repaired_lines)

    source_body, translated_head, translated_body = _dialogue_body(source, translated)
    source_stripped = source_body.strip()
    if source_stripped:
        opener = source_stripped[:1]
        closer = _DIALOGUE_PAIRS.get(opener)
        if closer and source_stripped.endswith(closer):
            translated = translated_head + _wrap_dialogue(translated_body, opener, closer)
    return translated


def normalize_speaker_names(items: list[TextItem]) -> tuple[int, int]:
    """Use the majority translation for each repeated bracketed speaker name."""
    candidates: dict[str, list[str]] = defaultdict(list)
    parsed: list[tuple[TextItem, str, str] | None] = []
    for item in items:
        source_match = _SPEAKER_RE.match(str(item.original or ""))
        translated_match = _SPEAKER_RE.match(str(item.translated or ""))
        if not source_match or not translated_match:
            parsed.append(None)
            continue
        source_name = source_match.group(1).strip()
        translated_name = translated_match.group(1).strip()
        parsed.append((item, source_name, translated_name))
        if translated_name:
            candidates[source_name].append(translated_name)

    preferred: dict[str, str] = {}
    inconsistent_groups = 0
    for source_name, names in candidates.items():
        if len(names) < 2:
            continue
        counts = Counter(names)
        if len(counts) > 1:
            inconsistent_groups += 1
        # Counter preserves first-seen order for ties, keeping output deterministic.
        preferred[source_name] = counts.most_common(1)[0][0]

    changed = 0
    for entry in parsed:
        if entry is None:
            continue
        item, source_name, current_name = entry
        chosen = preferred.get(source_name)
        if not chosen or chosen == current_name:
            continue
        item.translated = _SPEAKER_RE.sub(
            lambda _match: f"【{chosen}】\n",
            item.translated,
            count=1,
        )
        changed += 1
    return changed, inconsistent_groups


def finalize_local_translations(items: list[TextItem]) -> tuple[int, int, int]:
    structure_fixed = 0
    for item in items:
        if not item.translated or item.translated == item.original:
            continue
        repaired = repair_translation_structure(item.original, item.translated)
        if repaired != item.translated:
            item.translated = repaired
            structure_fixed += 1
    speaker_fixed, inconsistent_groups = normalize_speaker_names(items)
    return structure_fixed, speaker_fixed, inconsistent_groups


def _dialogue_body(source: str, translated: str) -> tuple[str, str, str]:
    source_match = _SPEAKER_RE.match(source)
    translated_match = _SPEAKER_RE.match(translated)
    if source_match and translated_match:
        return (
            source[source_match.end():],
            translated[:translated_match.end()],
            translated[translated_match.end():],
        )
    return source, "", translated


def _wrap_dialogue(text: str, opener: str, closer: str) -> str:
    inner = _EDGE_QUOTES_RE.sub("", str(text or "").strip())
    return opener + inner + closer
