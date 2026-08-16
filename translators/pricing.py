from __future__ import annotations

from dataclasses import dataclass


USD_TO_CNY = 7.2


@dataclass(frozen=True)
class ModelPricing:
    input_cny_per_m: float
    output_cny_per_m: float
    note: str


DEEPSEEK_V4_FLASH = ModelPricing(1.0, 2.0, "deepseek-v4-flash")
UNKNOWN_FALLBACK = ModelPricing(1.0, 2.0, "unknown-fallback-deepseek-v4-flash")


def pricing_for(provider: str | None, model: str | None) -> ModelPricing:
    provider_key = str(provider or "").strip().lower()
    model_key = str(model or "").strip().lower()

    if provider_key == "hy_mt2":
        return ModelPricing(0.0, 0.0, "local-offline-no-api-cost")

    if provider_key in {"deepseek", "generic", ""}:
        if "pro" in model_key:
            return ModelPricing(3.132, 6.264, "deepseek-v4-pro-usd-estimate")
        return DEEPSEEK_V4_FLASH

    if provider_key == "qwen":
        if "turbo" in model_key or "flash" in model_key:
            return ModelPricing(1.2, 7.2, "qwen3.6-flash-estimate")
        if "max" in model_key:
            return ModelPricing(12.0, 36.0, "qwen3.7-max-estimate")
        return ModelPricing(2.0, 8.0, "qwen3.7-plus-estimate")

    if provider_key == "zhipu":
        if "glm-4.7-flash" in model_key or "glm-4-flash" in model_key:
            return ModelPricing(0.0, 0.0, "glm-flash-free")
        if "flashx" in model_key or "flash-x" in model_key:
            return ModelPricing(0.5, 3.0, "glm-flashx-estimate")
        if "glm-5.2" in model_key or "glm-5.1" in model_key:
            return _usd(1.4, 4.4, "glm-5.2/5.1-usd-estimate")
        if "glm-5" in model_key:
            return _usd(1.0, 3.2, "glm-5-usd-estimate")
        if "glm-4.7" in model_key:
            return _usd(0.6, 2.2, "glm-4.7-usd-estimate")
        return ModelPricing(0.0, 0.0, "glm-free-fallback")

    if provider_key == "moonshot":
        if model_key.startswith("moonshot-v1-128k"):
            return ModelPricing(60.0, 60.0, "moonshot-v1-128k-estimate")
        if model_key.startswith("moonshot-v1-32k"):
            return ModelPricing(24.0, 24.0, "moonshot-v1-32k-estimate")
        if model_key.startswith("moonshot-v1-8k"):
            return ModelPricing(12.0, 12.0, "moonshot-v1-8k-estimate")
        if "k2.7" in model_key:
            return _usd(0.95, 4.0, "kimi-k2.7-code-usd-estimate")
        if "k2.6" in model_key:
            return _usd(0.6, 2.5, "kimi-k2.6-usd-estimate")
        return _usd(0.6, 2.5, "kimi-k-estimate")

    if provider_key == "doubao":
        if "2-1-pro" in model_key or "2.1-pro" in model_key:
            return ModelPricing(6.0, 30.0, "doubao-seed-2.1-pro-estimate")
        if "2-1-turbo" in model_key or "2.1-turbo" in model_key:
            return ModelPricing(3.0, 15.0, "doubao-seed-2.1-turbo-estimate")
        if "flash" in model_key:
            return ModelPricing(0.3, 0.6, "doubao-flash-estimate")
        if "turbo" in model_key:
            return ModelPricing(3.0, 15.0, "doubao-turbo-estimate")
        return ModelPricing(1.0, 2.0, "doubao-estimate")

    if provider_key == "openai":
        if "5.5" in model_key:
            return _usd(5.0, 30.0, "openai-gpt-5.5-estimate")
        if "5.4-mini" in model_key:
            return _usd(0.75, 4.5, "openai-gpt-5.4-mini-estimate")
        if "5.4-nano" in model_key:
            return _usd(0.15, 1.2, "openai-gpt-5.4-nano-estimate")
        if "5.4" in model_key:
            return _usd(2.5, 15.0, "openai-gpt-5.4-estimate")
        if "nano" in model_key:
            return _usd(0.05, 0.4, "openai-nano-estimate")
        if "4o-mini" in model_key:
            return _usd(0.15, 0.6, "openai-4o-mini-estimate")
        if "4.1-mini" in model_key:
            return _usd(0.4, 1.6, "openai-4.1-mini-estimate")
        if "mini" in model_key:
            return _usd(0.75, 4.5, "openai-mini-estimate")
        return _usd(2.0, 8.0, "openai-standard-estimate")

    if provider_key == "anthropic":
        if "haiku" in model_key:
            return _usd(1.0, 5.0, "claude-haiku-estimate")
        if "fable" in model_key or "mythos" in model_key:
            return _usd(10.0, 50.0, "claude-fable-estimate")
        if "opus" in model_key:
            return _usd(5.0, 25.0, "claude-opus-estimate")
        if "sonnet-5" in model_key:
            return _usd(2.0, 10.0, "claude-sonnet-5-intro-estimate")
        return _usd(3.0, 15.0, "claude-sonnet-estimate")

    return UNKNOWN_FALLBACK


def cost_cny(input_tokens: int, output_tokens: int, provider: str | None, model: str | None) -> float:
    pricing = pricing_for(provider, model)
    return round(
        max(0, int(input_tokens or 0)) / 1_000_000 * pricing.input_cny_per_m
        + max(0, int(output_tokens or 0)) / 1_000_000 * pricing.output_cny_per_m,
        6,
    )


def pricing_dict(provider: str | None, model: str | None) -> dict[str, float | str]:
    pricing = pricing_for(provider, model)
    return {
        "input_cny_per_m": pricing.input_cny_per_m,
        "output_cny_per_m": pricing.output_cny_per_m,
        "note": pricing.note,
    }


def _usd(input_usd_per_m: float, output_usd_per_m: float, note: str) -> ModelPricing:
    return ModelPricing(
        round(input_usd_per_m * USD_TO_CNY, 6),
        round(output_usd_per_m * USD_TO_CNY, 6),
        note,
    )
