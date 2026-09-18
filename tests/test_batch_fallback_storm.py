"""单条兜底风暴回归：已是目标语言的条目不得反复触发单条重译。

真实案例：日语 RPGMaker 游戏数据库里混有中文/数字条目（"城镇2"、"攻击+3"），
旧逻辑逐条判"翻译失败"→ 单条兜底 → 结果相同再回退原文，白烧两倍请求；
几百条一起触发限流，整轮翻译从 1 分钟拖到 5 分钟以上。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engines.base import TextItem
from translators.deepseek import DeepSeekTranslator
from utils.text_extract import contains_kana, is_acceptable_same_as_source


def _item(text: str) -> TextItem:
    return TextItem(file="t", key="k", original=text, translated="", context="dialogue")


def test_same_as_source_accepts_han_digit_symbol_mixes():
    for text in ("城镇2", "攻击+3", "電源", "１００％勝利"):
        assert is_acceptable_same_as_source(text, text), text


def test_same_as_source_still_rejects_untranslated_content():
    assert not is_acceptable_same_as_source("こんにちは", "こんにちは")  # 假名=还是日文
    assert not is_acceptable_same_as_source("Hello", "Hello")            # 无汉字
    assert not is_acceptable_same_as_source("12345", "12345")            # 纯数字
    assert not is_acceptable_same_as_source("城镇2", "城市2")            # 真的翻了


def test_batch_validation_accepts_already_chinese_entry():
    tr = DeepSeekTranslator()
    assert tr._validate_batch_translation(_item("城镇2"), "城镇2") == "城镇2"
    assert tr._validate_batch_translation(_item("攻击+3"), "攻击+3") == "攻击+3"


def test_batch_validation_rejects_same_pure_kanji_for_ja_source():
    tr = DeepSeekTranslator()
    for text in ("勝利", "敗北", "情報提供", "選択肢"):
        assert tr._validate_batch_translation(_item(text), text, "ja") is None
        assert tr._should_store_translation(_item(text), text, "ja") is False
        assert tr._valid_cached_translation_for_item(_item(text), text, "ja") is None


def test_batch_validation_still_accepts_database_tokens_for_ja_source():
    tr = DeepSeekTranslator()
    assert tr._validate_batch_translation(_item("城镇2"), "城镇2", "ja") == "城镇2"
    assert tr._validate_batch_translation(_item("攻击+3"), "攻击+3", "ja") == "攻击+3"
    assert tr._is_same_as_source_refusal(_item("HP+10"), "HP+10", "ja") is True


def test_refusal_shortcut_only_for_kana_free_ja_sources():
    tr = DeepSeekTranslator()
    # 日语源、无假名、含数字/符号、结果=原文 → 采纳原样，不进单条兜底
    assert tr._is_same_as_source_refusal(_item("HP+10"), "HP+10", "ja") is True
    assert tr._is_same_as_source_refusal(_item("城镇2"), "城镇2", "ja") is True
    assert tr._is_same_as_source_refusal(_item("攻击+3"), "攻击+3", "ja") is True
    assert tr._is_same_as_source_refusal(_item("100%勝利"), "100%勝利", "ja") is True

    # 纯汉字词（日文）必须重译，不能接受原样（关键负例：防漏翻）
    assert tr._is_same_as_source_refusal(_item("勝利"), "勝利", "ja") is False
    assert tr._is_same_as_source_refusal(_item("敗北"), "敗北", "ja") is False
    assert tr._is_same_as_source_refusal(_item("情報提供"), "情報提供", "ja") is False
    assert tr._is_same_as_source_refusal(_item("選択肢"), "選択肢", "ja") is False

    # 含假名说明还是日文，必须继续走重译
    assert tr._is_same_as_source_refusal(_item("エリクサー"), "エリクサー", "ja") is False
    # 非日语源不启用该捷径（英文源同文=漏翻，不能采纳）
    assert tr._is_same_as_source_refusal(_item("HP+10"), "HP+10", "en") is False
    # 结果和原文不同不算拒答
    assert tr._is_same_as_source_refusal(_item("城镇2"), "城市2", "ja") is False
    # 空结果不算
    assert tr._is_same_as_source_refusal(_item("城镇2"), "", "ja") is False


def test_contains_kana_signal():
    assert contains_kana("こんにちは")
    assert contains_kana("ｱｲｳ")
    assert not contains_kana("城镇2")
    assert not contains_kana("")
