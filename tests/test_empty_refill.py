"""空译补翻：只针对 message/choice 的空译文，循环到收敛。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import core.pipeline as pipeline_mod
from core.pipeline_translate_stage import refill_empty_translations
from engines.base import TextItem


class _FakeTranslator:
    name = "deepseek"

    def __init__(self, fill: bool = True) -> None:
        self.fill = fill
        self.calls = 0
        self.sizes: list[int] = []

    async def translate_batch(self, items, source_lang, target_lang, **kwargs):
        self.calls += 1
        self.sizes.append(len(items))
        if self.fill:
            for item in items:
                item.translated = "译文"
        return list(items)


def _item(context: str, original: str, translated: str = "") -> TextItem:
    item = TextItem(file="a.ks", key=f"k-{original}", original=original, context=context, line=1)
    item.translated = translated
    return item


def _pipeline() -> SimpleNamespace:
    diagnostics = SimpleNamespace(set=lambda *args, **kwargs: None)
    return SimpleNamespace(diagnostics=diagnostics)


def test_refill_only_empty_dialogue_items(tmp_path, monkeypatch):
    translator = _FakeTranslator()
    monkeypatch.setattr(pipeline_mod, "_get_translator", lambda name: translator)
    items = [
        _item("message", "「あ」"),
        _item("message", "「い」"),
        _item("message", "「う」", "已有译文"),
        _item("name", "杏珠"),
    ]

    result = asyncio.run(
        refill_empty_translations(_pipeline(), items, tmp_path / "ck.json", "ja", "zh-CN")
    )

    assert result is items
    assert items[0].translated == "译文"
    assert items[1].translated == "译文"
    assert items[2].translated == "已有译文"
    assert items[3].translated == ""
    assert translator.calls >= 1


def test_refill_stops_when_no_progress(tmp_path, monkeypatch):
    translator = _FakeTranslator(fill=False)
    monkeypatch.setattr(pipeline_mod, "_get_translator", lambda name: translator)
    captured: dict = {}

    def _set(key, value):
        captured[key] = value

    pipeline = SimpleNamespace(diagnostics=SimpleNamespace(set=_set))
    items = [_item("message", "「あ」"), _item("message", "「い」")]

    asyncio.run(refill_empty_translations(pipeline, items, tmp_path / "ck.json", "ja", "zh-CN"))

    assert translator.calls == 1
    assert captured["empty_refill"]["remaining"] == 2
    assert captured["empty_refill"]["filled"] == 0
