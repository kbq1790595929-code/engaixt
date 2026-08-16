from __future__ import annotations

import asyncio
import threading

from engines.base import TextItem
from translators.factory import create_translator


_instances: dict[str, object] = {}
_instance_lock = threading.Lock()
_provider_locks: dict[str, threading.Lock] = {}


def translate_with_local_provider(
    text: str,
    source_lang: str,
    target_lang: str,
    provider: str,
) -> str | None:
    """Translate one runtime-captured line through a registered local provider."""
    translator, call_lock = _provider(provider)
    if translator is None:
        return None

    item = TextItem(
        file="<xunity-runtime>",
        key="runtime",
        original=text,
        context="message",
        meta={"kind": "message"},
    )
    with call_lock:
        result = asyncio.run(
            translator.translate_batch([item], source_lang, target_lang)
        )
    if not result:
        return None
    translated = str(result[0].translated or "")
    return translated if translated.strip() else None


def prewarm_local_provider(provider: str) -> None:
    """Load a local provider in the background before the first game line arrives."""
    if provider != "hy_mt2":
        return
    from translators.hy_mt2_runtime import get_runtime

    get_runtime().ensure_started()


def _provider(provider: str) -> tuple[object | None, threading.Lock]:
    key = str(provider or "").strip().lower()
    with _instance_lock:
        translator = _instances.get(key)
        if translator is None:
            translator = create_translator(key)
            if translator is not None:
                _instances[key] = translator
        call_lock = _provider_locks.setdefault(key, threading.Lock())
    return translator, call_lock


def _reset_for_tests() -> None:
    with _instance_lock:
        _instances.clear()
        _provider_locks.clear()
