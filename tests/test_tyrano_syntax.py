from __future__ import annotations

import pytest

from engines.tyrano.script import (
    apply_span_translations,
    extract_spans,
    is_japanese_visible_script,
)


def test_line_commands_and_unquoted_attributes_are_extracted_without_targets():
    script = (
        '@glink text="始める" target=*start storage="data/画像.ks"\n'
        '[ glink text=戻ります target=*return]\n'
        '[ptext text="こんにちは]みんな" x=100]\n'
        '[glink text="&tf.message" target=*dynamic]\n'
        '@glink text=%choice target=*macro\n'
    )

    spans = extract_spans(script, require_kana=True)

    assert [(span.role, span.text) for span in spans] == [
        ("attribute:glink.text", "始める"),
        ("attribute:glink.text", "戻ります"),
        ("attribute:ptext.text", "こんにちは]みんな"),
    ]
    assert all(script[span.start:span.end] == span.text for span in spans)


def test_script_and_comment_blocks_are_never_extracted_as_dialogue():
    script = (
        '@iscript\n'
        'const choices = [\n'
        '  "こんにちは",\n'
        '];\n'
        'const marker = "[endscript]";\n'
        '"これはコードです"\n'
        '@endscript\n'
        '/*\n'
        'ここはコメントです\n'
        '*/\n'
        '[iscript]f.values[0] = "秘密です";[ endscript]\n'
        '通常の台詞です[p]\n'
    )

    spans = extract_spans(script, require_kana=True)

    assert [span.text for span in spans] == ["通常の台詞です"]


def test_literal_display_name_defaults_inside_iscript_are_extracted():
    script = (
        "[iscript]\n"
        "f['Oni_Name3']='お兄ちゃん';\n"
        "f['Imo_Name']='さくら';\n"
        "f['Image_Name']='背景.png';\n"
        "[endscript]\n"
        "#&f.Imo_Name\n"
        "こんにちは[p]\n"
    )

    spans = extract_spans(script, require_kana=False)

    assert [(span.role, span.text) for span in spans] == [
        ("variable:Oni_Name3", "お兄ちゃん"),
        ("variable:Imo_Name", "さくら"),
        ("text", "こんにちは"),
    ]
    patched = apply_span_translations(script, [
        (spans[0].start, spans[0].end, spans[0].text, "哥哥"),
        (spans[1].start, spans[1].end, spans[1].text, "樱"),
    ])
    assert "f['Oni_Name3']='哥哥';" in patched
    assert "f['Imo_Name']='樱';" in patched
    assert "#&f.Imo_Name" in patched


def test_literal_fragments_for_japanese_variable_names_are_extracted():
    script = (
        "[iscript]\n"
        "f.頭をなでる表示 = Math.max(0, f.count);\n"
        "f.頭をなでる = \"頭をなでる　　\" + f.頭をなでる表示 + \"％\";\n"
        "f.未使用 = \"翻訳しない\";\n"
        "f.画像 = \"背景/部屋.png\";\n"
        "[endscript]\n"
        "[glink text=\"&f.頭をなでる\"]\n"
    )

    spans = extract_spans(script, require_kana=False)

    assert [(span.role, span.text) for span in spans] == [
        ("variable:頭をなでる", "頭をなでる　　"),
    ]
    patched = apply_span_translations(script, [
        (spans[0].start, spans[0].end, spans[0].text, "摸头      "),
    ])
    assert 'f.頭をなでる = "摸头      " + f.頭をなでる表示' in patched
    assert 'f.未使用 = "翻訳しない";' in patched


def test_dynamic_speaker_and_embedded_text_reference_variable_assignments():
    script = (
        "[iscript]\n"
        "f.sakura = \"さくら\";\n"
        "f.action = \"手をつなぐ\";\n"
        "[endscript]\n"
        "#&f.sakura\n"
        "&f.action[p]\n"
        "[emb exp=\"f.action\"]\n"
        "[glink text=\"f.手をつなぐ\"]\n"
    )

    spans = extract_spans(script, require_kana=False)

    assert [(span.role, span.text) for span in spans] == [
        ("variable:sakura", "さくら"),
        ("variable:action", "手をつなぐ"),
    ]


def test_visible_item_data_fields_are_extracted_from_item_arrays():
    script = (
        "[iscript]\n"
        "f.Item = [\n"
        '  {"name": "媚薬", "memo": "とてもえっちな気分になる"},\n'
        '  {"name": "背景.png", "memo": "説明"},\n'
        "];\n"
        "f.my_Item = [\n"
        '  {"name": "ブランド服"},\n'
        "];\n"
        "[endscript]\n"
    )

    spans = extract_spans(script, require_kana=True)

    assert [(span.role, span.text) for span in spans] == [
        ("variable:Item.name", "媚薬"),
        ("variable:Item.memo", "とてもえっちな気分になる"),
        ("variable:my_Item.name", "ブランド服"),
    ]
    patched = apply_span_translations(script, [
        (spans[0].start, spans[0].end, spans[0].text, "媚药"),
        (spans[1].start, spans[1].end, spans[1].text, "让人非常兴奋"),
        (spans[2].start, spans[2].end, spans[2].text, "品牌服装"),
    ])
    assert '{"name": "媚药", "memo": "让人非常兴奋"}' in patched
    assert '"name": "背景.png"' in patched
    assert '"name": "品牌服装"' in patched


