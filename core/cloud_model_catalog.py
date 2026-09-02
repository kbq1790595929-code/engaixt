"""Cloud model discovery and translation cost estimates.

Provider model endpoints expose model IDs, but generally do not expose a
stable public price table.  Model IDs are therefore discovered remotely while
prices are resolved through the project's provider pricing registry.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from translators.cache import TranslationCache
from translators.pricing import deepseek_pricing_period, pricing_dict


_PROVIDER_SPECS: dict[str, dict[str, Any]] = {
    "openai": {
        "url": "https://api.openai.com/v1/models",
        "key_header": "Authorization",
        "auth": "bearer",
    },
    "deepseek": {
        "url": "https://api.deepseek.com/models",
        "key_header": "Authorization",
        "auth": "bearer",
    },
    "qwen": {
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
        "key_header": "Authorization",
        "auth": "bearer",
    },
    "zhipu": {
        "url": "https://open.bigmodel.cn/api/paas/v4/models",
        "key_header": "Authorization",
        "auth": "bearer",
    },
    "moonshot": {
        "url": "https://api.moonshot.cn/v1/models",
        "key_header": "Authorization",
        "auth": "bearer",
    },
    "doubao": {
        "url": "https://ark.cn-beijing.volces.com/api/v3/models",
        "key_header": "Authorization",
        "auth": "bearer",
    },
    "anthropic": {
        "url": "https://api.anthropic.com/v1/models",
        "key_header": "x-api-key",
        "auth": "raw",
        "extra_headers": {"anthropic-version": "2023-06-01"},
    },
}

_DEFAULT_PRICE_CATALOG_URL = "https://engaixt.com/updates/cloud-pricing.json"
_REMOTE_PRICE_CACHE: dict[str, Any] = {
    "loaded_at": 0.0,
    "providers": {},
    "updated_at": "",
}


def provider_specs() -> dict[str, dict[str, Any]]:
    """Return non-sensitive endpoint metadata for diagnostics/tests."""
    return {
        name: {key: value for key, value in spec.items() if key != "extra_headers"}
        for name, spec in _PROVIDER_SPECS.items()
    }


def _error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
            else:
                message = error
            if message:
                return str(message)[:240]
    except Exception:
        pass
    return f"HTTP {response.status_code}"


def _model_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw_models = payload.get("data")
        if not isinstance(raw_models, list):
            raw_models = payload.get("models")
    elif isinstance(payload, list):
        raw_models = payload
    else:
        raw_models = []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_models or []:
        if isinstance(raw, str):
            model_id = raw.strip()
            owner = ""
        elif isinstance(raw, dict):
            model_id = str(raw.get("id") or raw.get("model") or raw.get("name") or "").strip()
            owner = str(raw.get("owned_by") or raw.get("provider") or "").strip()
        else:
            continue
        if not model_id or model_id in seen:
            continue
        if _looks_non_chat_model(model_id):
            continue
        seen.add(model_id)
        rows.append({
            "id": model_id,
            "label": model_id,
            "owner": owner,
            "api_pricing": _extract_api_pricing(raw),
        })
    return sorted(rows, key=lambda row: row["id"].casefold())


def _looks_non_chat_model(model_id: str) -> bool:
    """Keep model selectors focused on models usable by chat translation."""
    lowered = model_id.casefold().replace("_", "-")
    blocked = (
        "embedding", "moderation", "whisper", "tts", "text-to-speech",
        "transcribe", "dall-e", "image", "realtime", "audio-preview",
    )
    return any(token in lowered for token in blocked)


def _extract_api_pricing(raw: Any) -> dict[str, Any] | None:
    """Accept only an explicit CNY-per-million pricing shape from a provider."""
    if not isinstance(raw, dict):
        return None
    candidate = raw.get("pricing") or raw.get("pricing_info")
    if not isinstance(candidate, dict):
        return None
    input_rate = candidate.get("input_cny_per_m")
    output_rate = candidate.get("output_cny_per_m")
    if input_rate is None or output_rate is None:
        return None
    try:
        return {
            "input_cny_per_m": float(input_rate),
            "output_cny_per_m": float(output_rate),
            "note": "provider-api-pricing",
        }
    except (TypeError, ValueError):
        return None


def _normalize_price_override(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    source = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else raw
    required = ("input_cny_per_m", "output_cny_per_m")
    if any(source.get(key) is None for key in required):
        return None
    result: dict[str, Any] = {}
    numeric_fields = (
        "input_cny_per_m", "output_cny_per_m", "input_cache_hit_cny_per_m",
        "input_cache_miss_cny_per_m", "peak_input_cache_hit_cny_per_m",
        "peak_input_cache_miss_cny_per_m", "peak_output_cny_per_m",
    )
    try:
        for key in numeric_fields:
            if source.get(key) is not None:
                result[key] = float(source[key])
    except (TypeError, ValueError):
        return None
    if "period" in source:
        result["period"] = str(source["period"])
    result["note"] = "online-price-catalog"
    return result


def _price_catalog_url() -> str:
    return os.environ.get("ENGAIXT_CLOUD_PRICING_URL", _DEFAULT_PRICE_CATALOG_URL).strip()


def _load_remote_price_catalog(*, force: bool = False, timeout: float = 4.0) -> tuple[dict[str, Any], str]:
    now = time.monotonic()
    if not force and now - float(_REMOTE_PRICE_CACHE["loaded_at"]) < 300:
        return _REMOTE_PRICE_CACHE["providers"], _REMOTE_PRICE_CACHE["updated_at"]
    url = _price_catalog_url()
    if not url:
        return {}, ""
    providers: dict[str, Any] = {}
    updated_at = ""
    try:
        response = requests.get(url, headers={"Accept": "application/json"}, timeout=timeout)
        if response.ok:
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("providers"), dict):
                providers = payload["providers"]
                updated_at = str(payload.get("updated_at") or "")
    except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
        providers = {}
    _REMOTE_PRICE_CACHE.update({"loaded_at": now, "providers": providers, "updated_at": updated_at})
    return providers, updated_at


def _remote_price_for(provider: str, model: str, *, refresh: bool = False) -> tuple[dict[str, Any] | None, str]:
    providers, updated_at = _load_remote_price_catalog(force=refresh)
    price = _remote_price_from_providers(providers, provider, model)
    if price is None and not refresh:
        # The cache is shared by all providers. If the previous lookup loaded
        # only another provider (or an older catalog), refresh once so a newly
        # selected model is not incorrectly shown with a stale local fallback.
        providers, updated_at = _load_remote_price_catalog(force=True)
        price = _remote_price_from_providers(providers, provider, model)
    return price, updated_at


def _remote_price_from_providers(providers: dict[str, Any], provider: str, model: str) -> dict[str, Any] | None:
    provider_data = providers.get(str(provider or "").strip().lower())
    if not isinstance(provider_data, dict):
        return None
    models = provider_data.get("models")
    raw = models.get(str(model or "")) if isinstance(models, dict) else None
    if raw is None:
        raw = provider_data.get("default")
    return _normalize_price_override(raw)


def pricing_for_display(provider: str | None, model: str | None, *, refresh: bool = False) -> dict[str, Any]:
    """Return pricing plus its source for the settings UI and estimates."""
    if str(provider or "").strip().lower() == "hy_mt2":
        # Offline inference has no provider API tariff and should not trigger
        # a network request just to render a zero-cost estimate.
        return {
            "pricing": pricing_dict(provider, model),
            "pricing_source": "offline_model",
            "price_synced_at": "",
        }
    local = pricing_dict(provider, model)
    remote, updated_at = _remote_price_for(str(provider or ""), str(model or ""), refresh=refresh)
    if remote:
        merged = {**local, **remote}
        return {"pricing": merged, "pricing_source": "online_catalog", "price_synced_at": updated_at}
    return {"pricing": local, "pricing_source": "local_estimate", "price_synced_at": ""}


def _with_prices(provider: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    providers, updated_at = _load_remote_price_catalog(force=True)
    result = []
    for row in rows:
        remote = _remote_price_from_providers(providers, provider, row["id"])
        pricing = row.get("api_pricing") or remote or pricing_dict(provider, row["id"])
        source = "provider_api" if row.get("api_pricing") else "online_catalog" if remote else "local_estimate"
        result.append({
            "id": row["id"],
            "label": row["label"],
            "owner": row.get("owner", ""),
            "pricing": pricing,
            "pricing_source": source,
            "price_synced_at": updated_at if source == "online_catalog" else "",
        })
    return result


def _empty_result(provider: str, *, error: str = "") -> dict[str, Any]:
    display = pricing_for_display(provider, "", refresh=bool(error == ""))
    return {
        "ok": not bool(error),
        "provider": provider,
        "models": [],
        "pricing": display["pricing"],
        "pricing_source": display["pricing_source"],
        "price_synced_at": display["price_synced_at"],
        "fetched_at": "",
        "error": error,
    }


def fetch_model_catalog(provider: str, api_key: str, *, timeout: float = 12.0) -> dict[str, Any]:
    """Fetch a provider model list without ever returning the API key."""
    provider_key = str(provider or "").strip().lower()
    spec = _PROVIDER_SPECS.get(provider_key)
    if not spec:
        return _empty_result(provider_key, error="该服务商暂不支持动态模型列表")
    if not str(api_key or "").strip():
        return _empty_result(provider_key, error="未配置 API Key")

    headers = {"Accept": "application/json"}
    if spec.get("auth") == "bearer":
        headers[spec["key_header"]] = f"Bearer {api_key}"
    else:
        headers[spec["key_header"]] = str(api_key)
    headers.update(spec.get("extra_headers") or {})
    try:
        response = requests.get(spec["url"], headers=headers, timeout=timeout)
        if not response.ok:
            return _empty_result(provider_key, error=_error_message(response))
        rows = _with_prices(provider_key, _model_rows(response.json()))
        return {
            "ok": True,
            "provider": provider_key,
            "models": rows,
            "pricing": rows[0]["pricing"] if rows else pricing_for_display(provider_key, "", refresh=False)["pricing"],
            "pricing_source": rows[0]["pricing_source"] if rows else "local_estimate",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "error": "" if rows else "接口未返回可用模型",
        }
    except requests.RequestException as exc:
        return _empty_result(provider_key, error=f"请求失败: {str(exc)[:220]}")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return _empty_result(provider_key, error=f"响应解析失败: {str(exc)[:220]}")


def _checkpoint_candidates(path: str | Path) -> list[Path]:
    selected = Path(path).expanduser()
    game_dir = selected if selected.is_dir() else selected.parent
    return [
        game_dir / "_translation_meta" / "translation_checkpoint.json",
        game_dir / "translation_checkpoint.json",
        game_dir / "_translation_meta" / "extract_diagnostics.json",
        game_dir / "_translation_meta" / "extraction_stats.json",
    ]


def game_text_stats(path: str | Path) -> dict[str, Any]:
    """Read already-produced extraction metadata without scanning game assets."""
    for candidate in _checkpoint_candidates(path):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        raw_items = data.get("items") if isinstance(data, dict) else None
        if isinstance(raw_items, list):
            items = [item for item in raw_items if isinstance(item, dict) and str(item.get("original") or "")]
            source_chars = sum(len(str(item.get("original") or "")) for item in items)
            return {
                "available": True,
                "text_count": len(items),
                "source_chars": source_chars,
                "file_count": len({str(item.get("file") or "") for item in items}),
                "source": str(candidate),
            }
        if isinstance(data, dict) and int(data.get("text_count") or 0) > 0:
            return {
                "available": True,
                "text_count": int(data["text_count"]),
                "source_chars": int(data.get("source_chars") or 0),
                "file_count": int(data.get("file_count") or 0),
                "source": str(candidate),
            }
    return {"available": False, "text_count": 0, "source_chars": 0, "file_count": 0, "source": ""}


def _estimate_tokens(text_count: int, source_chars: int) -> tuple[int, int, bool]:
    count = max(0, int(text_count or 0))
    chars = max(0, int(source_chars or 0))
    assumed = chars <= 0 and count > 0
    if assumed:
        chars = count * 30
    source_tokens = max(0, int(chars * 1.15))
    input_tokens = source_tokens + count * 24
    output_tokens = max(0, int(source_tokens * 1.05 + count * 8))
    return input_tokens, output_tokens, assumed


def estimate_cost(
    provider: str,
    model: str,
    *,
    text_count: int,
    source_chars: int = 0,
    cache_hit_ratio: float = 0.0,
    pricing_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    input_tokens, output_tokens, assumed = _estimate_tokens(text_count, source_chars)
    ratio = max(0.0, min(1.0, float(cache_hit_ratio or 0.0)))
    hit_tokens = int(input_tokens * ratio)
    miss_tokens = input_tokens - hit_tokens
    display_pricing = pricing_override or pricing_dict(provider, model)
    provider_key = str(provider or "").strip().lower()
    if provider_key in {"deepseek", "generic", ""} and display_pricing.get("input_cache_hit_cny_per_m") is not None:
        period = str(display_pricing.get("period") or deepseek_pricing_period())
        hit_rate = display_pricing.get(
            "peak_input_cache_hit_cny_per_m" if period == "peak" else "input_cache_hit_cny_per_m",
            display_pricing.get("input_cny_per_m", 0.0),
        )
        miss_rate = display_pricing.get(
            "peak_input_cache_miss_cny_per_m" if period == "peak" else "input_cache_miss_cny_per_m",
            display_pricing.get("input_cny_per_m", 0.0),
        )
        output_rate = display_pricing.get(
            "peak_output_cny_per_m" if period == "peak" else "output_cny_per_m",
            0.0,
        )
        amount = round(
            hit_tokens / 1_000_000 * float(hit_rate or 0.0)
            + miss_tokens / 1_000_000 * float(miss_rate or 0.0)
            + output_tokens / 1_000_000 * float(output_rate or 0.0),
            6,
        )
    else:
        amount = round(
            input_tokens / 1_000_000 * float(display_pricing.get("input_cny_per_m", 0.0) or 0.0)
            + output_tokens / 1_000_000 * float(display_pricing.get("output_cny_per_m", 0.0) or 0.0),
            6,
        )
    return {
        "provider": str(provider or "").strip().lower(),
        "model": str(model or ""),
        "text_count": max(0, int(text_count or 0)),
        "source_chars": max(0, int(source_chars or 0)),
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "cache_hit_ratio": ratio,
        "cache_hit_input_tokens": hit_tokens,
        "cache_miss_input_tokens": miss_tokens,
        "estimated_cost_cny": amount,
        "pricing": display_pricing,
        "pricing_source": "online_catalog" if pricing_override else "local_estimate",
        "assumed_source_chars": assumed,
        "confidence": "low" if assumed else "medium",
        "note": "仅为预估，实际费用以服务商返回的 usage 为准",
    }


def estimate_game_cost(path: str | Path, provider: str, model: str) -> dict[str, Any]:
    stats = game_text_stats(path)
    display = pricing_for_display(provider, model)
    result = estimate_cost(
        provider,
        model,
        text_count=stats["text_count"],
        source_chars=stats["source_chars"],
        pricing_override=display["pricing"],
    )
    result["pricing_source"] = display["pricing_source"]
    result["price_synced_at"] = display["price_synced_at"]
    result["is_local"] = str(provider or "").strip().lower() == "hy_mt2"
    result["available"] = bool(stats["available"])
    result["file_count"] = stats["file_count"]
    result["source"] = stats["source"]
    return result


def estimate_game_cost_from_counts(
    path: str | Path,
    provider: str,
    model: str,
    *,
    text_count: int,
    source_chars: int = 0,
) -> dict[str, Any]:
    """Estimate from live extraction metadata while the selected game runs."""
    display = pricing_for_display(provider, model)
    result = estimate_cost(
        provider,
        model,
        text_count=text_count,
        source_chars=source_chars,
        pricing_override=display["pricing"],
    )
    result.update({
        "available": True,
        "file_count": 0,
        "source": "live_extraction",
        "pricing_source": display["pricing_source"],
        "price_synced_at": display["price_synced_at"],
        "is_local": str(provider or "").strip().lower() == "hy_mt2",
    })
    return result
