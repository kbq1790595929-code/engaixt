"""配置 schema 测试：类型校验、敏感字段权威定义、可导出字段清单。"""
from __future__ import annotations

import json
import sys
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config as config_mod
from config import (
    DIAGNOSTIC_SNAPSHOT_FIELDS,
    Config,
    is_sensitive_field,
)


def _load_from(tmp_path: Path, data: dict, monkeypatch) -> Config:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
    return Config.load()


def test_load_coerces_numeric_strings(tmp_path, monkeypatch):
    cfg = _load_from(tmp_path, {
        "max_batch_size": "25",
        "translation_cache_max_size_gb": "2.5",
        "hy_mt2_context_size": "4096",
    }, monkeypatch)
    assert cfg.max_batch_size == 25
    assert cfg.translation_cache_max_size_gb == 2.5
    assert cfg.hy_mt2_context_size == 4096


def test_load_bad_types_fall_back_to_defaults(tmp_path, monkeypatch):
    defaults = Config()
    cfg = _load_from(
        tmp_path,
        {
            "max_concurrency": "很多",           # 垃圾字符串 → 默认
            "auto_launch": "yes",                # 字符串不是 bool → 默认
            "active_translator": 123,            # 数字不是 str → 默认
            "engine_whitelist": "not-a-list",    # 非 list → 默认
            "keep_workspace": 1,                 # int 不是 bool → 默认
        },
        monkeypatch,
    )
    assert cfg.max_concurrency == defaults.max_concurrency
    assert cfg.auto_launch == defaults.auto_launch
    assert cfg.active_translator == defaults.active_translator
    assert cfg.engine_whitelist == defaults.engine_whitelist
    assert cfg.keep_workspace == defaults.keep_workspace


def test_load_ignores_unknown_keys(tmp_path, monkeypatch):
    cfg = _load_from(tmp_path, {"totally_unknown_field": 1, "target_lang": "zh-TW"}, monkeypatch)
    assert cfg.target_lang == "zh-TW"
    assert not hasattr(cfg, "totally_unknown_field")


def test_legacy_24h_tool_interval_migrates(tmp_path, monkeypatch):
    cfg = _load_from(tmp_path, {"tool_update_interval_hours": 24}, monkeypatch)
    assert cfg.tool_update_interval_hours == config_mod.DEFAULT_TOOL_UPDATE_INTERVAL_HOURS


def test_legacy_krkr_false_migrates_once(tmp_path, monkeypatch):
    cfg = _load_from(tmp_path, {"kirikiri_enable_static_patch": False}, monkeypatch)
    assert cfg.kirikiri_enable_static_patch is True
    assert cfg.config_schema_version == config_mod.CONFIG_SCHEMA_VERSION


def test_schema_two_krkr_opt_out_is_preserved_after_schema_bump(tmp_path, monkeypatch):
    cfg = _load_from(
        tmp_path,
        {
            "config_schema_version": 2,
            "kirikiri_enable_static_patch": False,
        },
        monkeypatch,
    )
    assert cfg.kirikiri_enable_static_patch is False
    assert cfg.config_schema_version == config_mod.CONFIG_SCHEMA_VERSION


def test_current_krkr_opt_out_is_preserved(tmp_path, monkeypatch):
    cfg = _load_from(
        tmp_path,
        {
            "config_schema_version": config_mod.CONFIG_SCHEMA_VERSION,
            "kirikiri_enable_static_patch": False,
        },
        monkeypatch,
    )
    assert cfg.kirikiri_enable_static_patch is False


def test_selection_preflight_defaults_to_disabled_and_accepts_saved_value(tmp_path, monkeypatch):
    assert Config().selection_preflight_enabled is False
    cfg = _load_from(tmp_path, {"selection_preflight_enabled": True}, monkeypatch)
    assert cfg.selection_preflight_enabled is True


def test_all_api_key_fields_are_sensitive():
    key_fields = [f.name for f in fields(Config) if f.name.endswith("_api_key")]
    assert key_fields, "配置里应存在 API key 字段"
    for name in key_fields:
        assert is_sensitive_field(name), name


def test_token_usage_metrics_are_not_credentials():
    for name in (
        "estimated_input_tokens",
        "actual_output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "final_translation_tokens",
        "total_tokens",
        "max_tokens",
    ):
        assert not is_sensitive_field(name), name
    for name in ("auth_token", "access_token", "refresh_token", "token", "token_secret"):
        assert is_sensitive_field(name), name


def test_diagnostic_snapshot_fields_exist_and_contain_no_secret():
    field_names = {f.name for f in fields(Config)}
    stale = DIAGNOSTIC_SNAPSHOT_FIELDS - field_names
    assert not stale, f"可导出清单里有已不存在的字段: {stale}"
    leaked = {name for name in DIAGNOSTIC_SNAPSHOT_FIELDS if is_sensitive_field(name)}
    assert not leaked, f"可导出清单混入敏感字段: {leaked}"


def test_public_dict_never_contains_sensitive_fields():
    cfg = Config(openai_api_key="sk-test", deepseek_api_key="sk-test2")
    public = cfg.public_dict()
    for name in public:
        assert not is_sensitive_field(name), name
    assert "openai_api_key" not in public
    assert "sk-test" not in json.dumps(public, ensure_ascii=False)
