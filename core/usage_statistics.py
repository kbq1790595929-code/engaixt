"""Successful translation history for the statistics page.

Version 2 stores one immutable summary per completed game. It does not read
the legacy per-request database and never stores prompts, translations,
credentials, failed runs, or in-progress runs.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core.game_identity import resolve_game_identity
from core.manifest import game_id_for
from utils.logger import warning


LEGACY_DB_PATH = Path.home() / "Downloads" / ".game_translator" / "usage_statistics.db"
DEFAULT_DB_PATH = Path.home() / "Downloads" / ".game_translator" / "usage_statistics_v2.db"
LOCAL_PROVIDERS = {"hy_mt2"}


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _resolved_default_db_path() -> Path:
    override = os.environ.get("ENGAIXT_USAGE_DB_PATH", "").strip()
    if override:
        return Path(override)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return Path(tempfile.gettempdir()) / "EngAixt-tests" / f"usage_statistics_v2_{os.getpid()}.db"
    return DEFAULT_DB_PATH


class UsageStatisticsStore:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path is not None else _resolved_default_db_path()
        self._lock = threading.RLock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._conn: sqlite3.Connection | None = None
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.execute("PRAGMA busy_timeout = 3000")
            self._conn.row_factory = sqlite3.Row
            self._create_tables()
        except Exception as exc:
            self._warn("usage statistics v2 database unavailable", exc)

    def _warn(self, message: str, exc: BaseException | None = None) -> None:
        warning(f"{message}{f': {exc}' if exc else ''}")

    def _create_tables(self) -> None:
        if self._conn is None:
            return
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS successful_runs (
                    run_id TEXT PRIMARY KEY,
                    completed_at REAL NOT NULL,
                    game_id TEXT NOT NULL,
                    game_title TEXT NOT NULL,
                    title_source TEXT NOT NULL,
                    game_dir TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL DEFAULT '',
                    total_texts INTEGER NOT NULL,
                    translated_texts INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    token_kind TEXT NOT NULL,
                    cost_cny REAL NOT NULL,
                    cost_kind TEXT NOT NULL,
                    estimated_cost_cny REAL NOT NULL,
                    actual_cost_cny REAL NOT NULL,
                    api_request_count INTEGER NOT NULL,
                    cache_hit_count INTEGER NOT NULL,
                    cache_miss_count INTEGER NOT NULL,
                    prompt_cache_hit_tokens INTEGER NOT NULL,
                    prompt_cache_miss_tokens INTEGER NOT NULL,
                    estimated_saved_cost_cny REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_successful_runs_completed
                    ON successful_runs(completed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_successful_runs_game
                    ON successful_runs(game_id, completed_at DESC);
                """
            )
            self._conn.commit()

    def start_run(
        self,
        *,
        game_title: str = "",
        title_source: str = "",
        game_name: str = "",
        game_path: str = "",
        mode: str = "translate",
        provider: str = "",
        model: str = "",
        prompt_version: str = "",
        engine: str = "",
        run_id: str | None = None,
    ) -> str:
        run_id = str(run_id or new_run_id())
        with self._lock:
            self._pending[run_id] = {
                "game_title": str(game_title or game_name or ""),
                "title_source": str(title_source or ""),
                "game_path": str(game_path or ""),
                "mode": str(mode or "translate"),
                "provider": str(provider or ""),
                "model": str(model or ""),
                "prompt_version": str(prompt_version or ""),
                "engine": str(engine or ""),
                "api_request_count": 0,
                "estimated_input_tokens": 0,
                "estimated_output_tokens": 0,
                "estimated_cost_cny": 0.0,
                "actual_input_tokens": 0,
                "actual_output_tokens": 0,
                "actual_cache_hit_input_tokens": 0,
                "actual_cache_miss_input_tokens": 0,
                "actual_cost_cny": 0.0,
            }
        return run_id

    def ensure_run(self, run_id: str, **kwargs: Any) -> str:
        if not run_id:
            return ""
        with self._lock:
            exists = run_id in self._pending
        return run_id if exists else self.start_run(run_id=run_id, **kwargs)

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not run_id:
            return
        allowed = {
            "game_title", "title_source", "game_name", "game_path", "mode",
            "engine", "provider", "model", "prompt_version",
        }
        with self._lock:
            pending = self._pending.get(run_id)
            if pending is None:
                return
            for key, value in fields.items():
                if key not in allowed:
                    continue
                pending["game_title" if key == "game_name" else key] = str(value or "")

    def record_api_call(self, run_id: str, **usage: Any) -> None:
        """Aggregate in memory for compatibility; never create anonymous runs."""
        if not run_id:
            return
        with self._lock:
            pending = self._pending.get(run_id)
            if pending is None:
                return
            pending["api_request_count"] += 1
            for key in (
                "estimated_input_tokens", "estimated_output_tokens",
                "actual_input_tokens", "actual_output_tokens",
                "actual_cache_hit_input_tokens", "actual_cache_miss_input_tokens",
            ):
                pending[key] += max(0, int(usage.get(key) or 0))
            for key in ("estimated_cost_cny", "actual_cost_cny"):
                pending[key] += max(0.0, float(usage.get(key) or 0))
            for key in ("provider", "model", "prompt_version"):
                if usage.get(key):
                    pending[key] = str(usage[key])

    def finish_run(self, run_id: str, diagnostics: dict[str, Any] | None = None) -> bool:
        if not run_id:
            return False
        diagnostics = diagnostics or {}
        with self._lock:
            if self._record_exists(run_id):
                self._pending.pop(run_id, None)
                return True
            pending = self._pending.pop(run_id, {})
        record = _build_success_record(run_id, pending, diagnostics)
        return self._insert_record(record) if record is not None else False

    def _record_exists(self, run_id: str) -> bool:
        if self._conn is None:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM successful_runs WHERE run_id = ? LIMIT 1", (run_id,)
        ).fetchone()
        return row is not None

    def _insert_record(self, record: dict[str, Any]) -> bool:
        try:
            with self._lock:
                if self._conn is None:
                    return False
                columns = tuple(record)
                self._conn.execute(
                    f"INSERT OR IGNORE INTO successful_runs ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    tuple(record[column] for column in columns),
                )
                self._conn.commit()
                return True
        except Exception as exc:
            self._warn("usage statistics v2 success write failed", exc)
            return False

    def clear(self) -> None:
        try:
            with self._lock:
                self._pending.clear()
                if self._conn is not None:
                    self._conn.execute("DELETE FROM successful_runs")
                    self._conn.commit()
        except Exception as exc:
            self._warn("usage statistics v2 clear failed", exc)

    def query(self, range_key: str = "30d") -> dict[str, Any]:
        key = str(range_key or "30d").lower()
        if key not in {"latest", "7d", "30d", "90d", "all"}:
            key = "30d"
        return _aggregate_rows(key, self._query_rows(key))

    def _query_rows(self, range_key: str) -> list[sqlite3.Row]:
        if self._conn is None:
            return []
        try:
            with self._lock:
                if range_key == "latest":
                    return list(self._conn.execute(
                        "SELECT * FROM successful_runs ORDER BY completed_at DESC LIMIT 1"
                    ).fetchall())
                if range_key == "all":
                    return list(self._conn.execute(
                        "SELECT * FROM successful_runs ORDER BY completed_at DESC"
                    ).fetchall())
                start = time.time() - {"7d": 7, "30d": 30, "90d": 90}[range_key] * 86400
                return list(self._conn.execute(
                    "SELECT * FROM successful_runs WHERE completed_at >= ? ORDER BY completed_at DESC",
                    (start,),
                ).fetchall())
        except Exception as exc:
            self._warn("usage statistics v2 query failed", exc)
            return []


