from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from engines.base import TextItem
from utils.text_extract import (
    is_acceptable_same_as_source,
    protect_placeholders,
    restore_placeholders,
    verify_translation,
)


async def retry_with_backoff(fn, max_retries=3, base_delay=2.0):
    """带指数退避的重试，处理 API 限流。"""
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as e:
            msg = str(e).lower()
            is_rate_limit = any(k in msg for k in (
                "rate", "limit", "429", "too many requests",
                "quota", "overloaded", "503", "service unavailable",
            ))
            if is_rate_limit and attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                await asyncio.sleep(delay)
                continue
            raise


class TranslatorBase(ABC):
    name: str = "base"
    label: str = "基础翻译器"

    @abstractmethod
    async def translate_batch(
        self,
        items: list[TextItem],
        source_lang: str,
        target_lang: str,
        on_progress: "Callable[[int, int], None] | None" = None,
    ) -> list[TextItem]: ...

    def prepare_prompt(self, text: str, source_lang: str, target_lang: str,
                        examples: list[tuple[str, str]] | None = None,
                        prev_text: str = "", next_text: str = "") -> tuple[str, list[str]]:
        """返回 (提示词, 占位符列表)。占位符列表用于翻译后还原。

        Args:
            examples: 可选的最多 10 条 (原文, 译文) 作为短期记忆示例。
            prev_text: 同一场景中的前一行对话（不翻译，仅上下文）
            next_text: 同一场景中的后一行对话（不翻译，仅上下文）
        """
        protected_text, placeholders = protect_placeholders(text)

        lang_map = {
            "ja": "日语", "en": "英语", "ko": "韩语", "ru": "俄语",
            "zh-CN": "简体中文", "zh-TW": "繁体中文",
        }
        src = lang_map.get(source_lang, source_lang)
        tgt = lang_map.get(target_lang, target_lang)

        # 只在实际有占位符时才加入保护指令，避免 AI 幻觉
        placeholder_rule = ""
        if placeholders:
            placeholder_rule = (
                "1. {{PH0}} {{PH1}} 等是游戏控制码，绝对禁止修改、删除、移动、翻译或拆分。\n"
                "   它们必须原封不动地出现在译文中。哪怕只差一个字符也算错误。\n"
            )
        else:
            placeholder_rule = (
                "1. 绝对不要添加任何标记或符号（包括 {{ }} 【 】 [ ] 等所有括号标记）。\n"
                "   只输出纯中文译文，一个字都别多。\n"
            )

        # 邻近对话上下文（帮助理解场景和保持术语一致）
        context_block = ""
        if prev_text or next_text:
            context_block = "## 场景上下文（同一角色在同一场景中的前后对话，不需翻译，仅用于理解语境）\n"
            if prev_text:
                context_block += f"- 前一行：{prev_text}\n"
            context_block += f"- 当前行：{text}\n"
            if next_text:
                context_block += f"- 后一行：{next_text}\n"
            context_block += "请根据前后文推断说话人的身份、语气和场景，保持翻译风格一致。\n\n"

        # 短期记忆示例
        examples_block = ""
        if examples:
            examples_block = "## 参考示例（请保持术语、风格、语气一致）\n"
            for orig, trans in examples:
                examples_block += f"- {orig} → {trans}\n"
            examples_block += "\n"

        prompt = (
            f"{examples_block}{context_block}"
            f"将以下{src}游戏文本翻译成自然流畅的{tgt}。\n"
            f"要求：地道口语、符合语境、不死译不硬译。\n"
            f"{placeholder_rule}"
            "2. 只输出译文本身，不要解释、注释、引号或 Markdown。\n\n"
            f"{protected_text}"
        )
        return prompt, placeholders

    def valid_cached_translation(self, original: str, cached: str | None) -> str | None:
        if not cached or not cached.strip():
            return None
        safe, _warns = verify_translation(original, cached)
        if self.should_cache_translation(original, safe):
            return safe
        return None

    def should_cache_translation(self, original: str, translated: str | None) -> bool:
        if not translated or not translated.strip():
            return False
        safe, _warns = verify_translation(original, translated)
        if safe != translated and safe == original and translated != original:
            return False
        translated = safe
        if translated != original:
            return True
        return is_acceptable_same_as_source(original, translated)

    def prepare_retry_feedback(self, text: str, failed_result: str, warnings: list[str]) -> str:
        """翻译失败后，将具体错误信息反馈给 AI 进行修正。"""
        protected_text, placeholders = protect_placeholders(text)
        error_lines = "\n".join(f"  - {w}" for w in warnings)
        warning_text = "\n".join(warnings)
        correction_rules: list[str] = []
        if "假名" in warning_text:
            correction_rules.append(
                "译文中不得保留任何日文平假名或片假名；专有名词、路线名后缀和拟声词也要改成中文或中文音译。"
            )
        if placeholders:
            placeholder_tokens = " ".join(f"{{{{PH{i}}}}}" for i in range(len(placeholders)))
            correction_rules.append(
                f"必须按原顺序完整保留这些控制码占位符，每个恰好一次：{placeholder_tokens}"
            )
        correction_block = ""
        if correction_rules:
            correction_block = "\n".join(f"  - {rule}" for rule in correction_rules) + "\n\n"
        return (
            f"你上次的翻译存在以下问题：\n{error_lines}\n\n"
            f"请重新翻译，务必避开这些错误：\n{correction_block}"
            f"原文：{protected_text}\n\n"
            f"你上次的错误译文（仅供参考，不要照抄）：\n{failed_result}\n\n"
            f"修正后的译文（只输出译文本身）："
        )
