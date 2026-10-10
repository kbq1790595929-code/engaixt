"""Build a high-confidence LoRA subset from an existing EngAixt dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils.text_extract import (
    PLACEHOLDER_PATTERN,
    contains_kana,
    extract_placeholders,
    is_mojibake,
    verify_translation,
)

from .clean import normalize_for_dataset
from .writer import SYSTEM_PROMPT


@dataclass(frozen=True)
class StrictFilterConfig:
    min_quality_score: float = 1.0
    validation_ratio: float = 0.1
    require_visible_japanese: bool = False
    reject_visible_target_kana: bool = False
    reject_model_annotations: bool = False
    reject_mangled_source_identifiers: bool = False
    min_visible_source_chars_for_ratio: int = 0
    min_target_length_ratio: float = 0.0
    max_target_length_ratio: float = float("inf")
    reject_extra_sentences: int = 0
    normalize_chinese_punctuation: bool = False

    @classmethod
    def high_confidence(cls, validation_ratio: float = 0.1) -> "StrictFilterConfig":
        """Return the conservative profile used for LoRA training candidates.

        This intentionally trades a tiny amount of useful data for precision:
        semantically suspicious short/long pairs are not useful enough to risk
        teaching the model batch misalignment or commentary leakage.
        """
        return cls(
            validation_ratio=validation_ratio,
            require_visible_japanese=True,
            reject_visible_target_kana=True,
            reject_model_annotations=True,
            reject_mangled_source_identifiers=True,
            min_visible_source_chars_for_ratio=20,
            min_target_length_ratio=0.35,
            max_target_length_ratio=2.0,
            reject_extra_sentences=2,
            normalize_chinese_punctuation=True,
        )


_MODEL_ANNOTATION_RE = re.compile(
    r"(?:[（(]\s*注[：:]|典型的日语|中文常见的拟声词|(?:翻译时|翻譯時).{0,80}(?:保留|使用))"
)
_HIRAGANA_RE = re.compile(r"[\u3041-\u3096\u309d-\u309f]")
_KATAKANA_RUN_RE = re.compile(r"[\u30a1-\u30fa\u30fc-\u30ff\uff66-\uff9f]+")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
# ``!?`` / ``!!`` is one emotional sentence ending, not two. Treat a full
# punctuation run as one boundary so emphatic dialogue is not falsely labeled
# as batch leakage.
_SENTENCE_END_RE = re.compile(r"[。！？!?]+")
_CJK_BEFORE_ASCII_PUNCTUATION_RE = re.compile(r"(?<=[\u3400-\u9fff])\s*([,!?]+)\s*")


def _visible_text(text: str) -> str:
    return PLACEHOLDER_PATTERN.sub("", text or "")


def _visible_length(text: str) -> int:
    return len(re.sub(r"\s+", "", _visible_text(text)))


def _sentence_count(text: str) -> int:
    return len(_SENTENCE_END_RE.findall(_visible_text(text)))


def _looks_like_mangled_source_identifier(text: str) -> bool:
    """Identify short resource IDs misread as Japanese text.

    Real Japanese prose normally contains hiragana particles. The pattern here
    deliberately requires no hiragana plus multiple isolated Katakana glyphs
    mixed with CJK characters, such as ``ア芸 リ預 ゼ放``. It is kept narrow
    because Katakana loanwords and stylized dialogue are otherwise valid data.
    """
    visible = _visible_text(text)
    compact = re.sub(r"\s+", "", visible)
    if len(compact) > 30 or _HIRAGANA_RE.search(visible) or not _CJK_RE.search(visible):
        return False
    isolated_runs = [
        run for run in _KATAKANA_RUN_RE.findall(visible)
        if len(run) == 1 and run != "ー"
    ]
    return len(isolated_runs) >= 2


def _normalize_visible_punctuation(text: str) -> str:
    """Normalize only Chinese prose punctuation, leaving ASCII game/UI text alone."""
    replacements = {",": "，", "!": "！", "?": "？"}
    return _CJK_BEFORE_ASCII_PUNCTUATION_RE.sub(
        lambda match: "".join(replacements[character] for character in match.group(1)), text
    )


def normalize_target_punctuation(text: str) -> str:
    """Normalize visible Chinese prose without touching engine placeholders."""
    parts: list[str] = []
    position = 0
    for match in PLACEHOLDER_PATTERN.finditer(text or ""):
        parts.append(_normalize_visible_punctuation((text or "")[position:match.start()]))
        parts.append(match.group(0))
        position = match.end()
    parts.append(_normalize_visible_punctuation((text or "")[position:]))
    return "".join(parts)


def _row_texts(row: dict[str, Any], config: StrictFilterConfig) -> tuple[str, str]:
    source = normalize_for_dataset(row.get("source", ""))
    target = normalize_for_dataset(row.get("target", ""))
    if config.normalize_chinese_punctuation:
        target = normalize_target_punctuation(target)
    return source, target


def rejection_reason(row: dict[str, Any], config: StrictFilterConfig) -> str:
    source, target = _row_texts(row, config)
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}

    if not source or not target:
        return "empty"
    if str(metadata.get("source_language") or "") != "ja":
        return "source_language_not_explicit_ja"
    if config.require_visible_japanese and not contains_kana(_visible_text(source)):
        return "source_not_actually_japanese"
    if config.reject_mangled_source_identifiers and _looks_like_mangled_source_identifier(source):
        return "source_mangled_identifier"
    try:
        quality_score = float(metadata.get("quality_score") or 0.0)
    except (TypeError, ValueError):
        quality_score = 0.0
    if quality_score < config.min_quality_score:
        return "quality_score_below_threshold"
    if _contains_corrupted_text(source) or _contains_corrupted_text(target):
        return "mojibake"
    if Counter(extract_placeholders(source)) != Counter(extract_placeholders(target)):
        return "placeholder_mismatch"
    if config.reject_visible_target_kana and contains_kana(_visible_text(target)):
        return "target_contains_visible_japanese"
    if config.reject_model_annotations and _MODEL_ANNOTATION_RE.search(_visible_text(target)):
        return "model_annotation"
    safe_target, warnings = verify_translation(source, target)
    # ``normalize_for_dataset`` uses NFKC, which folds Chinese punctuation
    # such as ``，`` back to ASCII. The high-confidence profile normalizes
    # visible prose after that stage, so compare the validator's exact result.
    if not safe_target or safe_target != target:
        return "translation_validation_changed_or_failed"
    if warnings:
        return "translation_validation_warning"
    visible_source_length = _visible_length(source)
    visible_target_length = _visible_length(target)
    if visible_source_length >= config.min_visible_source_chars_for_ratio:
        ratio = visible_target_length / max(1, visible_source_length)
        if ratio < config.min_target_length_ratio:
            return "translation_too_short"
        if ratio > config.max_target_length_ratio:
            return "translation_too_long"
        if (
            config.reject_extra_sentences
            and _sentence_count(target) >= _sentence_count(source) + config.reject_extra_sentences
        ):
            return "translation_contains_extra_sentences"
    return ""


def _contains_corrupted_text(text: str) -> bool:
    if is_mojibake(text):
        return True
    for char in text:
        codepoint = ord(char)
        if char == "\ufffd" or 0xE000 <= codepoint <= 0xF8FF:
            return True
        if (codepoint < 32 and char not in {"\n", "\r", "\t"}) or codepoint == 0x7F:
            return True
    return False


def _source_bucket(source: str) -> int:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 10000


def _write_jsonl(handle, row: dict[str, Any]) -> None:
    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _chat_row(source: str, target: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": source},
            {"role": "assistant", "content": target},
        ],
        "metadata": metadata,
    }


def filter_existing_dataset(
    source_path: Path,
    output_dir: Path,
    config: StrictFilterConfig = StrictFilterConfig(),
) -> dict[str, Any]:
    """Filter ``all_pairs.jsonl`` without altering the original dataset."""
    source_path = Path(source_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    providers: Counter[str] = Counter()
    genres: Counter[str] = Counter()
    seen_sources: set[str] = set()
    threshold = int(max(0.0, min(0.5, config.validation_ratio)) * 10000)

    with (
        source_path.open("r", encoding="utf-8") as source_handle,
        (output_dir / "all_pairs.jsonl").open("w", encoding="utf-8", newline="\n") as pairs_handle,
        (output_dir / "train.jsonl").open("w", encoding="utf-8", newline="\n") as train_handle,
        (output_dir / "validation.jsonl").open("w", encoding="utf-8", newline="\n") as validation_handle,
        (output_dir / "rejected.jsonl").open("w", encoding="utf-8", newline="\n") as rejected_handle,
    ):
        for line_number, raw_line in enumerate(source_handle, start=1):
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                counts["invalid_json"] += 1
                _write_jsonl(rejected_handle, {"line": line_number, "reason": "invalid_json"})
                continue
            if not isinstance(row, dict):
                counts["invalid_record"] += 1
                continue

            reason = rejection_reason(row, config)
            if reason:
                counts[reason] += 1
                _write_jsonl(rejected_handle, {
                    "source": row.get("source", ""),
                    "target": row.get("target", ""),
                    "metadata": row.get("metadata", {}),
                    "reason": reason,
                })
                continue

            source, target = _row_texts(row, config)
            source_key = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if source_key in seen_sources:
                counts["duplicate_source"] += 1
                continue
            seen_sources.add(source_key)

            metadata = dict(row.get("metadata") or {})
            _write_jsonl(pairs_handle, {"source": source, "target": target, "metadata": metadata})
            chat = _chat_row(source, target, metadata)
            if _source_bucket(source) < threshold:
                _write_jsonl(validation_handle, chat)
                counts["validation"] += 1
            else:
                _write_jsonl(train_handle, chat)
                counts["train"] += 1
            counts["accepted"] += 1
            providers[str(metadata.get("provider") or "per_game_unknown")] += 1
            genres[str(metadata.get("primary_genre") or "unknown")] += 1

    manifest = {
        "format": "chatml-jsonl-with-metadata",
        "language_pair": "ja-ZH-CN",
        "source_dataset": str(source_path),
        "filter": {
            "source_language": "ja",
            "min_quality_score": config.min_quality_score,
            "require_source_and_target_without_mojibake": True,
            "require_exact_placeholder_multiset": True,
            "require_no_translation_validation_warning": True,
            "require_visible_japanese": config.require_visible_japanese,
            "reject_visible_target_kana": config.reject_visible_target_kana,
            "reject_model_annotations": config.reject_model_annotations,
            "reject_mangled_source_identifiers": config.reject_mangled_source_identifiers,
            "min_visible_source_chars_for_ratio": config.min_visible_source_chars_for_ratio,
            "min_target_length_ratio": config.min_target_length_ratio,
            "max_target_length_ratio": config.max_target_length_ratio,
            "reject_extra_sentences": config.reject_extra_sentences,
            "normalize_chinese_punctuation": config.normalize_chinese_punctuation,
        },
        "counts": {
            "accepted": counts["accepted"],
            "train": counts["train"],
            "validation": counts["validation"],
            "rejected": sum(value for key, value in counts.items() if key not in {"accepted", "train", "validation"}),
        },
        "rejection_reasons": {
            key: value
            for key, value in sorted(counts.items())
            if key not in {"accepted", "train", "validation"}
        },
        "providers": dict(sorted(providers.items())),
        "genres": dict(sorted(genres.items())),
        "validation_ratio": config.validation_ratio,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "README.md").write_text(
        "# Strict Japanese to Chinese LoRA dataset\n\n"
        "Generated from an existing EngAixt dataset without modifying the source files. "
        "Only explicit Japanese source text with a perfect quality score and clean "
        "validation result is included.\n",
        encoding="utf-8",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Filter an existing EngAixt LoRA dataset.")
    parser.add_argument("source", type=Path, help="Source all_pairs.jsonl")
    parser.add_argument("output", type=Path, help="New output directory")
    parser.add_argument("--min-quality-score", type=float, default=1.0)
    parser.add_argument("--validation-ratio", type=float, default=0.1)
    parser.add_argument(
        "--high-confidence",
        action="store_true",
        help="Use conservative semantic-sanity filters intended for LoRA training.",
    )
    args = parser.parse_args(argv)
    config = (
        StrictFilterConfig.high_confidence(args.validation_ratio)
        if args.high_confidence
        else StrictFilterConfig(
            min_quality_score=max(0.0, min(1.0, args.min_quality_score)),
            validation_ratio=args.validation_ratio,
        )
    )
    manifest = filter_existing_dataset(args.source, args.output, config)
    print(json.dumps(manifest["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
