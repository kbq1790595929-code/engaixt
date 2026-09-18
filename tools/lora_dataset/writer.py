from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from .models import CleanRecord, RejectedRecord


SYSTEM_PROMPT = (
    "你是专业的日文到简体中文游戏文本翻译器。只输出译文，不解释、不添加前缀；"
    "保留原文中的占位符、控制标签、变量、换行和语气。"
)


def _write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def _source_key(source: str) -> str:
    normalized = " ".join((source or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _metadata(record: CleanRecord) -> dict[str, object]:
    return {
        "source_hash": record.source_hash,
        "db_name": record.db_name,
        "game_id": record.game_id,
        "game_name": record.game_name,
        "source_file": record.source_file,
        "genres": list(record.genres),
        "primary_genre": record.primary_genre,
        "text_type": record.text_type,
        "source_language": record.source_language,
        "quality_score": record.quality_score,
        "verified": record.verified,
        "source_lang": record.source_lang,
        "target_lang": record.target_lang,
        "provider": record.provider,
        "model": record.model,
        "prompt_version": record.prompt_version,
        "hit_count": record.hit_count,
    }


def pair_row(record: CleanRecord) -> dict[str, object]:
    return {
        "source": record.source_text,
        "target": record.target_text,
        "metadata": _metadata(record),
    }


def chat_row(record: CleanRecord) -> dict[str, object]:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": record.source_text},
            {"role": "assistant", "content": record.target_text},
        ],
        "metadata": _metadata(record),
    }


def reject_row(record: RejectedRecord) -> dict[str, object]:
    return {
        "source": record.source_text,
        "target": record.target_text,
        "db_name": record.db_name,
        "source_hash": record.source_hash,
        "source_file": record.source_file,
        "reason": record.reason,
        "detail": record.detail,
    }


def split_records(
    records: list[CleanRecord],
    validation_ratio: float,
) -> tuple[list[CleanRecord], list[CleanRecord]]:
    """Stable exact-source split; repeated source text cannot leak across sets."""
    validation_ratio = max(0.0, min(0.5, validation_ratio))
    train: list[CleanRecord] = []
    validation: list[CleanRecord] = []
    threshold = int(validation_ratio * 10000)
    for record in records:
        bucket = int(_source_key(record.source_text)[:8], 16) % 10000
        (validation if bucket < threshold else train).append(record)
    if records and validation_ratio > 0 and not validation:
        validation.append(train.pop())
    return train, validation


def deduplicate_records(
    records: Iterable[CleanRecord],
) -> tuple[list[CleanRecord], list[dict[str, object]]]:
    """Keep identical pairs once and exclude conflicting translations."""
    grouped: dict[str, list[CleanRecord]] = defaultdict(list)
    for record in records:
        grouped[_source_key(record.source_text)].append(record)

    clean: list[CleanRecord] = []
    conflicts: list[dict[str, object]] = []
    for key in sorted(grouped):
        group = grouped[key]
        targets = {record.target_text for record in group}
        if len(targets) > 1:
            conflicts.append({
                "source_key": key,
                "source": group[0].source_text,
                "variants": [
                    {
                        "target": record.target_text,
                        "db_name": record.db_name,
                        "source_file": record.source_file,
                        "provider": record.provider,
                        "model": record.model,
                    }
                    for record in group
                ],
                "reason": "conflicting_translations",
            })
            continue
        # Preserve per-game provenance when the legacy global cache contains the
        # same pair. Verified rows still take precedence over all other signals.
        chosen = sorted(
            group,
            key=lambda item: (
                not item.verified,
                item.source_file.startswith("legacy:"),
                -item.hit_count,
                item.db_name,
                item.source_file,
            ),
        )[0]
        clean.append(chosen)
    return clean, conflicts


def build_outputs(
    output_dir: Path,
    records: list[CleanRecord],
    rejected: list[RejectedRecord],
    conflicts: list[dict[str, object]],
    validation_ratio: float,
    database_summary: list[dict[str, object]],
) -> dict[str, object]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    genre_dir = output_dir / "by_genre"
    genre_dir.mkdir(exist_ok=True)
    for stale_file in genre_dir.glob("*.jsonl"):
        stale_file.unlink()

    train, validation = split_records(records, validation_ratio)
    _write_jsonl(output_dir / "all_pairs.jsonl", (pair_row(item) for item in records))
    _write_jsonl(output_dir / "train.jsonl", (chat_row(item) for item in train))
    _write_jsonl(output_dir / "validation.jsonl", (chat_row(item) for item in validation))
    _write_jsonl(output_dir / "rejected.jsonl", (reject_row(item) for item in rejected))
    _write_jsonl(output_dir / "conflicts.jsonl", conflicts)

    by_genre: dict[str, list[CleanRecord]] = defaultdict(list)
    for item in records:
        by_genre[item.primary_genre].append(item)
    genre_counts: dict[str, int] = {}
    for genre, genre_records in sorted(by_genre.items()):
        filename = f"{genre}.jsonl"
        _write_jsonl(genre_dir / filename, (chat_row(item) for item in genre_records))
        genre_counts[genre] = len(genre_records)

    manifest = {
        "format": "chatml-jsonl-with-metadata",
        "language_pair": "ja-ZH-CN",
        "system_prompt": SYSTEM_PROMPT,
        "counts": {
            "accepted": len(records),
            "train": len(train),
            "validation": len(validation),
            "rejected": len(rejected),
            "conflicting_sources": len(conflicts),
        },
        "genres": genre_counts,
        "rejection_reasons": dict(Counter(item.reason for item in rejected)),
        "games": dict(Counter(item.game_name for item in records)),
        "providers": dict(Counter(item.provider or "per_game_unknown" for item in records)),
        "models": dict(Counter(item.model or "per_game_unknown" for item in records)),
        "databases": database_summary,
        "validation_ratio": validation_ratio,
        "files": [
            "all_pairs.jsonl",
            "train.jsonl",
            "validation.jsonl",
            "rejected.jsonl",
            "conflicts.jsonl",
            "by_genre/",
        ],
        "warning": "Keep this dataset local unless you have the rights to redistribute source game text.",
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def write_label_template(output_dir: Path, summaries: list[dict[str, object]]) -> Path:
    path = Path(output_dir) / "labels.template.json"
    payload: dict[str, object] = {
        "default": {
            "name": "",
            "genres": [],
            "note": "Fill this only when the database-to-game mapping is known.",
        }
    }
    for item in summaries:
        payload[str(item["db_name"])] = {
            "name": "",
            "genres": [],
            "note": f"Top source files: {', '.join(map(str, item.get('source_files', [])))}",
        }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_readme(output_dir: Path) -> Path:
    path = Path(output_dir) / "README.md"
    path.write_text(
        "# Japanese to Chinese LoRA dataset\n\n"
        "`train.jsonl` and `validation.jsonl` use a ChatML-style `messages` field. "
        "`all_pairs.jsonl` keeps the cleaned source/target pair and metadata.\n\n"
        "Records are filtered for empty text, code/path fragments, untranslated output, "
        "mojibake, model explanations, placeholder mismatches, and low quality scores. "
        "Conflicting translations for the same normalized source are excluded and listed "
        "in `conflicts.jsonl`.\n\n"
        "Genre labels are heuristic unless supplied through `--labels`; review the manifest "
        "before training. This dataset may contain copyrighted game text and should remain "
        "private unless redistribution rights are clear.\n",
        encoding="utf-8",
    )
    return path
