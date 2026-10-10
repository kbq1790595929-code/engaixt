from __future__ import annotations

import json
import hashlib
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .models import CacheRow, GameLabel


def _read_only_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def load_labels(path: Path | None) -> dict[str, GameLabel]:
    """Read optional database labels from a small JSON mapping.

    Accepted keys are the full database filename, its stem, or ``default``.
    Values may be a string game name or an object with ``name``, ``genres`` and
    ``note`` fields.
    """
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("labels JSON must contain an object")

    result: dict[str, GameLabel] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            result[str(key)] = GameLabel(name=value)
            continue
        if not isinstance(value, dict):
            raise ValueError(f"label for {key!r} must be a string or object")
        genres = value.get("genres", ())
        if isinstance(genres, str):
            genres = (genres,)
        elif isinstance(genres, list):
            genres = tuple(str(item) for item in genres)
        else:
            genres = tuple(genres or ())
        result[str(key)] = GameLabel(
            name=str(value.get("name", "") or ""),
            genres=genres,
            note=str(value.get("note", "") or ""),
        )
    return result


def label_for_db(labels: dict[str, GameLabel], db_name: str) -> GameLabel:
    return (
        labels.get(db_name)
        or labels.get(Path(db_name).stem)
        or labels.get("default")
        or GameLabel()
    )


def iter_cache_rows(cache_dir: Path) -> Iterable[CacheRow]:
    """Yield translation rows from all per-game cache databases, read-only."""
    cache_dir = Path(cache_dir)
    if not cache_dir.exists():
        return
    for db_path in sorted(cache_dir.glob("translations_*.db")):
        try:
            with _read_only_connection(db_path) as connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='translations'"
                ).fetchone()
                if not table:
                    continue
                rows = connection.execute(
                    "SELECT source_hash, source_text, target_text, source_file, "
                    "extraction_time, verified FROM translations "
                    "WHERE target_text IS NOT NULL AND trim(target_text) <> ''"
                )
                for source_hash, source, target, source_file, extracted, verified in rows:
                    yield CacheRow(
                        db_name=db_path.name,
                        source_hash=str(source_hash or ""),
                        source_text=str(source or ""),
                        target_text=str(target or ""),
                        source_file=str(source_file or ""),
                        extraction_time=extracted,
                        verified=bool(verified),
                    )
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError(f"cannot read cache database {db_path}: {exc}") from exc


def iter_legacy_cache_rows(path: Path | None) -> Iterable[CacheRow]:
    """Read the old global cache format without modifying it.

    The legacy database has no game directory, so rows intentionally share a
    synthetic source file and are classified as ``unknown`` unless labeled by
    the caller.
    """
    if path is None or not path.exists():
        return
    path = Path(path)
    try:
        with _read_only_connection(path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "cache" in tables:
                for row in connection.execute(
                    "SELECT original, translated, source_lang, target_lang, created_at "
                    "FROM cache WHERE translated IS NOT NULL AND trim(translated) <> ''"
                ):
                    source, target, source_lang, target_lang, created_at = row
                    digest = hashlib.sha256(
                        f"legacy|{source}|{target}|{source_lang}|{target_lang}".encode("utf-8")
                    ).hexdigest()
                    yield CacheRow(
                        db_name=path.name,
                        source_hash="legacy-cache:" + digest,
                        source_text=str(source or ""),
                        target_text=str(target or ""),
                        source_file="legacy:cache",
                        extraction_time=None,
                        verified=False,
                        source_lang=str(source_lang or ""),
                        target_lang=str(target_lang or ""),
                    )
            if "cache_v2" in tables:
                for row in connection.execute(
                    "SELECT provider, model, prompt_version, source_lang, target_lang, "
                    "text_type, original, translated, created_at, hit_count "
                    "FROM cache_v2 WHERE translated IS NOT NULL AND trim(translated) <> ''"
                ):
                    (
                        provider,
                        model,
                        prompt_version,
                        source_lang,
                        target_lang,
                        text_type,
                        source,
                        target,
                        created_at,
                        hit_count,
                    ) = row
                    digest = hashlib.sha256(
                        f"legacy-v2|{provider}|{model}|{source}|{target}".encode("utf-8")
                    ).hexdigest()
                    source_hash = "legacy-v2:" + digest
                    yield CacheRow(
                        db_name=path.name,
                        source_hash=source_hash,
                        source_text=str(source or ""),
                        target_text=str(target or ""),
                        source_file=f"legacy:{text_type or 'message'}",
                        extraction_time=int(created_at) if created_at else None,
                        verified=False,
                        source_lang=str(source_lang or ""),
                        target_lang=str(target_lang or ""),
                        provider=str(provider or ""),
                        model=str(model or ""),
                        prompt_version=str(prompt_version or ""),
                        text_type=str(text_type or "message"),
                        hit_count=int(hit_count or 0),
                    )
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(f"cannot read legacy cache database {path}: {exc}") from exc


def summarize_databases(cache_dir: Path, legacy_cache: Path | None = None) -> list[dict[str, object]]:
    """Return a stable label template without loading all text into memory."""
    summaries: list[dict[str, object]] = []
    for db_path in sorted(Path(cache_dir).glob("translations_*.db")):
        item: dict[str, object] = {
            "db_name": db_path.name,
            "game_id": db_path.stem,
            "rows": 0,
            "source_files": [],
        }
        try:
            with _read_only_connection(db_path) as connection:
                rows = connection.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT source_file) FROM translations "
                    "WHERE target_text IS NOT NULL AND trim(target_text) <> ''"
                ).fetchone()
                item["rows"] = int(rows[0] or 0)
                item["source_files"] = [
                    row[0]
                    for row in connection.execute(
                        "SELECT source_file FROM translations "
                        "WHERE source_file IS NOT NULL AND source_file <> '' "
                        "GROUP BY source_file ORDER BY COUNT(*) DESC LIMIT 12"
                    )
                ]
        except (OSError, sqlite3.Error) as exc:
            item["error"] = str(exc)
        summaries.append(item)
    if legacy_cache and Path(legacy_cache).exists():
        path = Path(legacy_cache)
        row_count = 0
        source_files: set[str] = set()
        with _read_only_connection(path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "cache" in tables:
                row_count += int(connection.execute(
                    "SELECT COUNT(*) FROM cache "
                    "WHERE translated IS NOT NULL AND trim(translated) <> ''"
                ).fetchone()[0])
                source_files.add("legacy:cache")
            if "cache_v2" in tables:
                row_count += int(connection.execute(
                    "SELECT COUNT(*) FROM cache_v2 "
                    "WHERE translated IS NOT NULL AND trim(translated) <> ''"
                ).fetchone()[0])
                source_files.update(
                    f"legacy:{row[0] or 'message'}"
                    for row in connection.execute(
                        "SELECT DISTINCT text_type FROM cache_v2"
                    )
                )
        summaries.append({
            "db_name": path.name,
            "game_id": path.stem,
            "rows": row_count,
            "source_files": sorted(source_files),
            "legacy": True,
        })
    return summaries


def group_rows_by_db(rows: Iterable[CacheRow]) -> dict[str, list[CacheRow]]:
    grouped: dict[str, list[CacheRow]] = defaultdict(list)
    for row in rows:
        grouped[row.db_name].append(row)
    return dict(grouped)
