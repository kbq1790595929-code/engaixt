from __future__ import annotations

from importlib import import_module
from typing import Type

from translators.base import TranslatorBase


TRANSLATOR_CLASS_PATHS: dict[str, str] = {
    "deepseek": "translators.deepseek:DeepSeekTranslator",
    "qwen": "translators.domestic:QwenTranslator",
    "zhipu": "translators.domestic:ZhipuTranslator",
    "moonshot": "translators.domestic:MoonshotTranslator",
    "doubao": "translators.domestic:DoubaoTranslator",
    "openai": "translators.openai:OpenAITranslator",
    "anthropic": "translators.anthropic:AnthropicTranslator",
    "hy_mt2": "translators.hy_mt2:HyMt2Translator",
}


def translator_choices() -> list[str]:
    return list(TRANSLATOR_CLASS_PATHS)


def get_translator_class(name: str | None) -> Type[TranslatorBase] | None:
    key = str(name or "").strip().lower()
    path = TRANSLATOR_CLASS_PATHS.get(key)
    if not path:
        return None
    module_name, class_name = path.split(":", 1)
    module = import_module(module_name)
    return getattr(module, class_name)


def create_translator(name: str | None) -> TranslatorBase | None:
    cls = get_translator_class(name)
    return cls() if cls else None


def translator_model(name: str | None, config=None) -> str:
    cls = get_translator_class(name)
    if not cls:
        return "unknown"
    instance = cls()
    if hasattr(instance, "_model"):
        return str(instance._model(config) or "unknown")
    field = str(getattr(cls, "MODEL_CONFIG_FIELD", "") or "")
    if field and config is not None:
        value = str(getattr(config, field, "") or "").strip()
        if value:
            return value
    return str(getattr(cls, "MODEL", "unknown") or "unknown")


def translator_prompt_version(name: str | None) -> str:
    cls = get_translator_class(name)
    if not cls:
        return "legacy_v1"
    return str(getattr(cls, "PROMPT_VERSION", "legacy_v1") or "legacy_v1")


def translator_api_key(name: str | None, config) -> str:
    cls = get_translator_class(name)
    if not cls:
        return ""
    instance = cls()
    if hasattr(instance, "_api_key"):
        return str(instance._api_key(config) or "")
    if str(name or "").lower() == "openai":
        return str(getattr(config, "openai_api_key", "") or "")
    if str(name or "").lower() == "anthropic":
        return str(getattr(config, "anthropic_api_key", "") or "")
    return ""


def translator_uses_trial_quota(name: str | None) -> bool:
    cls = get_translator_class(name)
    if not cls:
        return True
    return bool(getattr(cls, "USES_TRIAL_QUOTA", True))
