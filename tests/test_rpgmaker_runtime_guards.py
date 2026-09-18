from __future__ import annotations

from pathlib import Path

from core.rpgmaker_runtime import (
    _build_runtime_translation_map,
    _build_display_coverage_report,
    _sanitize_translation_map,
    _scan_event_list,
    _should_skip,
)
from core.rpgmaker_event_extraction import (
    RPGMAKER_MESSAGE_CONTRACT,
    RPGMAKER_MESSAGE_SEPARATOR,
    _scan_event_records,
)
from engines.base import TextItem


def test_rpgmaker_hook_covers_choices_help_and_draw_text_ex():
    hook = (Path(__file__).resolve().parent.parent / "engines" / "assets" / "rpgmaker_hook.js").read_text(
        encoding="utf-8"
    )

    assert "Game_Message.prototype.setChoices" in hook
    assert "Game_Message.prototype.setChoiceHelpTexts" in hook
    assert "Game_Message.prototype.setTexts" in hook
    assert "Game_Message.prototype.setSpeakerName" in hook
    assert "Window_Message.prototype.showNameWindow" in hook
    assert "function _replaceTextTree(value)" in hook
    assert "Window_Base.prototype.drawTextEx" in hook
    assert "Window_Base.prototype.createTextState" in hook
    assert "Window_Help.prototype.setText" in hook
    assert "Window_Command.prototype.addCommand" in hook
    assert "function _normalizeResourcePath(value)" in hook
    assert "normalize('NFC')" in hook
    assert "ImageManager.loadNormalBitmap" in hook
    assert "hooked ImageManager.loadNormalBitmap NFC compatibility" in hook
    assert "RPGM_SEGMENT" in hook
    assert "segments.join(RPGM_SEGMENT)" in hook


def test_rpgmaker_extracts_visible_choice_help_without_translating_marker():
    commands = [
        {"code": 102, "parameters": [["\u30aa\u30fc\u30d7\u30cb\u30f3\u30b0\u3092\u898b\u308b"], -1, 0, 2, 0]},
        {"code": 108, "parameters": ["\u9078\u629e\u80a2\u30d8\u30eb\u30d7"]},
        {"code": 408, "parameters": ["\u56de\u60f3\u30eb\u30fc\u30e0\u306e\u5168\u958b\u653e\u30b9\u30a4\u30c3\u30c1"]},
        {"code": 408, "parameters": ["\u4e00\u56de\u4ee5\u4e0a\u9078\u3093\u3067\u304f\u3060\u3055\u3044\u3002"]},
        {"code": 108, "parameters": ["\u958b\u767a\u7528\u30b3\u30e1\u30f3\u30c8"]},
        {"code": 408, "parameters": ["\u3053\u308c\u306f\u8868\u793a\u3057\u306a\u3044"]},
    ]

    extracted = _scan_event_list(commands, "CmEv.1")
    values = [item[0] for item in extracted]

    assert "\u30aa\u30fc\u30d7\u30cb\u30f3\u30b0\u3092\u898b\u308b" in values
    assert "\u56de\u60f3\u30eb\u30fc\u30e0\u306e\u5168\u958b\u653e\u30b9\u30a4\u30c3\u30c1" in values
    assert "\u4e00\u56de\u4ee5\u4e0a\u9078\u3093\u3067\u304f\u3060\u3055\u3044\u3002" in values
    assert "\u9078\u629e\u80a2\u30d8\u30eb\u30d7" not in values
    assert "\u958b\u767a\u7528\u30b3\u30e1\u30f3\u30c8" not in values
    assert "\u3053\u308c\u306f\u8868\u793a\u3057\u306a\u3044" not in values


