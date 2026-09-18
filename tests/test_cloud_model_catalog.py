from __future__ import annotations

import json
from pathlib import Path

from core.cloud_model_catalog import estimate_cost, fetch_model_catalog, game_text_stats, pricing_for_display


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self._payload


def test_model_catalog_parses_models_and_never_returns_api_key(monkeypatch):
    secret = "sk-test-only-do-not-return"

    def fake_get(url, headers, timeout):
        if url.endswith("/models"):
            assert headers["Authorization"] == f"Bearer {secret}"
            return _Response({"data": [
                {"id": "new-model", "owned_by": "provider"},
                {"id": "text-embedding-latest"},
                {"id": "new-model"},
            ]})
        return _Response({})

    monkeypatch.setattr("core.cloud_model_catalog.requests.get", fake_get)
    result = fetch_model_catalog("openai", secret)

    assert result["ok"] is True
    assert [row["id"] for row in result["models"]] == ["new-model"]
    assert secret not in repr(result)


def test_model_catalog_failure_is_safe_and_does_not_include_secret(monkeypatch):
    secret = "sk-secret-value"

    def fake_get(*args, **kwargs):
        return _Response({"error": {"message": "invalid key"}}, status_code=401)

    monkeypatch.setattr("core.cloud_model_catalog.requests.get", fake_get)
    result = fetch_model_catalog("deepseek", secret)

    assert result["ok"] is False
    assert result["models"] == []
    assert "invalid key" in result["error"]
    assert secret not in repr(result)


def test_game_text_stats_reads_checkpoint_without_scanning_assets(tmp_path: Path):
    meta = tmp_path / "_translation_meta"
    meta.mkdir()
    checkpoint = meta / "translation_checkpoint.json"
    checkpoint.write_text(json.dumps({
        "items": [
            {"file": "a.ks", "original": "こんにちは", "translated": "你好"},
            {"file": "a.ks", "original": "さようなら", "translated": "再见"},
        ]
    }), encoding="utf-8")

    result = game_text_stats(tmp_path)

    assert result["available"] is True
    assert result["text_count"] == 2
    assert result["source_chars"] == 10
    assert result["file_count"] == 1


def test_online_price_catalog_overrides_local_estimate(monkeypatch):
    def fake_get(url, headers, timeout):
        return _Response({
            "schema": 1,
            "updated_at": "2026-08-29T00:00:00+08:00",
            "providers": {
                "deepseek": {
                    "models": {
                        "deepseek-current": {
                            "input_cny_per_m": 2.0,
                            "output_cny_per_m": 6.0,
                        }
                    }
                }
            },
        })

    monkeypatch.setattr("core.cloud_model_catalog.requests.get", fake_get)
    result = pricing_for_display("deepseek", "deepseek-current", refresh=True)

    assert result["pricing_source"] == "online_catalog"
    assert result["pricing"]["input_cny_per_m"] == 2.0
    assert result["price_synced_at"] == "2026-08-29T00:00:00+08:00"


def test_estimate_from_counts_uses_selected_online_model_price(monkeypatch, tmp_path: Path):
    def fake_get(url, headers, timeout):
        return _Response({
            "schema": 1,
            "updated_at": "2026-08-29T00:00:00+08:00",
            "providers": {
                "qwen": {
                    "models": {
                        "qwen-custom": {
                            "input_cny_per_m": 3.0,
                            "output_cny_per_m": 9.0,
                        }
                    }
                }
            },
        })

    monkeypatch.setattr("core.cloud_model_catalog.requests.get", fake_get)
    from core.cloud_model_catalog import estimate_game_cost_from_counts

    result = estimate_game_cost_from_counts(
        tmp_path, "qwen", "qwen-custom", text_count=100, source_chars=3000
    )

    assert result["pricing_source"] == "online_catalog"
    assert result["pricing"]["input_cny_per_m"] == 3.0
    assert result["pricing"]["output_cny_per_m"] == 9.0
    assert result["estimated_cost_cny"] > 0
    assert result["is_local"] is False


def test_estimate_cost_reflects_cache_hits():
    cold = estimate_cost("deepseek", "deepseek-v4-flash", text_count=100, source_chars=3000)
    warm = estimate_cost(
        "deepseek", "deepseek-v4-flash", text_count=100, source_chars=3000, cache_hit_ratio=0.8
    )

    assert cold["estimated_input_tokens"] == warm["estimated_input_tokens"]
    assert warm["cache_hit_ratio"] == 0.8
    assert warm["estimated_cost_cny"] < cold["estimated_cost_cny"]


def test_local_estimate_is_explicitly_offline_and_skips_price_network(monkeypatch, tmp_path: Path):
    def fail_network(*_args, **_kwargs):
        raise AssertionError("local estimate must not fetch cloud pricing")

    monkeypatch.setattr("core.cloud_model_catalog.requests.get", fail_network)
    checkpoint = tmp_path / "_translation_meta" / "translation_checkpoint.json"
    checkpoint.parent.mkdir()
    checkpoint.write_text(json.dumps({
        "items": [{"file": "scene.ks", "original": "こんにちは"}],
    }, ensure_ascii=False), encoding="utf-8")

    result = __import__("core.cloud_model_catalog", fromlist=["estimate_game_cost"]).estimate_game_cost(
        tmp_path, "hy_mt2", "Hy-MT2-7B-Q4_K_M"
    )

    assert result["available"] is True
    assert result["is_local"] is True
    assert result["estimated_cost_cny"] == 0
    assert result["pricing_source"] == "offline_model"
