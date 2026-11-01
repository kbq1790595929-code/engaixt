"""用户自定义模型价格优先于内置估算表。"""
from __future__ import annotations

from types import SimpleNamespace

import config as config_module
from config import _INVALID, _coerce_config_value
from translators.pricing import pricing_for


def _use_custom_pricing(monkeypatch, table: dict) -> None:
    config = SimpleNamespace(custom_pricing=table)
    monkeypatch.setattr(config_module, "get_config", lambda: config)


def test_custom_pricing_overrides_builtin(monkeypatch):
    _use_custom_pricing(monkeypatch, {
        "deepseek": {
            "deepseek-chat": {"input_cny_per_m": 0.7, "output_cny_per_m": 2.8},
        },
    })

    price = pricing_for("deepseek", "deepseek-chat")

    assert price.note == "user-custom-pricing"
    assert price.input_cny_per_m == 0.7
    assert price.output_cny_per_m == 2.8


def test_custom_pricing_supports_provider_wildcard(monkeypatch):
    _use_custom_pricing(monkeypatch, {
        "qwen": {"*": {"input_cny_per_m": 1.0, "output_cny_per_m": 3.0}},
    })

    price = pricing_for("qwen", "any-model")

    assert price.note == "user-custom-pricing"
    assert price.output_cny_per_m == 3.0


def test_invalid_custom_pricing_falls_back_to_builtin(monkeypatch):
    _use_custom_pricing(monkeypatch, {
        "deepseek": {"deepseek-chat": {"input_cny_per_m": "not-a-number"}},
    })

    price = pricing_for("deepseek", "deepseek-chat")

    assert price.note != "user-custom-pricing"


def test_config_accepts_custom_pricing_dict():
    assert _coerce_config_value({"deepseek": {}}, {}) == {"deepseek": {}}
    assert _coerce_config_value("bad", {}) is _INVALID