def test_rpgmaker_groups_message_lines_and_extracts_speaker_name():
    commands = [
        {"code": 101, "parameters": ["", 0, 0, 2, ""]},
        {"code": 401, "parameters": ["\\n<\u304a\u3058\u3058>\u300c\u4eca\u65e5\u306f\u5916\u306e\u8a71\u3092"]},
        {"code": 401, "parameters": ["\u3000\u805e\u304b\u305b\u3066\u3042\u3052\u3088\u3046\u300d"]},
        {"code": 101, "parameters": ["", 0, 0, 2, ""]},
        {"code": 401, "parameters": ["\\n<\u30b3\u30cf\u30af>\u300c\u3046\u3093\uff01\u300d"]},
    ]

    records = _scan_event_records(commands, "CmEv.1.dialogue")
    messages = [record for record in records if record["context"].endswith(".line")]
    speakers = [record for record in records if record["meta"].get("rpgmaker_speaker_name")]

    assert len(messages) == 2
    assert messages[0]["text"] == (
        "\\n<\u304a\u3058\u3058>\u300c\u4eca\u65e5\u306f\u5916\u306e\u8a71\u3092"
        + RPGMAKER_MESSAGE_SEPARATOR
        + "\u3000\u805e\u304b\u305b\u3066\u3042\u3052\u3088\u3046\u300d"
    )
    assert messages[0]["meta"]["translation_contract"] == RPGMAKER_MESSAGE_CONTRACT
    assert messages[0]["meta"]["rpgmaker_segments"] == [
        "\\n<\u304a\u3058\u3058>\u300c\u4eca\u65e5\u306f\u5916\u306e\u8a71\u3092",
        "\u3000\u805e\u304b\u305b\u3066\u3042\u3052\u3088\u3046\u300d",
    ]
    assert [record["text"] for record in speakers] == ["\u304a\u3058\u3058", "\u30b3\u30cf\u30af"]


def test_rpgmaker_runtime_map_splits_group_without_crossing_speakers():
    speaker = TextItem(
        file="hook",
        original="\u304a\u3058\u3058",
        translated="\u7237\u7237",
        context="CmEv.1.name",
        meta={"rpgmaker_speaker_name": True},
    )
    message = TextItem(
        file="hook",
        original=(
            "\\n<\u304a\u3058\u3058>\u300c\u4eca\u65e5\u306f\u5916\u306e\u8a71\u3092"
            + RPGMAKER_MESSAGE_SEPARATOR
            + "\u3000\u805e\u304b\u305b\u3066\u3042\u3052\u3088\u3046\u300d"
        ),
        translated=(
            "\\n<\u304a\u3058\u3058>\u300c\u4eca\u5929\u7ed9\u4f60\u8bb2\u8bb2\u5916\u9762\u7684\u4e16\u754c"
            + RPGMAKER_MESSAGE_SEPARATOR
            + "\u3000\u5427\u3002\u300d"
        ),
        context="CmEv.1.line",
        meta={
            "rpgmaker_segments": [
                "\\n<\u304a\u3058\u3058>\u300c\u4eca\u65e5\u306f\u5916\u306e\u8a71\u3092",
                "\u3000\u805e\u304b\u305b\u3066\u3042\u3052\u3088\u3046\u300d",
            ],
            "rpgmaker_speaker": "\u304a\u3058\u3058",
        },
    )

    mapping, stats = _build_runtime_translation_map([speaker, message])
    cleaned = _sanitize_translation_map(mapping)

    assert cleaned == {
        "\\n<\u304a\u3058\u3058>\u300c\u4eca\u65e5\u306f\u5916\u306e\u8a71\u3092": "\\n<\u7237\u7237>\u300c\u4eca\u5929\u7ed9\u4f60\u8bb2\u8bb2\u5916\u9762\u7684\u4e16\u754c",
        "\u805e\u304b\u305b\u3066\u3042\u3052\u3088\u3046\u300d": "\u5427\u3002\u300d",
        "\u304a\u3058\u3058": "\u7237\u7237",
    }
    assert stats["grouped_messages"] == 1
    assert stats["expanded_lines"] == 2

    report = _build_display_coverage_report([speaker, message], mapping, cleaned)
    assert report["mapped_sources"] == 3
    assert report["unmapped_sources"] == 0
    assert report["display_map_coverage_percent"] == 100.0


def test_rpgmaker_runtime_map_drops_speaker_tag_leaked_into_continuation():
    cleaned = _sanitize_translation_map({
        "\u7d20\u6674\u3089\u3057\u3044\u53cb\u4eba\u304c\u51fa\u6765\u308b\u3055\u300d": "<\u7237\u7237>\u300c\u6b21\u306e\u53f0\u8bcd\u300d",
    })

    assert cleaned == {}


