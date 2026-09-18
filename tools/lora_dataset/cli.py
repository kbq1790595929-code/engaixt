from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .clean import CleaningConfig, classify_genres, clean_row, normalize_for_dataset
from .models import CleanRecord, RejectedRecord
from .reader import (
    group_rows_by_db,
    iter_cache_rows,
    iter_legacy_cache_rows,
    label_for_db,
    load_labels,
    summarize_databases,
)
from .writer import build_outputs, write_label_template, write_readme


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a cleaned Japanese-to-Chinese LoRA JSONL dataset from EngAixt cache DBs."
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path.home() / ".game_translator" / "cache",
        help="Directory containing translations_*.db files.",
    )
    parser.add_argument(
        "--legacy-cache",
        type=Path,
        default=Path.home() / "Downloads" / ".game_translator" / "translation_cache.db",
        help="Old global cache database; included automatically when present.",
    )
    parser.add_argument(
        "--no-legacy",
        action="store_true",
        help="Ignore the old global cache database.",
    )
    parser.add_argument(
        "--include-local-models",
        action="store_true",
        help="Include local-model records such as Hy-MT2; excluded by default.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / ".game_translator" / "datasets" / "ja-zh-lora",
        help="New directory for the generated dataset; source cache is never modified.",
    )
    parser.add_argument("--labels", type=Path, help="Optional database-to-game/genre JSON mapping.")
    parser.add_argument("--min-score", type=float, default=0.65)
    parser.add_argument("--min-visible-chars", type=int, default=2)
    parser.add_argument("--max-chars", type=int, default=2000)
    parser.add_argument("--validation-ratio", type=float, default=0.1)
    parser.add_argument(
        "--exclude-genres",
        default="",
        help="Comma-separated genre keys to omit, e.g. adult,unknown.",
    )
    parser.add_argument(
        "--exclude-ambiguous-kanji",
        action="store_true",
        help="Drop pure-kanji Japanese lines whose language cannot be proven from kana.",
    )
    parser.add_argument(
        "--keep-unchanged",
        action="store_true",
        help="Keep source==target records; omitted by default because they teach no translation.",
    )
    parser.add_argument("--max-records", type=int, default=0)
    return parser


def _excluded_record(record: CleanRecord, reason: str) -> RejectedRecord:
    return RejectedRecord(
        db_name=record.db_name,
        source_hash=record.source_hash,
        source_text=record.source_text,
        target_text=record.target_text,
        source_file=record.source_file,
        reason=reason,
        detail=",".join(record.genres),
    )


_LOCAL_MODEL_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:local|hy[_-]?mt|llama(?:\.cpp)?|gguf|q4|1\.5b|1\.8b)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)


def is_local_model_row(row) -> bool:
    """Return true for known local inference records, not cloud providers."""
    identity = f"{row.provider} {row.model}".strip()
    return bool(identity and _LOCAL_MODEL_RE.search(identity))


def translation_pair_key(row) -> tuple[str, str]:
    return (
        normalize_for_dataset(row.source_text),
        normalize_for_dataset(row.target_text),
    )


def local_only_pair_keys(grouped_rows: dict[str, list]) -> set[tuple[str, str]]:
    """Find local-model pairs not independently present in a cloud cache."""
    local_pairs: set[tuple[str, str]] = set()
    cloud_pairs: set[tuple[str, str]] = set()
    for rows in grouped_rows.values():
        for row in rows:
            if is_local_model_row(row):
                local_pairs.add(translation_pair_key(row))
            elif row.provider or row.model:
                cloud_pairs.add(translation_pair_key(row))
    return local_pairs - cloud_pairs


