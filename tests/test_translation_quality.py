"""Translation quality guard regressions."""

from __future__ import annotations

from utils.text_extract import is_acceptable_same_as_source, verify_translation


def test_chinese_translation_with_leftover_kana_is_rejected():
    original = "ちょっと、やさしくね...？やさしくねって言ったわよね...っ！？"
    translated = "喂，轻点儿啊……？不是说了要温柔点嘛……っ！？"

    safe, warnings = verify_translation(original, translated)

    assert safe == original
    assert any("假名残留" in warning for warning in warnings)


def test_plain_chinese_translation_is_kept():
    original = "タイトルに戻る？"
    translated = "返回标题？"

    safe, warnings = verify_translation(original, translated)

    assert safe == translated
    assert not warnings


def test_bgi_ruby_visible_source_with_leftover_reading_is_rejected():
    original = "『安芸 かのこ』小学校のときからずっと同じクラス"
    translated = "『安芸 かのこ』从小学开始就一直同班"

    safe, warnings = verify_translation(original, translated)

    assert safe == original
    assert any("假名残留" in warning for warning in warnings)


def test_short_quoted_kana_terms_in_language_explanations_are_allowed():
    original = "つまり『は』だ。"
    translated = "也就是说，是『は』。"

    safe, warnings = verify_translation(original, translated)

    assert safe == translated
    assert not warnings


def test_cooking_mnemonic_kana_terms_are_allowed():
    original = "「日本では料理の基本を『さしすせそ』で言いますよね？」"
    translated = "「在日本，常说料理的基本是『さしすせそ』，对吧？」"

    safe, warnings = verify_translation(original, translated)

    assert safe == translated
    assert not warnings


def test_leftover_kana_without_language_context_is_still_rejected():
    original = "これは、はっきり言うべきだった。"
    translated = "这件事，はっきり说出来才对。"

    safe, warnings = verify_translation(original, translated)

    assert safe == original
    assert any("假名残留" in warning for warning in warnings)


def test_short_cjk_same_as_source_can_be_effective_translation():
    assert is_acceptable_same_as_source("雪。", "雪。")
    assert is_acceptable_same_as_source("警告。", "警告。")
    assert is_acceptable_same_as_source("条件？", "条件？")
    assert not is_acceptable_same_as_source("つまり『は』だ。", "つまり『は』だ。")
    assert not is_acceptable_same_as_source("テスト", "テスト")


def test_dialogue_quote_style_follows_source_outer_quotes():
    safe, warnings = verify_translation("「おーい、朝だよー。起きろー」", "“喂，早上了哦。起床啦——”")

    assert safe == "「喂，早上了哦。起床啦——」"
    assert any("外层台词引号" in warning for warning in warnings)


def test_extra_outer_quotes_are_removed_for_unquoted_source():
    safe, warnings = verify_translation("眩しさから逃れようとした。", "“为了躲避刺眼的光线。”")

    assert safe == "为了躲避刺眼的光线。"
    assert any("额外引号" in warning for warning in warnings)


def test_double_corner_quote_style_is_preserved():
    safe, warnings = verify_translation("『巻き込んだのは俺の方だからな』", "「是我把你卷进来的」")

    assert safe == "『是我把你卷进来的』"
    assert any("外层台词引号" in warning for warning in warnings)
