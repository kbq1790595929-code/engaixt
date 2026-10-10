from __future__ import annotations

from utils.text_extract import verify_translation


def test_model_explanation_output_is_rejected():
    original = "\u9ebb\u8863"
    translated = "\u9ebb\u8863\uff08\u4eba\u540d\uff0c\u4fdd\u7559\u539f\u6837\u6216\u8bd1\u4f5c\u201cMai\u201d\uff09"

    safe, warnings = verify_translation(original, translated)

    assert safe == original
    assert warnings


def test_parenthetical_encyclopedia_note_on_short_name_is_rejected():
    original = "\u5927\u548c"
    translated = "\u5927\u548c\uff08\u65e5\u672c\u53e4\u56fd\u540d/\u6218\u8230\u540d\uff09"

    safe, warnings = verify_translation(original, translated)

    assert safe == original
    assert warnings


def test_model_refusal_output_is_rejected():
    original = "\u7537"
    translated = (
        "\u60a8\u597d\uff0c\u60a8\u53ea\u63d0\u4f9b\u4e86\u201c\u7537\u201d\u4e00\u4e2a\u5b57\uff0c"
        "\u6ca1\u6709\u63d0\u4f9b\u5b8c\u6574\u7684\u65e5\u8bed\u6e38\u620f\u6587\u672c\u3002"
        "\u8bf7\u63d0\u4f9b\u9700\u8981\u7ffb\u8bd1\u7684\u5177\u4f53\u65e5\u8bed\u5185\u5bb9\u3002"
    )

    safe, warnings = verify_translation(original, translated)

    assert safe == original
    assert warnings


def test_short_system_token_refusal_output_is_rejected():
    original = "TP"
    translated = (
        "\u60a8\u6ca1\u6709\u63d0\u4f9b\u5177\u4f53\u7684\u65e5\u8bed\u6e38\u620f\u6587\u672c\u5185\u5bb9\u3002"
        "\u8bf7\u5c06\u9700\u8981\u7ffb\u8bd1\u7684\u65e5\u8bed\u6587\u672c\u7c98\u8d34\u8fc7\u6765\uff0c"
        "\u6211\u4f1a\u4e3a\u60a8\u7ffb\u8bd1\u6210\u81ea\u7136\u6d41\u7545\u7684\u4e2d\u6587\u3002"
    )

    safe, warnings = verify_translation(original, translated)

    assert safe == original
    assert warnings


def test_short_ui_markdown_explanation_is_rejected():
    original = "\u6b66\u5668"
    translated = (
        "\u5728\u65e5\u8bed\u6e38\u620f\u6587\u672c\u4e2d\uff0c\u201c\u6b66\u5668\u201d\u901a\u5e38\u76f4\u63a5\u8bd1\u4e3a"
        "\u201c\u6b66\u5668\u201d\u3002\u4e5f\u53ef\u4ee5\u66f4\u53e3\u8bed\u5316\u4e3a\uff1a**\u88c5\u5907**"
    )

    safe, warnings = verify_translation(original, translated)

    assert safe == original
    assert warnings


def test_punctuation_only_source_does_not_accept_filler():
    original = "\uff1f"
    translated = "\u55ef\uff1f"

    safe, warnings = verify_translation(original, translated)

    assert safe == original
    assert warnings


def test_short_source_long_explanation_is_rejected():
    original = "\u3060\u3063\u305f\u3002"
    translated = "\uff08\u4ec0\u4e48\u90fd\u6ca1\u8bf4\uff0c\u53ea\u662f\u5728\u5fc3\u91cc\u8fd9\u4e48\u60f3\u3002\uff09"

    safe, warnings = verify_translation(original, translated)

    assert safe == original
    assert warnings


def test_mojibake_refusal_output_is_rejected():
    original = "vE&vvDvO]"
    translated = (
        "抱歉，您提供的文本“vE&vvDvO]”看起来像是乱码或键盘误输入，"
        "并不是有意义的英语游戏文本。请确认后重新提供正确的原文。"
    )

    safe, warnings = verify_translation(original, translated)

    assert safe == original
    assert warnings


def test_normal_apology_dialogue_is_not_rejected_as_refusal():
    original = "悪いな、日直の用事で手間取って……先に行ってもらってて正解だったぜ」"
    translated = "抱歉啊，值日的事情拖了点时间……让你先走果然是对的」"

    safe, warnings = verify_translation(original, translated)

    assert safe == translated
    assert not warnings


def test_normal_cannot_judge_dialogue_is_not_rejected_as_refusal():
    original = "「ただし、右腕に関しては……申し訳ないが私には判断が付かない」"
    translated = "「不过，关于右臂……很抱歉，我无法判断」"

    safe, warnings = verify_translation(original, translated)

    assert safe == translated
    assert not warnings


def test_normal_cannot_understand_dialogue_is_not_rejected_as_refusal():
    original = "「私にはもう、理解できません……」"
    translated = "「我已经无法理解了……」"

    safe, warnings = verify_translation(original, translated)

    assert safe == translated
    assert not warnings


def test_character_name_question_is_not_rejected_as_model_explanation():
    original = "\u300c\u30d5\u30a1\u30f3\u30b0\u306f\u4eba\u540d\uff1f\u3000\u6f64\u306e\u80b2\u3066\u306e\u89aa\u3067\u3059\u304b\uff1f\u300d"
    translated = "\u300c\u65b9\u683c\u662f\u4eba\u540d\uff1f\u662f\u6f64\u7684\u517b\u7236\u6bcd\u5417\uff1f\u300d"

    safe, warnings = verify_translation(original, translated)

    assert safe == translated
    assert not warnings
