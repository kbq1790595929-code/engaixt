"""Legacy grouped translation entry point for RPG Maker runtime scans.

The shared pipeline owns normal translation. This module remains for callers
that still import ``translate_scanned_items`` directly.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import traceback
from pathlib import Path

from config import get_config
from engines.base import TextItem
from translators.factory import create_translator
from utils.logger import info, warning


async def _translate_items_grouped(
    items: list[TextItem],
    source_lang: str = "ja",
    target_lang: str = "zh-CN",
    on_progress: object = None,
) -> list[TextItem]:
    """Translate event groups concurrently while preserving the old API."""
    translator = (
        create_translator(getattr(get_config(), "active_translator", "deepseek"))
        or create_translator("deepseek")
    )

    groups: dict[str, list[TextItem]] = {}
    singles: list[TextItem] = []
    for item in items:
        group_key = (item.meta or {}).get("group_key", "")
        if group_key:
            groups.setdefault(group_key, []).append(item)
        else:
            singles.append(item)

    real_groups: dict[str, list[TextItem]] = {}
    for group_key, group_items in groups.items():
        if len(group_items) <= 1:
            singles.extend(group_items)
        else:
            real_groups[group_key] = group_items

    total_items = sum(len(group) for group in real_groups.values()) + len(singles)
    total_groups = len(real_groups) + (1 if singles else 0)
    info(
        f"批量翻译: {len(real_groups)} 个事件组 + "
        f"{len(singles)} 条单项 = {total_items} 条"
    )
    group_counter = itertools.count(1)

    def report_progress(step: str = "") -> None:
        if not on_progress or not callable(on_progress):
            return
        translated = sum(
            1 for item in items
            if item.translated and item.translated != item.original
        )
        percent = 23 + int(translated / max(len(items), 1) * 5)
        message = f"翻译中 ({translated}/{len(items)})"
        if step:
            message += f" - {step}"
        try:
            on_progress(message, percent)
        except Exception:
            pass

    async def translate_group(group_key: str, group_items: list[TextItem]):
        index = next(group_counter)
        report_progress(f"第 {index}/{total_groups} 组 ({len(group_items)} 条)")
        try:
            result = await translator.translate_group(group_items, source_lang, target_lang)
        except Exception as exc:
            warning(f"事件组 {group_key} 翻译失败，降级批处理: {exc}")
            result = await translator.translate_batch(group_items, source_lang, target_lang)
        report_progress()
        return result

    async def translate_singles():
        index = next(group_counter)
        report_progress(f"第 {index}/{total_groups} 组 ({len(singles)} 条)")
        result = await translator.translate_batch(singles, source_lang, target_lang)
        report_progress()
        return result

    tasks = [
        translate_group(group_key, group_items)
        for group_key, group_items in real_groups.items()
    ]
    if singles:
        tasks.append(translate_singles())

    report_progress()
    results = await asyncio.gather(*tasks)
    translated_items: list[TextItem] = []
    for result in results:
        if isinstance(result, list):
            translated_items.extend(result)
    return translated_items


def translate_scanned_items(
    game_path: Path,
    items: list[TextItem],
    source_lang: str = "ja",
    target_lang: str = "zh-CN",
    on_progress: object = None,
) -> list[TextItem]:
    """Synchronous compatibility wrapper for the legacy grouped translator."""
    del game_path
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _translate_items_grouped(items, source_lang, target_lang, on_progress)
        )

    result: list[list[TextItem]] = []
    errors: list[BaseException] = []

    def run_in_thread() -> None:
        try:
            result.append(asyncio.run(
                _translate_items_grouped(items, source_lang, target_lang, on_progress)
            ))
        except BaseException as exc:
            errors.append(exc)
            traceback.print_exc()

    thread = threading.Thread(target=run_in_thread)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0]


__all__ = ["translate_scanned_items"]
