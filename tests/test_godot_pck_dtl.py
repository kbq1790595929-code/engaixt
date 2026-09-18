"""Godot Dialogic DTL extraction and patching regressions."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engines.godot_pck import extract_dtl_text, patch_dtl_content


def test_visible_bbcode_lines_are_extracted_without_command_noise():
    content = (
        '[background arg="res://Assets/bg.png" fade="0.0"]\n'
        '[wait time="1.0"]\n'
        "[i]The Bar opened just a few hours ago.[/i]\n"
        "[i]It is [b]the night of All Hallows Eve[/b], after all.[/i]\n"
        "[b]BUT NOT GOOD ENOUGH!!![/b]\n"
        "[speed=4] WHAT ARE YOU DOING?\n"
    )

    items = extract_dtl_text(content, "Timelines/intro.dtl")
    originals = [item.original for item in items]

    assert "[i]The Bar opened just a few hours ago.[/i]" in originals
    assert "[i]It is [b]the night of All Hallows Eve[/b], after all.[/i]" in originals
    assert "[b]BUT NOT GOOD ENOUGH!!![/b]" in originals
    assert "[speed=4] WHAT ARE YOU DOING?" in originals
    assert not any("background" in item.original for item in items)
    assert not any("wait time" in item.original for item in items)

    patched, count = patch_dtl_content(content, {
        "[i]The Bar opened just a few hours ago.[/i]": "[i]酒吧几小时前刚开门。[/i]",
        "[i]It is [b]the night of All Hallows Eve[/b], after all.[/i]": "[i]毕竟今晚是[b]万圣节前夜[/b]。[/i]",
        "[b]BUT NOT GOOD ENOUGH!!![/b]": "[b]但还不够！！！[/b]",
        "[speed=4] WHAT ARE YOU DOING?": "[speed=4] 你在干什么？",
    })

    assert count == 4
    assert "[i]酒吧几小时前刚开门。[/i]" in patched
    assert "[i]毕竟今晚是[b]万圣节前夜[/b]。[/i]" in patched
    assert "[b]但还不够！！！[/b]" in patched
    assert "[speed=4] 你在干什么？" in patched
    assert "The Bar opened" not in patched


def test_text_input_default_is_extracted_and_patched_with_escaping():
    content = (
        '[text_input variable="player_name" default="銇仾\\\"銇?"]\n'
        "銇撱倱銇仭銇?\n"
    )

    items = extract_dtl_text(content, "TimeLine/Input.dtl")

    assert any(item.key == "input_0" and item.original == '銇仾"銇?' for item in items)

    patched, count = patch_dtl_content(content, {'銇仾"銇?': '名字"默认'})

    assert count == 1
    assert 'default="名字\\"默认"' in patched
    assert 'default="銇仾' not in patched


def test_standalone_narration_continuation_is_extracted_and_patched():
    content = (
        "[wait time=\"1.0\"]\n"
        "導入シーンをスキップしますか？\\\n"
        "（所要時間５～１０分）\n"
        "- はい\n"
        "\tjump skip\n"
    )

    items = extract_dtl_text(content, "TimeLine/Prologue.dtl")
    originals = [item.original for item in items]

    original = "導入シーンをスキップしますか？（所要時間５～１０分）"
    assert original in originals

    patched, count = patch_dtl_content(content, {original: "要跳过导入场景吗？（约 5 到 10 分钟）"})

    assert count == 1
    assert "要跳过导入场景吗？（约 5 到 10 分钟）" in patched
    assert "導入シーンをスキップしますか" not in patched
    assert "（所要時間５～１０分）" not in patched


def test_standalone_story_continuation_is_extracted_as_one_block():
    content = (
        "MC: ...室長？\n"
        "――見たくもない顔だった。\\\n"
        "一応僕も形式上まだ所属していることになっている「魔力研究室」の室長。\n"
        "顔を合わせたことはほとんど無いが...\\\n"
        "僕のような田舎者を嫌う派閥筆頭の人間だった。\n"
    )

    items = extract_dtl_text(content, "TimeLine/01_Main/01/01_Prologue_A.dtl")
    originals = [item.original for item in items]

    first = "――見たくもない顔だった。一応僕も形式上まだ所属していることになっている「魔力研究室」の室長。"
    second = "顔を合わせたことはほとんど無いが...僕のような田舎者を嫌う派閥筆頭の人間だった。"
    assert first in originals
    assert second in originals

    patched, count = patch_dtl_content(content, {
        first: "那是一张我一点也不想见到的脸。名义上，他还是我所属的魔力研究室室长。",
        second: "虽然我几乎没和他照过面，但他正是厌恶乡下人的派阀头目。",
    })

    assert count == 2
    assert "――見たくもない顔だった" not in patched
    assert "顔を合わせたことはほとんど無い" not in patched
    assert "那是一张我一点也不想见到的脸" in patched
    assert "虽然我几乎没和他照过面" in patched
