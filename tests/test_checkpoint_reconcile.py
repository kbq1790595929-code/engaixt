"""续翻前必须先重新提取，再用检查点译文按原文对齐。"""
from __future__ import annotations

from types import SimpleNamespace

from core.pipeline_checkpoint_stage import _reconcile_checkpoint_with_extraction
from engines.base import TextItem


def _text_item(file: str, key: str, original: str) -> TextItem:
    return TextItem(file=file, key=key, original=original, context="message", line=1)


def test_reuses_checkpoint_translations_by_original_text(tmp_path, monkeypatch):
    checkpoint_items = [
        SimpleNamespace(file="a.ks", original="あ", translated="甲"),
        SimpleNamespace(file="b.ks", original="い", translated=""),
    ]
    extracted = [
        _text_item("a.ks", "line_1_span_0", "あ"),
        _text_item("b.ks", "line_2_span_0", "い"),
        _text_item("c.ks", "line_3_span_0", "う"),
    ]
    monkeypatch.setattr(
        "core.pipeline_stage_runtime.run_extract_stage",
        lambda *args, **kwargs: (None, extracted, len(extracted)),
    )
    diagnostics = SimpleNamespace(set=lambda *args, **kwargs: None)
    pipeline = SimpleNamespace(diagnostics=diagnostics)

    result = _reconcile_checkpoint_with_extraction(pipeline, tmp_path, None, checkpoint_items)

    assert result is extracted
    assert extracted[0].translated == "甲"
    assert extracted[1].translated == ""
    assert extracted[2].translated == ""


def test_falls_back_to_checkpoint_when_extraction_is_empty(tmp_path, monkeypatch):
    checkpoint_items = [SimpleNamespace(file="a.ks", original="あ", translated="甲")]
    monkeypatch.setattr(
        "core.pipeline_stage_runtime.run_extract_stage",
        lambda *args, **kwargs: (None, [], 0),
    )
    diagnostics = SimpleNamespace(set=lambda *args, **kwargs: None)
    pipeline = SimpleNamespace(diagnostics=diagnostics)

    result = _reconcile_checkpoint_with_extraction(pipeline, tmp_path, None, checkpoint_items)

    assert result is checkpoint_items
