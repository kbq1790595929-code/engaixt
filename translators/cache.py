from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from translators.pricing import DEEPSEEK_V4_FLASH, cost_cny, pricing_dict
from utils.logger import warning
from utils.text_extract import is_acceptable_same_as_source, verify_translation


DB_PATH = Path.home() / "Downloads" / ".game_translator" / "translation_cache.db"

DEFAULT_PROVIDER = "generic"
DEFAULT_MODEL = "unknown"
DEFAULT_PROMPT_VERSION = "legacy_v1"

DEEPSEEK_V4_FLASH_INPUT_CNY_PER_M = DEEPSEEK_V4_FLASH.input_cny_per_m
DEEPSEEK_V4_FLASH_OUTPUT_CNY_PER_M = DEEPSEEK_V4_FLASH.output_cny_per_m


def _usage_value(value: Any, key: str):
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


_SPACE_RE = re.compile(r"[ \t\f\v]+")
_LINE_SPACE_RE = re.compile(r" *\n *")


@dataclass
class CacheLookup:
    translated: str | None
    key: str
    normalized_text: str
    hit: bool = False
    legacy_hit: bool = False


@dataclass(frozen=True)
class CacheLookupRequest:
    text: str
    source_lang: str
    target_lang: str
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    prompt_version: str = DEFAULT_PROMPT_VERSION
    text_type: str = "message"