def test_rpgmaker_extracts_visible_plugin_args_but_not_control_fields():
    nested_choices = '["{\\"label\\":\\"\u30c6\u30e9\u30b9\u3092\u5b88\u308b\\",\\"switchId\\":\\"1\\"}"]'
    commands = [
        {
            "code": 357,
            "parameters": [
                "LL_GalgeChoiceWindow",
                "showChoice",
                "",
                {
                    "messageText": "\u30eb\u30fc\u30c8\u3092\u9078\u3093\u3067\u304f\u3060\u3055\u3044\u3002",
                    "choices": nested_choices,
                    "script": "console.log('debug')",
                    "name": "\u30c6\u30e9\u30b9",
                    "position": "\u53f3",
                },
            ],
        }
    ]

    values = [item[0] for item in _scan_event_list(commands, "CmEv.2")]

    assert "\u30eb\u30fc\u30c8\u3092\u9078\u3093\u3067\u304f\u3060\u3055\u3044\u3002" in values
    assert "\u30c6\u30e9\u30b9\u3092\u5b88\u308b" in values
    assert "\u30c6\u30e9\u30b9" not in values
    assert "\u53f3" not in values
    assert not any("console.log" in value for value in values)


def test_rpgmaker_skips_fullwidth_punctuation_fragments():
    assert _should_skip("\uff1f")
    assert _should_skip("\u2026\u2026\uff01")
    assert _should_skip("\uff01\uff1f")


def test_rpgmaker_skips_literal_system_tokens():
    assert _should_skip("TP")
    assert _should_skip("png")
    assert _should_skip("blob")


def test_rpgmaker_keeps_real_kana_dialogue_lines():
    assert not _should_skip("\u3048\uff1f\u3000\u305d\u3093\u306a\u3053\u3068\u3042\u308a\u307e\u305b\u3093\u3088\uff1f")


def test_rpgmaker_translation_map_drops_polluted_entries():
    cleaned = _sanitize_translation_map(
        {
            "\uff1f": "\u55ef\uff1f",
            "TP": "\u60a8\u6ca1\u6709\u63d0\u4f9b\u5177\u4f53\u7684\u65e5\u8bed\u6e38\u620f\u6587\u672c\u5185\u5bb9\u3002",
            "\u6b66\u5668": "\u201c\u6b66\u5668\u201d\u53ef\u4ee5\u7ffb\u8bd1\u4e3a\uff1a**\u6b66\u5668**",
            "\u3048\uff1f\u3000\u305d\u3093\u306a\u3053\u3068\u3042\u308a\u307e\u305b\u3093\u3088\uff1f": "\u8bf6\uff1f\u6ca1\u90a3\u56de\u4e8b\u554a\uff1f",
        }
    )

    assert cleaned == {
        "\u3048\uff1f\u3000\u305d\u3093\u306a\u3053\u3068\u3042\u308a\u307e\u305b\u3093\u3088\uff1f": "\u8bf6\uff1f\u6ca1\u90a3\u56de\u4e8b\u554a\uff1f"
    }


def test_rpgmaker_display_coverage_reports_actual_map_and_kana_gaps():
    translated = "\u30aa\u30fc\u30d7\u30cb\u30f3\u30b0\u3092\u898b\u308b"
    missing = "\u4e00\u56de\u4ee5\u4e0a\u9078\u3093\u3067\u304f\u3060\u3055\u3044\u3002"
    items = [
        TextItem(file="hook", original=translated, context="choice"),
        TextItem(file="hook", original=missing, context="choice_help"),
        TextItem(file="hook", original="\u5973\u6027", context="speaker"),
        TextItem(file="hook", original="\uff1f", context="punctuation"),
    ]

    report = _build_display_coverage_report(
        items,
        {translated: "\u89c2\u770b\u5f00\u573a"},
        {translated: "\u89c2\u770b\u5f00\u573a"},
    )

    assert report["extracted_entries"] == 4
    assert report["extracted_unique_sources"] == 3
    assert report["mapped_sources"] == 1
    assert report["unmapped_sources"] == 2
    assert report["unmapped_with_kana_count"] == 1
    assert report["unmapped_with_kana_samples"][0]["source"] == missing
    assert report["unmapped_cjk_without_kana_count"] == 1
