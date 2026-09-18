"""Godot Dialogic choice context regressions."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engines.godot_pck import patch_dtl_content


def test_skip_confirmation_choices_are_made_explicit():
    content = (
        "導入シーンをスキップしますか？\\\n"
        "（所要時間５～１０分）\n"
        "- はい\n"
        "\tjump skip\n"
        "- いいえ\n"
        "label skip (skip)\n"
    )

    patched, count = patch_dtl_content(content, {
        "導入シーンをスキップしますか？（所要時間５～１０分）": "要跳过开场剧情吗？（约 5 到 10 分钟）",
        "- はい": "好的",
        "- いいえ": "不",
    })

    assert count == 3
    assert "- 跳过剧情" in patched
    assert "- 观看剧情" in patched
    assert "- 好的" not in patched
    assert "- 不" not in patched
