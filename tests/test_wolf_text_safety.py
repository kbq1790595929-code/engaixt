from __future__ import annotations

from engines.base import TextItem
from engines.wolf.text_safety import sanitize_wolf_items, strip_wolf_ruby
from utils.text_extract import protect_placeholders, validation_source_for_item


def _item(original: str, translated: str) -> TextItem:
    return TextItem(
        file="Data/BasicData.wolf",
        original=original,
        translated=translated,
        context="WOLF command / Message",
        meta={"wolf_role": "command", "wolf_pointer": "/commands/0/stringArgs/0"},
    )


def test_wolf_controls_are_atomic_placeholders_and_keep_duplicates():
    source = "\\E\\cself[8] and \\cself[8]\\f[\\cself[17]]"

    protected, placeholders = protect_placeholders(source)

    assert placeholders == ["\\E", "\\cself[8]", "\\cself[8]", "\\f[\\cself[17]]"]
    assert protected == "{{PH0}}{{PH1}} and {{PH2}}{{PH3}}"


def test_wolf_authored_newline_is_protected_for_translation():
    protected, placeholders = protect_placeholders("first line\nsecond line")

    assert protected == "first line{{PH0}}second line"
    assert placeholders == ["\n"]


def test_wolf_legacy_translation_restores_leading_control_and_line_layout():
    item = _item("\\Efirst sentence\nsecond sentence", "first translated. second translated.")

    stats = sanitize_wolf_items([item], "zh-CN")

    assert item.translated.startswith("\\E")
    assert item.translated.count("\n") == 1
    assert stats.leading_controls_restored == 1
    assert stats.line_layout_restored == 1


def test_wolf_ruby_uses_visible_base_text_and_drops_broken_legacy_markup():
    item = _item("\\r[base,reading] text", "translated \\r[word]")

    assert strip_wolf_ruby(item.original) == "base text"
    assert validation_source_for_item(item) == "base text"
    stats = sanitize_wolf_items([item], "en")

    assert item.translated == "translated word"
    assert stats.ruby_markup_removed == 1


def test_wolf_rejects_english_cache_output_for_chinese_target():
    item = _item("right hand", "right hand")
    item.original = "\u53f3\u624b"

    stats = sanitize_wolf_items([item], "zh-CN")

    assert item.translated == item.original
    assert item.meta["wolf_validation_reason"] == "non_chinese_output"
    assert stats.non_chinese_rejected == 1


def test_wolf_rejects_residual_kana_for_retry():
    item = _item("\u3044\u3089\u3063\u3057\u3083\u3044\u307e\u305b", "\u6b22\u8fce\u5149\u4e34\u30fc")

    stats = sanitize_wolf_items([item], "zh-CN")

    assert item.translated == "\u6b22\u8fce\u5149\u4e34\u2014"
    assert stats.kana_residuals_cleaned == 1


def test_wolf_normalizes_ecchi_ui_label_to_chinese():
    item = _item("\u3048\u3063\u3061", "H")

    stats = sanitize_wolf_items([item], "zh-CN")

    assert item.translated == "\u8272\u60c5"
    assert stats.kana_residuals_cleaned == 1


def test_wolf_allows_runtime_variable_particle_to_disappear():
    item = _item("\\cself[7]\u304c", "\\cself[7]")

    stats = sanitize_wolf_items([item], "zh-CN")

    assert item.translated == "\\cself[7]"
    assert stats.non_chinese_rejected == 0