@dataclass
class TranslationCacheStats:
    cache_enabled: bool = True
    cache_auto_cleanup: bool = True
    cache_max_size_bytes: int = 1024 * 1024 * 1024
    cache_pruned_rows: int = 0
    cache_pruned_bytes: int = 0
    cache_cleanup_count: int = 0
    total_texts: int = 0
    cache_exact_hit: int = 0
    cache_legacy_hit: int = 0
    cache_miss: int = 0
    api_request_count: int = 0
    dedupe_saved: int = 0
    batch_request_count: int = 0
    batch_item_count: int = 0
    compact_batch_request_count: int = 0
    compact_batch_item_count: int = 0
    contextual_batch_request_count: int = 0
    contextual_batch_item_count: int = 0
    produced_batch_count: int = 0
    first_batch_ready_seconds: float = 0.0
    first_api_request_seconds: float = 0.0
    batch_retry_count: int = 0
    batch_split_count: int = 0
    json_parse_fail_count: int = 0
    partial_batch_recovered_count: int = 0
    partial_batch_recovered_item_count: int = 0
    validation_fail_count: int = 0
    single_fallback_count: int = 0
    local_quality_fail_count: int = 0
    local_control_fail_count: int = 0
    local_truncation_count: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    estimated_cost_cny: float = 0.0
    actual_input_tokens: int = 0
    actual_output_tokens: int = 0
    actual_cache_hit_input_tokens: int = 0
    actual_cache_miss_input_tokens: int = 0
    actual_cost_cny: float = 0.0
    estimated_saved_input_tokens: int = 0
    estimated_saved_output_tokens: int = 0
    estimated_saved_cost_cny: float = 0.0
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    prompt_version: str = DEFAULT_PROMPT_VERSION
    cache_db: str = str(DB_PATH)
    run_id: str = ""
    started_at: float = field(default_factory=time.time)

    def reset(self, provider: str = DEFAULT_PROVIDER, model: str = DEFAULT_MODEL,
              prompt_version: str = DEFAULT_PROMPT_VERSION, cache_db: Path | str = DB_PATH,
              run_id: str = ""):
        enabled, auto_cleanup, max_size_bytes = _cache_runtime_settings()
        self.cache_enabled = enabled
        self.cache_auto_cleanup = auto_cleanup
        self.cache_max_size_bytes = max_size_bytes
        self.cache_pruned_rows = 0
        self.cache_pruned_bytes = 0
        self.cache_cleanup_count = 0
        self.total_texts = 0
        self.cache_exact_hit = 0
        self.cache_legacy_hit = 0
        self.cache_miss = 0
        self.api_request_count = 0
        self.dedupe_saved = 0
        self.batch_request_count = 0
        self.batch_item_count = 0
        self.compact_batch_request_count = 0
        self.compact_batch_item_count = 0
        self.contextual_batch_request_count = 0
        self.contextual_batch_item_count = 0
        self.produced_batch_count = 0
        self.first_batch_ready_seconds = 0.0
        self.first_api_request_seconds = 0.0
        self.batch_retry_count = 0
        self.batch_split_count = 0
        self.json_parse_fail_count = 0
        self.partial_batch_recovered_count = 0
        self.partial_batch_recovered_item_count = 0
        self.validation_fail_count = 0
        self.single_fallback_count = 0
        self.local_quality_fail_count = 0
        self.local_control_fail_count = 0
        self.local_truncation_count = 0
        self.estimated_input_tokens = 0
        self.estimated_output_tokens = 0
        self.estimated_cost_cny = 0.0
        self.actual_input_tokens = 0
        self.actual_output_tokens = 0
        self.actual_cache_hit_input_tokens = 0
        self.actual_cache_miss_input_tokens = 0
        self.actual_cost_cny = 0.0
        self.estimated_saved_input_tokens = 0
        self.estimated_saved_output_tokens = 0
        self.estimated_saved_cost_cny = 0.0
        self.provider = provider
        self.model = model
        self.prompt_version = prompt_version
        self.cache_db = str(cache_db)
        self.run_id = str(run_id or "")
        self.started_at = time.time()

    def add_saved(self, input_tokens: int, output_tokens: int):
        self.estimated_saved_input_tokens += max(0, int(input_tokens))
        self.estimated_saved_output_tokens += max(0, int(output_tokens))
        self.estimated_saved_cost_cny = round(
            self.estimated_saved_cost_cny + cost_cny(input_tokens, output_tokens, self.provider, self.model),
            6,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cache_enabled": self.cache_enabled,
            "cache_auto_cleanup": self.cache_auto_cleanup,
            "cache_max_size_bytes": self.cache_max_size_bytes,
            "cache_pruned_rows": self.cache_pruned_rows,
            "cache_pruned_bytes": self.cache_pruned_bytes,
            "cache_cleanup_count": self.cache_cleanup_count,
            "total_texts": self.total_texts,
            "cache_exact_hit": self.cache_exact_hit,
            "cache_legacy_hit": self.cache_legacy_hit,
            "cache_miss": self.cache_miss,
            "api_request_count": self.api_request_count,
            "dedupe_saved": self.dedupe_saved,
            "batch_request_count": self.batch_request_count,
            "batch_item_count": self.batch_item_count,
            "compact_batch_request_count": self.compact_batch_request_count,
            "compact_batch_item_count": self.compact_batch_item_count,
            "contextual_batch_request_count": self.contextual_batch_request_count,
            "contextual_batch_item_count": self.contextual_batch_item_count,
            "produced_batch_count": self.produced_batch_count,
            "first_batch_ready_seconds": self.first_batch_ready_seconds,
            "first_api_request_seconds": self.first_api_request_seconds,
            "batch_retry_count": self.batch_retry_count,
            "batch_split_count": self.batch_split_count,
            "json_parse_fail_count": self.json_parse_fail_count,
            "partial_batch_recovered_count": self.partial_batch_recovered_count,
            "partial_batch_recovered_item_count": self.partial_batch_recovered_item_count,
            "validation_fail_count": self.validation_fail_count,
            "single_fallback_count": self.single_fallback_count,
            "local_quality_fail_count": self.local_quality_fail_count,
            "local_control_fail_count": self.local_control_fail_count,
            "local_truncation_count": self.local_truncation_count,
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
            "estimated_cost_cny": self.estimated_cost_cny,
            "actual_input_tokens": self.actual_input_tokens,
            "actual_output_tokens": self.actual_output_tokens,
            "actual_cache_hit_input_tokens": self.actual_cache_hit_input_tokens,
            "actual_cache_miss_input_tokens": self.actual_cache_miss_input_tokens,
            "actual_cost_cny": self.actual_cost_cny,
            "estimated_saved_input_tokens": self.estimated_saved_input_tokens,
            "estimated_saved_output_tokens": self.estimated_saved_output_tokens,
            "estimated_saved_cost_cny": self.estimated_saved_cost_cny,
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "cache_db": self.cache_db,
            "run_id": self.run_id,
            "pricing": pricing_dict(self.provider, self.model),
            "elapsed_seconds": round(time.time() - self.started_at, 3),
        }