def build_dataset(args: argparse.Namespace) -> dict[str, object]:
    cache_dir = args.cache_dir.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()
    if not cache_dir.exists():
        raise SystemExit(f"cache directory does not exist: {cache_dir}")
    labels = load_labels(args.labels.expanduser().resolve() if args.labels else None)
    legacy_cache = None if args.no_legacy else args.legacy_cache.expanduser().resolve()
    summaries = summarize_databases(cache_dir, legacy_cache)
    rows = iter_cache_rows(cache_dir)
    if legacy_cache:
        from itertools import chain

        rows = chain(rows, iter_legacy_cache_rows(legacy_cache))
    grouped = group_rows_by_db(rows)
    local_only_pairs = local_only_pair_keys(grouped)
    config = CleaningConfig(
        min_visible_chars=max(1, args.min_visible_chars),
        max_chars=max(32, args.max_chars),
        min_quality_score=max(0.0, min(1.0, args.min_score)),
        keep_ambiguous_kanji=not args.exclude_ambiguous_kanji,
        keep_unchanged=args.keep_unchanged,
    )
    excluded_genres = {
        item.strip() for item in args.exclude_genres.split(",") if item.strip()
    }

    accepted: list[CleanRecord] = []
    rejected: list[RejectedRecord] = []
    inferred_by_db: dict[str, tuple[str, ...]] = {}
    for db_name in sorted(grouped):
        rows = grouped[db_name]
        label = label_for_db(labels, db_name)
        sample_files = [row.source_file for row in rows[:100]]
        sample_texts = [row.source_text for row in rows[:200]]
        if db_name == (legacy_cache.name if legacy_cache else "") and not label.genres:
            genres = ("unknown",)
        else:
            genres = classify_genres(
                " ".join([db_name, label.name, *sample_files]),
                sample_texts,
                explicit=label.genres,
            )
        inferred_by_db[db_name] = genres
        for row in rows:
            local_fingerprint = translation_pair_key(row) in local_only_pairs
            if not args.include_local_models and (is_local_model_row(row) or local_fingerprint):
                rejected.append(RejectedRecord(
                    db_name=row.db_name,
                    source_hash=row.source_hash,
                    source_text=row.source_text,
                    target_text=row.target_text,
                    source_file=row.source_file,
                    reason="local_model_excluded",
                    detail=(
                        f"provider={row.provider};model={row.model}"
                        if is_local_model_row(row)
                        else "matched_local_model_pair_without_provider_metadata"
                    ),
                ))
                continue
            result = clean_row(row, label, genres, config)
            if isinstance(result, RejectedRecord):
                rejected.append(result)
                continue
            if excluded_genres and excluded_genres.intersection(result.genres):
                rejected.append(_excluded_record(result, "excluded_genre"))
                continue
            accepted.append(result)

    accepted, conflicts = _deduplicate_and_limit(accepted, args.max_records)
    database_summary = []
    for item in summaries:
        db_name = str(item["db_name"])
        enriched = dict(item)
        enriched["genres"] = list(inferred_by_db.get(db_name, ("unknown",)))
        label = label_for_db(labels, db_name)
        enriched["game_name"] = label.name or db_name.removesuffix(".db")
        enriched["label_source"] = "labels" if db_name in labels else "heuristic"
        database_summary.append(enriched)

    manifest = build_outputs(
        output_dir=output_dir,
        records=accepted,
        rejected=rejected,
        conflicts=conflicts,
        validation_ratio=args.validation_ratio,
        database_summary=database_summary,
    )
    write_label_template(output_dir, summaries)
    write_readme(output_dir)
    manifest["cache_dir"] = str(cache_dir)
    manifest["output_dir"] = str(output_dir)
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _deduplicate_and_limit(
    records: list[CleanRecord],
    max_records: int,
) -> tuple[list[CleanRecord], list[dict[str, object]]]:
    from .writer import deduplicate_records

    deduplicated, conflicts = deduplicate_records(records)
    if max_records > 0:
        deduplicated = deduplicated[:max_records]
    return deduplicated, conflicts


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = build_dataset(args)
    print(json.dumps(manifest["counts"], ensure_ascii=False))
    print(f"dataset: {manifest['output_dir']}")
    return 0