def _build_success_record(
    run_id: str,
    pending: dict[str, Any],
    diagnostics: dict[str, Any],
) -> dict[str, Any] | None:
    if not _eligible_for_history(diagnostics):
        return None

    cache = diagnostics.get("api_cache_stats") or {}
    translation = diagnostics.get("translation_stats") or {}
    engine_data = diagnostics.get("engine") or {}
    engine = str(engine_data.get("name") or "") if isinstance(engine_data, dict) else str(engine_data or "")
    engine = engine or str(pending.get("engine") or "")
    total_texts = max(0, int(cache.get("total_texts") or translation.get("total") or 0))
    translated_texts = max(0, int(translation.get("translated") or 0))
    if not engine or total_texts <= 0 or translated_texts <= 0:
        return None

    game_path = _final_game_path(pending, diagnostics)
    identity_data = diagnostics.get("game_identity") or {}
    try:
        identity = resolve_game_identity(game_path)
    except Exception:
        identity = None
    diagnostic_source = str(identity_data.get("source") or "")
    pending_source = str(pending.get("title_source") or "")
    if identity_data.get("title") and diagnostic_source != "directory_fallback":
        game_title = str(identity_data["title"])
        title_source = diagnostic_source or "diagnostics"
    elif pending.get("game_title") and pending_source != "directory_fallback":
        game_title = str(pending["game_title"])
        title_source = pending_source or "pipeline"
    else:
        game_title = str((identity.title if identity else "") or pending.get("game_title") or "未命名作品")
        title_source = str((identity.source if identity else "") or pending_source or "unknown")
    game_dir = identity.game_dir if identity else (game_path if game_path.is_dir() else game_path.parent)

    provider = str(cache.get("provider") or pending.get("provider") or "unknown")
    model = str(cache.get("model") or pending.get("model") or "unknown")
    estimated_input = max(0, int(cache.get("estimated_input_tokens") or pending.get("estimated_input_tokens") or 0))
    estimated_output = max(0, int(cache.get("estimated_output_tokens") or pending.get("estimated_output_tokens") or 0))
    actual_input = max(0, int(cache.get("actual_input_tokens") or pending.get("actual_input_tokens") or 0))
    actual_output = max(0, int(cache.get("actual_output_tokens") or pending.get("actual_output_tokens") or 0))
    has_actual_tokens = actual_input + actual_output > 0
    input_tokens = actual_input if has_actual_tokens else estimated_input
    output_tokens = actual_output if has_actual_tokens else estimated_output
    token_kind = "actual" if has_actual_tokens else "estimated"

    estimated_cost = max(0.0, float(cache.get("estimated_cost_cny") or pending.get("estimated_cost_cny") or 0))
    actual_cost = max(0.0, float(cache.get("actual_cost_cny") or pending.get("actual_cost_cny") or 0))
    if provider.casefold() in LOCAL_PROVIDERS:
        cost_cny, cost_kind = 0.0, "local"
    elif actual_cost > 0:
        cost_cny, cost_kind = actual_cost, "actual"
    else:
        cost_cny, cost_kind = estimated_cost, "estimated"

    return {
        "run_id": run_id,
        "completed_at": time.time(),
        "game_id": game_id_for(game_dir),
        "game_title": game_title,
        "title_source": title_source,
        "game_dir": str(game_dir),
        "engine": engine,
        "provider": provider,
        "model": model,
        "prompt_version": str(cache.get("prompt_version") or pending.get("prompt_version") or ""),
        "total_texts": total_texts,
        "translated_texts": translated_texts,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "token_kind": token_kind,
        "cost_cny": round(cost_cny, 6),
        "cost_kind": cost_kind,
        "estimated_cost_cny": round(estimated_cost, 6),
        "actual_cost_cny": round(actual_cost, 6),
        "api_request_count": max(0, int(cache.get("api_request_count") or pending.get("api_request_count") or 0)),
        "cache_hit_count": max(0, int(cache.get("cache_exact_hit") or 0)) + max(0, int(cache.get("cache_legacy_hit") or 0)),
        "cache_miss_count": max(0, int(cache.get("cache_miss") or 0)),
        "prompt_cache_hit_tokens": max(0, int(cache.get("actual_cache_hit_input_tokens") or 0)),
        "prompt_cache_miss_tokens": max(0, int(cache.get("actual_cache_miss_input_tokens") or 0)),
        "estimated_saved_cost_cny": round(max(0.0, float(cache.get("estimated_saved_cost_cny") or 0)), 6),
    }