class TranslationCache:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self.stats = TranslationCacheStats(cache_db=str(self.db_path))
        self._last_cleanup_check = 0.0
        self._create_tables()
        self._refresh_runtime_settings()
        self._maybe_auto_cleanup(force=True)

    def _create_tables(self):
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS cache ("
                "  hash TEXT PRIMARY KEY,"
                "  original TEXT,"
                "  translated TEXT,"
                "  source_lang TEXT,"
                "  target_lang TEXT,"
                "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_langs ON cache(source_lang, target_lang)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS cache_v2 ("
                "  cache_key TEXT PRIMARY KEY,"
                "  provider TEXT NOT NULL,"
                "  model TEXT NOT NULL,"
                "  prompt_version TEXT NOT NULL,"
                "  source_lang TEXT NOT NULL,"
                "  target_lang TEXT NOT NULL,"
                "  text_type TEXT NOT NULL,"
                "  original TEXT NOT NULL,"
                "  normalized_text TEXT NOT NULL,"
                "  translated TEXT NOT NULL,"
                "  created_at INTEGER NOT NULL,"
                "  last_hit_at INTEGER,"
                "  hit_count INTEGER DEFAULT 0"
                ")"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_v2_lookup "
                "ON cache_v2(provider, model, prompt_version, source_lang, target_lang, text_type)"
            )
            self._conn.commit()

    @staticmethod
    def normalize_text(text: str) -> str:
        text = unicodedata.normalize("NFKC", str(text or ""))
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = "\n".join(line.strip() for line in text.split("\n"))
        text = _LINE_SPACE_RE.sub("\n", text)
        text = _SPACE_RE.sub(" ", text)
        return text.strip()

    @staticmethod
    def estimate_tokens(text: str) -> int:
        if not text:
            return 0
        # Conservative CJK-friendly approximation. This is reporting only.
        cjk = sum(1 for ch in text if "\u3040" <= ch <= "\u30ff" or "\u3400" <= ch <= "\u9fff")
        other = max(0, len(text) - cjk)
        return max(1, int(cjk * 1.15 + other / 4))

    @staticmethod
    def infer_text_type(item_or_context: Any = None, meta: dict | None = None) -> str:
        context = ""
        item_meta = meta or {}
        if hasattr(item_or_context, "context"):
            context = str(getattr(item_or_context, "context", "") or "")
            item_meta = getattr(item_or_context, "meta", {}) or item_meta
        elif item_or_context is not None:
            context = str(item_or_context or "")

        raw = str(item_meta.get("kind") or context or "").lower()
        if raw in {"choice", "select", "option"}:
            return "choice"
        if raw in {"name", "speaker", "character"}:
            return "name"
        if raw in {"ui", "system", "menu", "button", "label"}:
            return "ui/system"
        return "message"

    def reset_stats(self, provider: str = DEFAULT_PROVIDER, model: str = DEFAULT_MODEL,
                    prompt_version: str = DEFAULT_PROMPT_VERSION, run_id: str = ""):
        self.stats.reset(
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            cache_db=self.db_path,
            run_id=run_id,
        )

    def stats_dict(self) -> dict[str, Any]:
        self._refresh_runtime_settings()
        return self.stats.to_dict()

    def _refresh_runtime_settings(self) -> tuple[bool, bool, int]:
        enabled, auto_cleanup, max_size_bytes = _cache_runtime_settings()
        self.stats.cache_enabled = enabled
        self.stats.cache_auto_cleanup = auto_cleanup
        self.stats.cache_max_size_bytes = max_size_bytes
        return enabled, auto_cleanup, max_size_bytes

    def _cache_enabled(self) -> bool:
        enabled, _auto_cleanup, _max_size_bytes = self._refresh_runtime_settings()
        return enabled

    def _legacy_key(self, text: str, source_lang: str, target_lang: str) -> str:
        return hashlib.md5(f"{text}|{source_lang}|{target_lang}".encode("utf-8")).hexdigest()

    def _key_v2(self, provider: str, model: str, prompt_version: str,
                source_lang: str, target_lang: str, text_type: str, normalized_text: str) -> str:
        payload = "|".join([
            provider or DEFAULT_PROVIDER,
            model or DEFAULT_MODEL,
            prompt_version or DEFAULT_PROMPT_VERSION,
            source_lang or "",
            target_lang or "",
            text_type or "message",
            normalized_text,
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def lookup(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
        *,
        provider: str = DEFAULT_PROVIDER,
        model: str = DEFAULT_MODEL,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        text_type: str = "message",
        allow_legacy: bool = True,
        count_stats: bool = True,
    ) -> CacheLookup:
        normalized = self.normalize_text(text)
        key = self._key_v2(provider, model, prompt_version, source_lang, target_lang, text_type, normalized)
        if not self._cache_enabled():
            return CacheLookup(None, key, normalized)
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT translated FROM cache_v2 WHERE cache_key = ?", (key,)
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE cache_v2 SET hit_count = hit_count + 1, last_hit_at = ? WHERE cache_key = ?",
                    (now, key),
                )
                self._conn.commit()
                if count_stats:
                    self.stats.cache_exact_hit += 1
                return CacheLookup(row[0], key, normalized, hit=True)

            if allow_legacy:
                legacy = self.get(text, source_lang, target_lang, count_stats=False)
                if legacy:
                    if count_stats:
                        self.stats.cache_legacy_hit += 1
                    return CacheLookup(legacy, key, normalized, hit=True, legacy_hit=True)

            if count_stats:
                self.stats.cache_miss += 1
            return CacheLookup(None, key, normalized)

    def lookup_many(
        self,
        requests: list[CacheLookupRequest],
        *,
        allow_legacy: bool = True,
        count_stats: bool = True,
    ) -> list[CacheLookup]:
        """Batch lookup cache_v2 rows.

        Large KiriKiri/BGI titles can easily enter translation with 50k+ text
        items. Calling ``lookup`` once per item is dominated by Python/SQLite
        round trips, especially with a large cross-game cache. This method keeps
        the exact same cache-key semantics but fetches v2 hits in chunks.
        """
        if not requests:
            return []

        normalized: list[str] = []
        keys: list[str] = []
        for req in requests:
            norm = self.normalize_text(req.text)
            normalized.append(norm)
            keys.append(self._key_v2(
                req.provider,
                req.model,
                req.prompt_version,
                req.source_lang,
                req.target_lang,
                req.text_type,
                norm,
            ))

        if not self._cache_enabled():
            return [CacheLookup(None, key, norm) for key, norm in zip(keys, normalized)]

        rows: dict[str, str] = {}
        now = int(time.time())
        with self._lock:
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                for key, translated in self._conn.execute(
                    f"SELECT cache_key, translated FROM cache_v2 WHERE cache_key IN ({placeholders})",
                    chunk,
                ).fetchall():
                    rows[key] = translated
            hit_keys = [key for key in dict.fromkeys(keys) if key in rows]
            for start in range(0, len(hit_keys), 500):
                chunk = hit_keys[start:start + 500]
                placeholders = ",".join("?" for _ in chunk)
                self._conn.execute(
                    f"UPDATE cache_v2 SET hit_count = hit_count + 1, last_hit_at = ? "
                    f"WHERE cache_key IN ({placeholders})",
                    (now, *chunk),
                )
            if hit_keys:
                self._conn.commit()

            legacy_rows: dict[str, str] = {}
            legacy_keys: list[str] = []
            if allow_legacy:
                legacy_by_key: dict[str, str] = {}
                for req, key in zip(requests, keys):
                    if key in rows:
                        continue
                    legacy_key = self._legacy_key(req.text, req.source_lang, req.target_lang)
                    legacy_by_key[legacy_key] = key
                    legacy_keys.append(legacy_key)
                for start in range(0, len(legacy_keys), 500):
                    chunk = legacy_keys[start:start + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    for legacy_key, translated in self._conn.execute(
                        f"SELECT hash, translated FROM cache WHERE hash IN ({placeholders})",
                        chunk,
                    ).fetchall():
                        cache_key = legacy_by_key.get(legacy_key)
                        if cache_key:
                            legacy_rows[cache_key] = translated

        results: list[CacheLookup] = []
        exact_hits = 0
        legacy_hits = 0
        misses = 0
        for key, norm in zip(keys, normalized):
            translated = rows.get(key)
            if translated is not None:
                exact_hits += 1
                results.append(CacheLookup(translated, key, norm, hit=True))
                continue
            translated = legacy_rows.get(key) if allow_legacy else None
            if translated is not None:
                legacy_hits += 1
                results.append(CacheLookup(translated, key, norm, hit=True, legacy_hit=True))
                continue
            misses += 1
            results.append(CacheLookup(None, key, norm))

        if count_stats:
            self.stats.cache_exact_hit += exact_hits
            self.stats.cache_legacy_hit += legacy_hits
            self.stats.cache_miss += misses
        return results

    def set_v2(
        self,
        original: str,
        translated: str,
        source_lang: str,
        target_lang: str,
        *,
        provider: str = DEFAULT_PROVIDER,
        model: str = DEFAULT_MODEL,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        text_type: str = "message",
    ):
        self.set_many_v2(
            [(original, translated, text_type)],
            source_lang,
            target_lang,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
        )

    def set_many_v2(
        self,
        entries: list[tuple[str, str, str]],
        source_lang: str,
        target_lang: str,
        *,
        provider: str = DEFAULT_PROVIDER,
        model: str = DEFAULT_MODEL,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
    ) -> int:
        if not self._cache_enabled():
            return 0
        now = int(time.time())
        rows = []
        for original, translated, text_type in entries:
            safe, _warnings = verify_translation(original, translated)
            translated = safe
            if (
                not translated
                or not translated.strip()
                or (translated == original and not is_acceptable_same_as_source(original, translated))
            ):
                continue
            normalized = self.normalize_text(original)
            key = self._key_v2(
                provider,
                model,
                prompt_version,
                source_lang,
                target_lang,
                text_type,
                normalized,
            )
            rows.append((
                key, provider, model, prompt_version, source_lang, target_lang, text_type,
                original, normalized, translated, key, now, key, key,
            ))
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO cache_v2 "
                "(cache_key, provider, model, prompt_version, source_lang, target_lang, text_type, "
                " original, normalized_text, translated, created_at, last_hit_at, hit_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "COALESCE((SELECT created_at FROM cache_v2 WHERE cache_key = ?), ?), "
                "COALESCE((SELECT last_hit_at FROM cache_v2 WHERE cache_key = ?), NULL), "
                "COALESCE((SELECT hit_count FROM cache_v2 WHERE cache_key = ?), 0))",
                rows,
            )
            self._conn.commit()
        self._maybe_auto_cleanup()
        return len(rows)

    def get(self, text: str, source_lang: str, target_lang: str, *, count_stats: bool = False) -> str | None:
        if not self._cache_enabled():
            return None
        key = self._legacy_key(text, source_lang, target_lang)
        row = self._conn.execute(
            "SELECT translated FROM cache WHERE hash = ?", (key,)
        ).fetchone()
        if row and count_stats:
            self.stats.cache_legacy_hit += 1
        return row[0] if row else None

    def set(self, original: str, translated: str, source_lang: str, target_lang: str):
        if not self._cache_enabled():
            return
        safe, _warnings = verify_translation(original, translated)
        translated = safe
        if (
            not translated
            or not translated.strip()
            or (translated == original and not is_acceptable_same_as_source(original, translated))
        ):
            return
        key = self._legacy_key(original, source_lang, target_lang)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache (hash, original, translated, source_lang, target_lang) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, original, translated, source_lang, target_lang),
            )
            self._conn.commit()
        self._maybe_auto_cleanup()

    def get_batch(self, texts: list[str], source_lang: str, target_lang: str) -> dict[str, str]:
        result = {}
        for t in texts:
            cached = self.get(t, source_lang, target_lang)
            if cached is not None:
                result[t] = cached
        return result

    def record_api_call(
        self,
        prompt: str,
        result: str,
        usage: Any = None,
        *,
        provider: str | None = None,
        model: str | None = None,
    ):
        self.stats.api_request_count += 1
        provider = str(provider or self.stats.provider or DEFAULT_PROVIDER)
        model = str(model or self.stats.model or DEFAULT_MODEL)
        if self.stats.provider in {DEFAULT_PROVIDER, "unknown", ""} and provider:
            self.stats.provider = provider
        if self.stats.model in {DEFAULT_MODEL, "unknown", ""} and model:
            self.stats.model = model
        estimated_input = self.estimate_tokens(prompt)
        estimated_output = self.estimate_tokens(result)
        self.stats.estimated_input_tokens += estimated_input
        self.stats.estimated_output_tokens += estimated_output
        estimated_delta = cost_cny(estimated_input, estimated_output, provider, model)
        self.stats.estimated_cost_cny = round(
            self.stats.estimated_cost_cny + estimated_delta,
            6,
        )
        actual_delta = 0.0
        if usage is not None:
            prompt_tokens = getattr(usage, "prompt_tokens", None)
            completion_tokens = getattr(usage, "completion_tokens", None)
            if prompt_tokens is None:
                prompt_tokens = getattr(usage, "input_tokens", None)
            if completion_tokens is None:
                completion_tokens = getattr(usage, "output_tokens", None)
            actual_input_delta = int(prompt_tokens) if prompt_tokens is not None else 0
            actual_output_delta = int(completion_tokens) if completion_tokens is not None else 0
            cached_input = _usage_value(usage, "prompt_cache_hit_tokens")
            if cached_input is None:
                cached_input = _usage_value(usage, "cached_tokens")
            if cached_input is None:
                details = _usage_value(usage, "prompt_tokens_details")
                if details is None:
                    details = _usage_value(usage, "input_tokens_details")
                cached_input = _usage_value(details, "cached_tokens")
            cached_input_delta = max(
                0,
                min(actual_input_delta, int(cached_input or 0)),
            )
            uncached_input_delta = max(0, actual_input_delta - cached_input_delta)
            if prompt_tokens is not None:
                self.stats.actual_input_tokens += actual_input_delta
            if completion_tokens is not None:
                self.stats.actual_output_tokens += actual_output_delta
            self.stats.actual_cache_hit_input_tokens += cached_input_delta
            self.stats.actual_cache_miss_input_tokens += uncached_input_delta
            actual_delta = cost_cny(
                actual_input_delta,
                actual_output_delta,
                provider,
                model,
                cache_hit_input_tokens=cached_input_delta,
                cache_miss_input_tokens=uncached_input_delta,
            )
            self.stats.actual_cost_cny = round(
                self.stats.actual_cost_cny + actual_delta,
                6,
            )
        # Keep estimated/actual provider cost in cache stats. It is informational
        # only and never deducts from a local balance or blocks an API request.

    def record_saved_request(self, original: str, translated: str, prompt: str | None = None):
        input_tokens = self.estimate_tokens(prompt if prompt is not None else original)
        output_tokens = self.estimate_tokens(translated)
        self.stats.add_saved(input_tokens, output_tokens)

    def close(self):
        try:
            self._conn.close()
        except Exception as e:
            warning(f"translation cache close failed: {e}")

    def _maybe_auto_cleanup(self, *, force: bool = False) -> None:
        _enabled, auto_cleanup, max_size_bytes = self._refresh_runtime_settings()
        if not auto_cleanup or max_size_bytes <= 0:
            return
        now = time.time()
        if not force and now - self._last_cleanup_check < 30:
            return
        self._last_cleanup_check = now
        try:
            before = self.db_path.stat().st_size if self.db_path.exists() else 0
        except OSError:
            return
        if before <= max_size_bytes:
            return
        target_bytes = max(1, int(max_size_bytes * 0.85))
        rows_deleted = 0
        with self._lock:
            try:
                total_v2 = self._conn.execute("SELECT COUNT(*) FROM cache_v2").fetchone()[0]
                total_legacy = self._conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
                batch = max(100, min(50000, int((total_v2 + total_legacy) * 0.15) or 100))
                if total_v2:
                    cur = self._conn.execute(
                        "DELETE FROM cache_v2 WHERE cache_key IN ("
                        "SELECT cache_key FROM cache_v2 "
                        "ORDER BY COALESCE(last_hit_at, created_at), created_at LIMIT ?"
                        ")",
                        (batch,),
                    )
                    rows_deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                if total_legacy and rows_deleted < batch:
                    cur = self._conn.execute(
                        "DELETE FROM cache WHERE hash IN ("
                        "SELECT hash FROM cache ORDER BY created_at LIMIT ?"
                        ")",
                        (batch - rows_deleted,),
                    )
                    rows_deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                self._conn.commit()
                if rows_deleted:
                    self._conn.execute("VACUUM")
            except Exception as exc:
                warning(f"translation cache cleanup failed: {exc}")
                return
        try:
            after = self.db_path.stat().st_size if self.db_path.exists() else before
        except OSError:
            after = before
        self.stats.cache_cleanup_count += 1
        self.stats.cache_pruned_rows += max(0, rows_deleted)
        self.stats.cache_pruned_bytes += max(0, before - after)
        if after > target_bytes and rows_deleted:
            warning(
                f"translation cache still above limit after cleanup: "
                f"{after / 1024 / 1024:.1f}MB > {max_size_bytes / 1024 / 1024:.1f}MB"
            )


_cache: TranslationCache | None = None


def _cache_runtime_settings() -> tuple[bool, bool, int]:
    try:
        from config import get_config

        config = get_config()
        enabled = bool(getattr(config, "translation_cache_enabled", True))
        auto_cleanup = bool(getattr(config, "translation_cache_auto_cleanup", True))
        max_gb = float(getattr(config, "translation_cache_max_size_gb", 1.0) or 1.0)
    except Exception:
        enabled = True
        auto_cleanup = True
        max_gb = 1.0
    max_gb = max(0.05, min(1024.0, max_gb))
    return enabled, auto_cleanup, int(max_gb * 1024 * 1024 * 1024)


def get_cache() -> TranslationCache:
    global _cache
    if _cache is None:
        _cache = TranslationCache()
    return _cache


def reset_cache_stats(provider: str = DEFAULT_PROVIDER, model: str = DEFAULT_MODEL,
                      prompt_version: str = DEFAULT_PROMPT_VERSION, run_id: str = ""):
    get_cache().reset_stats(
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        run_id=run_id,
    )


def get_cache_stats() -> dict[str, Any]:
    return get_cache().stats_dict()
