"""Small translation dispatch helpers that keep local-only callbacks isolated."""
from __future__ import annotations


async def translate_batch_with_callbacks(
    translator,
    items,
    source_lang: str,
    target_lang: str,
    *,
    on_progress=None,
    on_speed=None,
):
    kwargs = {"on_progress": on_progress}
    if getattr(translator, "name", "") == "hy_mt2" and on_speed is not None:
        kwargs["on_speed"] = on_speed
    return await translator.translate_batch(items, source_lang, target_lang, **kwargs)
