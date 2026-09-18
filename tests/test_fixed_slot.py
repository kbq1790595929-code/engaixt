from utils.fixed_slot import clean_translation_text, fit_fixed_slot


def _encode_cp932(text: str) -> bytes:
    return text.encode("cp932", errors="replace")


def test_clean_translation_text_removes_model_boilerplate():
    text = "好的，这是您要求的自然流畅的中文翻译：「嗯，我本来只是在那儿玩过。」"

    assert clean_translation_text(text) == "「嗯，我本来只是在那儿玩过。」"


def test_clean_translation_text_keeps_normal_dialogue():
    text = "好的，那我先走了。"

    assert clean_translation_text(text) == text


def test_clean_translation_text_preserves_ellipsis_in_dialogue():
    assert clean_translation_text("「……诶……真的？」") == "「……诶……真的?」"
    assert clean_translation_text("洗澡……！？") == "洗澡……!?"


def test_clean_translation_text_removes_dirty_full_stop_artifacts():
    assert clean_translation_text("「。嗯? ...」") == "「嗯? …」"
    assert clean_translation_text("洗澡。!?") == "洗澡!?"
    assert clean_translation_text("。好，继续") == "好，继续"


def test_tiny_family_label_prefers_meaningful_single_character():
    assert fit_fixed_slot("老爸", 2, _encode_cp932) == "爸"


def test_slot_rewrite_validation_rejects_over_budget():
    from utils.bgi_slot_rewrite import BgiSlotRewriteCandidate, _validate_result

    candidate = BgiSlotRewriteCandidate(
        original="父",
        current="老爸",
        slot_bytes=2,
        current_bytes=4,
    )

    assert _validate_result(candidate, "爸", b"")[0]
    assert not _validate_result(candidate, "老爸", b"")[0]