def _eligible_for_history(diagnostics: dict[str, Any]) -> bool:
    if diagnostics.get("success") is not True:
        return False
    mode = diagnostics.get("mode") or {}
    if isinstance(mode, dict) and any(
        bool(mode.get(key)) for key in ("extract_only", "patch_only", "polish", "preflight_only")
    ):
        return False
    if diagnostics.get("kirikiri_runtime_capture_mode") or diagnostics.get("fallback"):
        return False
    return True


def _final_game_path(pending: dict[str, Any], diagnostics: dict[str, Any]) -> Path:
    for value in (
        diagnostics.get("resolved_game_path"),
        diagnostics.get("resolved_input_path"),
        pending.get("game_path"),
    ):
        if value:
            return Path(str(value)).expanduser().resolve()
    return Path.cwd()


def _aggregate_rows(range_key: str, rows: list[sqlite3.Row]) -> dict[str, Any]:
    summary = {
        "total_tokens": 0, "input_tokens": 0, "output_tokens": 0,
        "display_cost_cny": 0.0, "display_cost_kind": "none",
        "api_request_count": 0, "cache_hit_count": 0, "cache_miss_count": 0,
        "cache_hit_rate": 0.0, "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0, "estimated_saved_cost_cny": 0.0,
        "run_count": len(rows), "successful_run_count": len(rows),
        "total_texts": 0, "translated_texts": 0,
    }
    trend: dict[str, dict[str, Any]] = {}
    providers: dict[tuple[str, str], dict[str, Any]] = {}
    engines: dict[str, dict[str, Any]] = {}
    cost_kinds: set[str] = set()
    recent_runs: list[dict[str, Any]] = []

    for row in rows:
        summary["total_tokens"] += int(row["total_tokens"] or 0)
        summary["input_tokens"] += int(row["input_tokens"] or 0)
        summary["output_tokens"] += int(row["output_tokens"] or 0)
        summary["display_cost_cny"] += float(row["cost_cny"] or 0)
        summary["api_request_count"] += int(row["api_request_count"] or 0)
        summary["cache_hit_count"] += int(row["cache_hit_count"] or 0)
        summary["cache_miss_count"] += int(row["cache_miss_count"] or 0)
        summary["prompt_cache_hit_tokens"] += int(row["prompt_cache_hit_tokens"] or 0)
        summary["prompt_cache_miss_tokens"] += int(row["prompt_cache_miss_tokens"] or 0)
        summary["estimated_saved_cost_cny"] += float(row["estimated_saved_cost_cny"] or 0)
        summary["total_texts"] += int(row["total_texts"] or 0)
        summary["translated_texts"] += int(row["translated_texts"] or 0)
        cost_kinds.add(str(row["cost_kind"] or "estimated"))

        date = datetime.fromtimestamp(float(row["completed_at"])).strftime("%Y-%m-%d")
        day = trend.setdefault(date, {"date": date, "tokens": 0, "cost_cny": 0.0, "runs": 0})
        day["tokens"] += int(row["total_tokens"] or 0)
        day["cost_cny"] += float(row["cost_cny"] or 0)
        day["runs"] += 1

        provider_key = (str(row["provider"] or "unknown"), str(row["model"] or "unknown"))
        provider = providers.setdefault(provider_key, {
            "provider": provider_key[0], "model": provider_key[1],
            "tokens": 0, "cost_cny": 0.0, "requests": 0, "runs": 0,
        })
        provider["tokens"] += int(row["total_tokens"] or 0)
        provider["cost_cny"] += float(row["cost_cny"] or 0)
        provider["requests"] += int(row["api_request_count"] or 0)
        provider["runs"] += 1

        engine_key = str(row["engine"] or "unknown")
        engine = engines.setdefault(engine_key, {"engine": engine_key, "runs": 0, "texts": 0, "translated": 0})
        engine["runs"] += 1
        engine["texts"] += int(row["total_texts"] or 0)
        engine["translated"] += int(row["translated_texts"] or 0)

        recent_runs.append({
            "run_id": row["run_id"], "game_title": row["game_title"],
            "game_name": row["game_title"], "title_source": row["title_source"],
            "engine": row["engine"], "provider": row["provider"], "model": row["model"],
            "tokens": int(row["total_tokens"] or 0), "token_kind": row["token_kind"],
            "display_cost_cny": round(float(row["cost_cny"] or 0), 6),
            "cost_kind": row["cost_kind"], "total_texts": int(row["total_texts"] or 0),
            "translated_texts": int(row["translated_texts"] or 0),
            "api_request_count": int(row["api_request_count"] or 0), "status": "success",
            "completed_at": datetime.fromtimestamp(float(row["completed_at"])).isoformat(timespec="seconds"),
        })

    cache_total = summary["cache_hit_count"] + summary["cache_miss_count"]
    summary["cache_hit_rate"] = round(summary["cache_hit_count"] / cache_total, 4) if cache_total else 0.0
    summary["display_cost_cny"] = round(summary["display_cost_cny"], 6)
    summary["estimated_saved_cost_cny"] = round(summary["estimated_saved_cost_cny"], 6)
    summary["display_cost_kind"] = next(iter(cost_kinds)) if len(cost_kinds) == 1 else ("mixed" if cost_kinds else "none")

    return {
        "schema_version": 2,
        "range": range_key,
        "from": None if range_key in {"latest", "all"} else datetime.fromtimestamp(
            time.time() - {"7d": 7, "30d": 30, "90d": 90}[range_key] * 86400
        ).isoformat(timespec="seconds"),
        "to": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "trend": [
            {**item, "cost_cny": round(item["cost_cny"], 6)}
            for item in sorted(trend.values(), key=lambda value: value["date"])
        ],
        "providers": sorted(
            ({**item, "cost_cny": round(item["cost_cny"], 6)} for item in providers.values()),
            key=lambda item: (-item["tokens"], item["provider"], item["model"]),
        ),
        "engines": sorted(engines.values(), key=lambda item: (-item["runs"], item["engine"])),
        "recent_runs": recent_runs[:50],
    }


_STORE: UsageStatisticsStore | None = None
_STORE_PATH: Path | None = None
_STORE_LOCK = threading.Lock()


def get_usage_store() -> UsageStatisticsStore:
    global _STORE, _STORE_PATH
    target = _resolved_default_db_path()
    with _STORE_LOCK:
        if _STORE is None or _STORE_PATH != target:
            _STORE = UsageStatisticsStore(target)
            _STORE_PATH = target
        return _STORE


def start_usage_run(**kwargs: Any) -> str:
    return get_usage_store().start_run(**kwargs)


def update_usage_run(run_id: str, **fields: Any) -> None:
    get_usage_store().update_run(run_id, **fields)


def record_usage_api_call(run_id: str, **usage: Any) -> None:
    get_usage_store().record_api_call(run_id, **usage)


def finish_usage_run(run_id: str, diagnostics: dict[str, Any] | None = None) -> bool:
    return get_usage_store().finish_run(run_id, diagnostics)


def get_usage_statistics(range_key: str = "30d") -> dict[str, Any]:
    return get_usage_store().query(range_key)


def clear_usage_statistics() -> None:
    get_usage_store().clear()