def test_japanese_only_kanji_item_name_is_extracted_from_display_array():
    script = (
        "[iscript]\n"
        "f.my_Item = [\n"
        '  {"name": "高級化粧品"},\n'
        "];\n"
        "[endscript]\n"
    )

    spans = extract_spans(script, require_kana=True)

    assert [(span.role, span.text) for span in spans] == [
        ("variable:my_Item.name", "高級化粧品"),
    ]


def test_reextraction_skips_chinese_name_defaults_but_keeps_japanese_names():
    script = (
        "[iscript]\n"
        'f.Oni_Name3 = "哥哥";\n'
        'f.Imo_Name = "さくら";\n'
        "[endscript]\n"
        '[chara_new name="sakura" jname="樱01"]\n'
        '[chara_new name="shizu" jname="桜01"]\n'
    )

    spans = extract_spans(script, require_kana=True)

    assert [(span.role, span.text) for span in spans] == [
        ("variable:Imo_Name", "さくら"),
        ("attribute:chara_new.jname", "桜01"),
    ]


def test_unclosed_tag_is_not_extracted_as_dialogue():
    script = '[ptext text="未完了の文字列" x=0\n普通の台詞です[p]\n'

    assert [span.text for span in extract_spans(script, require_kana=True)] == ["普通の台詞です"]


def test_character_display_name_is_translated_but_reference_ids_are_preserved():
    script = (
        '[chara_new name="クロエ" jname="クロエです"]\n'
        '#クロエ:smile\n'
        'こんにちは[p]\n'
    )
    spans = extract_spans(script, require_kana=True)

    assert [(span.role, span.text) for span in spans] == [
        ("attribute:chara_new.jname", "クロエです"),
        ("speaker", "クロエ"),
        ("text", "こんにちは"),
    ]
    assert spans[2].speaker == "クロエ"
    patched = apply_span_translations(script, [
        (spans[0].start, spans[0].end, spans[0].text, "克洛艾"),
        (spans[1].start, spans[1].end, spans[1].text, "克洛艾"),
        (spans[2].start, spans[2].end, spans[2].text, "你好"),
    ])
    assert '[chara_new name="クロエ" jname="克洛艾"]' in patched
    assert '#克洛艾:smile\n你好[p]' in patched


def test_speaker_name_keeps_face_suffix_and_skips_dynamic_or_color_values():
    script = "#&f.name:smile\n#FFF\n#FFFFFF\n#真白:通常\nこんにちは[p]\n"

    spans = extract_spans(script, require_kana=False)

    assert [(span.role, span.text) for span in spans] == [
        ("speaker", "真白"),
        ("text", "こんにちは"),
    ]
    name = spans[0]
    patched = apply_span_translations(script, [
        (name.start, name.end, name.text, "真白"),
    ])
    assert "#真白:通常" in patched


def test_pure_kanji_speaker_name_is_extracted_for_japanese_scripts():
    script = "#静\n日本語の台詞です[p]\n"

    spans = extract_spans(script, require_kana=False)

    assert [(span.role, span.text) for span in spans] == [
        ("speaker", "静"),
        ("text", "日本語の台詞です"),
    ]


def test_speaker_names_do_not_change_script_language_classification():
    script = "#静\n#真白\n#雪\n#花\n日本語の台詞です[p]\n"

    assert is_japanese_visible_script(script)


def test_speaker_translation_cannot_change_face_suffix():
    script = "#クロエ:smile\nこんにちは[p]\n"
    span = extract_spans(script, require_kana=True)[0]

    with pytest.raises(ValueError, match="face suffix"):
        apply_span_translations(script, [(span.start, span.end, span.text, "克洛艾:泪目")])


@pytest.mark.parametrize("translation", ['选择"继续', '选择]继续', '选择[继续', '选择\n@jump', '&tf.value'])
def test_unsafe_quoted_attribute_translations_fail_closed(translation):
    script = '[glink text="始める" target=*start]\n'
    span = extract_spans(script, require_kana=True)[0]

    with pytest.raises(ValueError, match="Tyrano"):
        apply_span_translations(script, [(span.start, span.end, span.text, translation)])


def test_unquoted_attribute_translation_with_spaces_is_safely_quoted():
    script = '@glink text=始める target=*start\n'
    span = extract_spans(script, require_kana=True)[0]

    patched = apply_span_translations(script, [(span.start, span.end, span.text, "Start game")])

    assert patched == '@glink text=`Start game` target=*start\n'


@pytest.mark.parametrize("translation", ['@jump target=*end', '#キャラ', '*label', ';comment'])
def test_dialogue_translation_cannot_turn_into_a_command(translation):
    script = '普通の台詞です[p]\n'
    span = extract_spans(script, require_kana=True)[0]

    with pytest.raises(ValueError, match="scenario command"):
        apply_span_translations(script, [(span.start, span.end, span.text, translation)])
