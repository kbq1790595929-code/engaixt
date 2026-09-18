from __future__ import annotations

import pytest

from core.runtime_translation_retry import translate_with_empty_retry


def test_runtime_translation_retries_one_empty_result() -> None:
    results = iter(["", "译文"])
    retries: list[tuple[str, int]] = []

    translated = translate_with_empty_retry(
        lambda: next(results),
        "原文",
        delay_seconds=0,
        on_retry=lambda reason, attempt: retries.append((reason, attempt)),
    )

    assert translated == "译文"
    assert retries == [("empty", 1)]


def test_runtime_translation_does_not_retry_success() -> None:
    calls = 0

    def translate_once() -> str:
        nonlocal calls
        calls += 1
        return "译文"

    assert translate_with_empty_retry(translate_once, "原文", delay_seconds=0) == "译文"
    assert calls == 1


def test_runtime_translation_retries_transient_error() -> None:
    calls = 0

    def translate_once() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary")
        return "译文"

    assert translate_with_empty_retry(translate_once, "原文", delay_seconds=0) == "译文"
    assert calls == 2


def test_runtime_translation_raises_last_error_after_retry() -> None:
    def fail() -> str:
        raise RuntimeError("temporary")

    with pytest.raises(RuntimeError, match="temporary"):
        translate_with_empty_retry(fail, "原文", delay_seconds=0)
