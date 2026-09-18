from __future__ import annotations

from engines.base import TextItem
from translators.polisher import (
    DeepSeekPolisher,
    POLISH_VERSION,
    _is_polish_current,
    _mark_polished,
    _parse_polish_json,
    _validate_polished,
)


def test_polisher_skips_names_choices_and_current_items():
    polisher = DeepSeekPolisher()
    message = TextItem(
        file="a.ks",
        original="原文",
        translated="我进行着这样的思考，并且觉得这之中有些奇怪。",
        context="message",
    )
    name = TextItem(file="a.ks", original="大和", translated="大和", context="name")
    choice = TextItem(file="a.ks", original="はい", translated="好的", context="choice")
    already = TextItem(
        file="a.ks",
        original="原文2",
        translated="这个句子已经审过，并且保持不变。",
        context="message",
    )
    _mark_polished(already)

    stats = type("Stats", (), {
        "skipped_names": 0,
        "skipped_already_polished": 0,
        "budget_limited": 0,
        "unchanged_count": 0,
    })()

    selected = polisher._select_candidates([message, name, choice, already], stats, 1.0)

    assert selected == [message]
    assert stats.skipped_names == 2
    assert stats.skipped_already_polished == 1
    assert _is_polish_current(already)
    assert already.meta["polish"]["version"] == POLISH_VERSION


def test_parse_polish_json_only_accepts_changed_map():
    assert _parse_polish_json('{"p":{"1":"我想，这件事有点奇怪。"}}') == {
        1: "我想，这件事有点奇怪。"
    }


def test_validate_polished_rejects_expansion_and_kana_regression():
    item = TextItem(file="a.ks", original="x", translated="我觉得这件事有点奇怪。")

    assert _validate_polished(item, "我觉得这件事有点奇怪，而且这说明过去发生了很多没有交代的事情。") is None
    assert _validate_polished(item, "我觉得これは有点奇怪。") is None
    assert _validate_polished(item, "我觉得这事有点怪。") == "我觉得这事有点怪。"


def test_validate_polished_preserves_quote_style_and_narration_anchor():
    quoted = TextItem(file="a.ks", original="x", translated="「你还在啊？」")
    quoted_change = TextItem(file="a.ks", original="x", translated="「你还在啊？安全屋的准备出问题了？」")
    narrated = TextItem(file="a.ks", original="x", translated="少女一副不知该如何反应的样子回应道。")

    assert _validate_polished(quoted, "“你还在啊？”") is None
    assert _validate_polished(quoted_change, "“你还在啊？安全屋准备有问题？”") == "「你还在啊？安全屋准备有问题？」"
    assert _validate_polished(quoted, "你还在啊？") is None
    assert _validate_polished(narrated, "少女一副不知该如何反应的样子。") is None
