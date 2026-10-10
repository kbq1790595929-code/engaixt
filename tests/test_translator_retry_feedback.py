from __future__ import annotations

from translators.base import TranslatorBase


class _Translator(TranslatorBase):
    async def translate_batch(self, items, source_lang, target_lang, on_progress=None):
        return items


def test_single_prompt_includes_control_context_and_examples():
    translator = _Translator()

    prompt, placeholders = translator.prepare_prompt(
        "__SE_3__いいえ",
        "ja",
        "zh-CN",
        examples=[("はい", "是")],
        prev_text="前の行",
        next_text="次の行",
    )

    assert placeholders == ["__SE_3__"]
    assert "{{PH0}}" in prompt
    assert "绝对禁止修改、删除、移动" in prompt
    assert "参考示例" in prompt
    assert "场景上下文" in prompt


def test_retry_feedback_forbids_kana_and_requires_all_placeholders():
    translator = _Translator()

    prompt = translator.prepare_retry_feedback(
        "__SE_3____COLOR_27__ステージ１ボス撃破済",
        "ステージ1首领已击破",
        ["译文仍包含日文假名残留", "危险！AI 破坏了控制符"],
    )

    assert "不得保留任何日文平假名或片假名" in prompt
    assert "{{PH0}} {{PH1}}" in prompt
    assert "每个恰好一次" in prompt
