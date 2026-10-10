from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from core.usage_statistics import UsageStatisticsStore


def _success_diagnostics(
    *,
    engine: str = "bgi",
    total: int = 100,
    translated: int = 96,
    provider: str = "deepseek",
    model: str = "deepseek-v4-flash",
) -> dict:
    return {
        "success": True,
        "mode": {"checkpoint": True, "extract_only": False, "patch_only": False},
        "engine": {"name": engine},
        "api_cache_stats": {
            "provider": provider,
            "model": model,
            "total_texts": total,
            "cache_exact_hit": 60,
            "cache_legacy_hit": 5,
            "cache_miss": 35,
            "api_request_count": 3,
            "estimated_input_tokens": 100,
            "estimated_output_tokens": 40,
            "estimated_cost_cny": 0.2,
            "actual_input_tokens": 80,
            "actual_output_tokens": 30,
            "actual_cache_hit_input_tokens": 50,
            "actual_cache_miss_input_tokens": 30,
            "actual_cost_cny": 0.1,
            "estimated_saved_cost_cny": 0.45,
        },
        "translation_stats": {"total": total, "translated": translated},
    }


def test_only_successful_full_translation_is_stored_with_work_title(tmp_path: Path):
    store = UsageStatisticsStore(tmp_path / "usage_statistics_v2.db")
    game_dir = tmp_path / "unhelpful-directory-name"
    data_dir = game_dir / "www" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "System.json").write_text(
        json.dumps({"gameTitle": "作品名来自游戏元数据"}, ensure_ascii=False),
        encoding="utf-8",
    )
    run_id = store.start_run(
        game_title="错误的目录名",
        title_source="directory_fallback",
        game_path=str(game_dir),
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    assert store.finish_run(run_id, _success_diagnostics()) is True

    result = store.query("all")
    summary = result["summary"]
    assert result["schema_version"] == 2
    assert summary["run_count"] == 1
    assert summary["successful_run_count"] == 1
    assert summary["total_tokens"] == 110
    assert summary["display_cost_cny"] == pytest.approx(0.1)
    assert summary["cache_hit_count"] == 65
    assert summary["cache_miss_count"] == 35
    assert result["recent_runs"][0]["game_title"] == "作品名来自游戏元数据"
    assert result["recent_runs"][0]["title_source"] == "rpgmaker_json"
    assert result["recent_runs"][0]["engine"] == "bgi"
    assert "game_dir" not in result["recent_runs"][0]


@pytest.mark.parametrize(
    "diagnostics_patch",
    [
        {"success": False},
        {"mode": {"extract_only": True}},
        {"mode": {"patch_only": True}},
        {"mode": {"polish": True}},
        {"kirikiri_runtime_capture_mode": {"enabled": True}},
        {"fallback": {"mode": "realtime_overlay"}},
        {"translation_stats": {"total": 100, "translated": 0}},
    ],
)
def test_failed_partial_and_realtime_tasks_never_enter_history(
    tmp_path: Path,
    diagnostics_patch: dict,
):
    store = UsageStatisticsStore(tmp_path / "usage_statistics_v2.db")
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    run_id = store.start_run(game_path=str(game_dir), game_title="Should Not Appear")
    diagnostics = _success_diagnostics()
    diagnostics.update(diagnostics_patch)

    assert store.finish_run(run_id, diagnostics) is False
    assert store.query("all")["recent_runs"] == []


def test_local_model_has_tokens_but_never_has_provider_cost(tmp_path: Path):
    store = UsageStatisticsStore(tmp_path / "usage_statistics_v2.db")
    game_dir = tmp_path / "local-game"
    game_dir.mkdir()
    run_id = store.start_run(game_path=str(game_dir), game_title="Local Work")
    diagnostics = _success_diagnostics(provider="hy_mt2", model="Hy-MT2-7B-Q8_0")
    diagnostics["api_cache_stats"].update({
        "actual_input_tokens": 0,
        "actual_output_tokens": 0,
        "actual_cost_cny": 18.0,
        "estimated_input_tokens": 1200,
        "estimated_output_tokens": 900,
        "estimated_cost_cny": 18.0,
    })

    store.finish_run(run_id, diagnostics)
    run = store.query("all")["recent_runs"][0]

    assert run["tokens"] == 2100
    assert run["token_kind"] == "estimated"
    assert run["display_cost_cny"] == 0
    assert run["cost_kind"] == "local"


def test_finish_is_idempotent_and_latest_selects_one_success(tmp_path: Path):
    store = UsageStatisticsStore(tmp_path / "usage_statistics_v2.db")
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = store.start_run(game_path=str(first_dir), game_title="First")
    second = store.start_run(game_path=str(second_dir), game_title="Second")
    diagnostics = _success_diagnostics()
    store.finish_run(first, diagnostics)
    store.finish_run(first, diagnostics)
    time.sleep(0.01)
    store.finish_run(second, diagnostics)

    assert store.query("all")["summary"]["run_count"] == 2
    latest = store.query("latest")
    assert latest["summary"]["run_count"] == 1
    assert latest["recent_runs"][0]["game_title"] == "Second"


def test_range_filter_and_clear_operate_only_on_v2_success_rows(tmp_path: Path):
    db_path = tmp_path / "usage_statistics_v2.db"
    store = UsageStatisticsStore(db_path)
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    old_run = store.start_run(game_path=str(old_dir), game_title="Old")
    new_run = store.start_run(game_path=str(new_dir), game_title="New")
    store.finish_run(old_run, _success_diagnostics())
    store.finish_run(new_run, _success_diagnostics())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE successful_runs SET completed_at = ? WHERE run_id = ?",
            (time.time() - 120 * 86400, old_run),
        )
        connection.commit()

    assert store.query("90d")["summary"]["run_count"] == 1
    assert store.query("all")["summary"]["run_count"] == 2
    store.clear()
    assert store.query("all")["summary"]["run_count"] == 0


def test_unmanaged_translation_cache_calls_do_not_create_usage_sessions(tmp_path: Path, monkeypatch):
    import core.usage_statistics as usage_statistics
    from translators.cache import TranslationCache

    calls: list[tuple] = []
    monkeypatch.setattr(
        usage_statistics,
        "record_usage_api_call",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    cache = TranslationCache(tmp_path / "translation-cache.db")
    cache.record_api_call("prompt", "result")
    cache.record_api_call("prompt 2", "result 2")

    assert calls == []
    assert cache.stats.api_request_count == 2
    assert cache.stats.run_id == ""


def test_database_failure_is_best_effort(tmp_path: Path):
    store = UsageStatisticsStore(tmp_path)
    run_id = store.start_run(game_path=str(tmp_path), game_title="Still Runs")
    assert store.finish_run(run_id, _success_diagnostics()) is False
    assert store.query("30d")["summary"]["run_count"] == 0
