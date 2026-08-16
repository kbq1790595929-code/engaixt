from __future__ import annotations

import time
from collections.abc import Callable


def translate_with_empty_retry(
    translate_once: Callable[[], str],
    source_text: str,
    *,
    max_attempts: int = 2,
    delay_seconds: float = 0.15,
    on_retry: Callable[[str, int], None] | None = None,
) -> str:
    attempts = max(1, int(max_attempts))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            translated = str(translate_once() or "").strip()
            if translated and translated != source_text:
                return translated
            reason = "empty"
        except Exception as exc:
            last_error = exc
            reason = type(exc).__name__
        if attempt >= attempts:
            break
        if on_retry:
            on_retry(reason, attempt)
        if delay_seconds > 0:
            time.sleep(delay_seconds)
    if last_error is not None:
        raise last_error
    return ""
