from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import get_config
from engines.base import TextItem
from translators.base import retry_with_backoff
from translators.cache import (
    get_cache,
)
from translators.pricing import DEEPSEEK_V4_FLASH, cost_cny, pricing_dict
from utils.logger import info, warning
from utils.text_extract import (
    is_acceptable_same_as_source,
    protect_placeholders,
    restore_placeholders,
    verify_translation,
)


POLISH_VERSION = "zh_polish_v1"
MODEL = "deepseek-v4-flash"
DEEPSEEK_V4_FLASH_INPUT_CNY_PER_M = DEEPSEEK_V4_FLASH.input_cny_per_m
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff\uff66-\uff9f]")
_ROUGH_HINT_RE = re.compile(
    r"(的的|了了|我我|你你|他他|她她|这个这个|那个那个|"
    r"进行着|拥有着|所进行|所拥有|被.+所|之中|于此|并且|然而|因此|"
    r"吗？。|吧？。|！？。|。。|，，|、、|……。)"
)
_QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"), ('"', '"'))
_NARRATION_ANCHORS = (
    "回应", "回答", "答道", "说道", "说着", "问道", "询问", "解释",
    "嘟囔", "低语", "喊道", "叫道", "想着", "心想",
)
_TERMINAL_PARTICLES = ("吧", "呢", "啊", "呀", "嘛", "哦", "啦", "喽")
_TERMINAL_PUNCT = "。！？?!…"


@dataclass
class PolishStats:
    total_texts: int = 0
    eligible_texts: int = 0
    skipped_names: int = 0
    skipped_already_polished: int = 0
    skipped_heuristic_ok: int = 0
    budget_limited: int = 0
    api_request_count: int = 0
    batch_request_count: int = 0
    batch_item_count: int = 0
    changed_count: int = 0
    unchanged_count: int = 0
    validation_fail_count: int = 0
    json_parse_fail_count: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    estimated_cost_cny: float = 0.0
    actual_input_tokens: int = 0
    actual_output_tokens: int = 0
    actual_cost_cny: float = 0.0
    provider: str = "deepseek"
    model: str = MODEL
    polish_version: str = POLISH_VERSION
    budget_cny: float = 1.0
    started_at: float = field(default_factory=time.time)

    def add_api_call(self, prompt: str, result: str, usage: Any = None) -> None:
        cache = get_cache()
        self.api_request_count += 1
        before_estimated = float(self.estimated_cost_cny)
        before_actual = float(self.actual_cost_cny)
        self.estimated_input_tokens += cache.estimate_tokens(prompt)
        self.estimated_output_tokens += cache.estimate_tokens(result)
        self.estimated_cost_cny = _cost(self.estimated_input_tokens, self.estimated_output_tokens)
        if usage is not None:
            prompt_tokens = getattr(usage, "prompt_tokens", None)
            completion_tokens = getattr(usage, "completion_tokens", None)
            if prompt_tokens is None:
                prompt_tokens = getattr(usage, "input_tokens", None)
            if completion_tokens is None:
                completion_tokens = getattr(usage, "output_tokens", None)
            if prompt_tokens is not None:
                self.actual_input_tokens += int(prompt_tokens)
            if completion_tokens is not None:
                self.actual_output_tokens += int(completion_tokens)
            self.actual_cost_cny = _cost(self.actual_input_tokens, self.actual_output_tokens)
        if usage is not None and self.actual_cost_cny > before_actual:
            cost_delta = round(self.actual_cost_cny - before_actual, 6)
            cost_basis = "actual"
        else:
            cost_delta = round(self.estimated_cost_cny - before_estimated, 6)
            cost_basis = "estimated"
        if cost_delta > 0:
            try:
                from core.trial_quota import TrialQuotaExceeded, charge_translation_cost

                charge_translation_cost(
                    cost_delta,
                    source="deepseek_polish_api",
                    details={
                        "basis": cost_basis,
                        "provider": self.provider,
                        "model": self.model,
                        "polish_version": self.polish_version,
                        "pricing": pricing_dict(self.provider, self.model),
                    },
                )
            except TrialQuotaExceeded:
                raise
            except Exception:
                pass

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_texts": self.total_texts,
            "eligible_texts": self.eligible_texts,
            "skipped_names": self.skipped_names,
            "skipped_already_polished": self.skipped_already_polished,
            "skipped_heuristic_ok": self.skipped_heuristic_ok,
            "budget_limited": self.budget_limited,
            "api_request_count": self.api_request_count,
            "batch_request_count": self.batch_request_count,
            "batch_item_count": self.batch_item_count,
            "changed_count": self.changed_count,
            "unchanged_count": self.unchanged_count,
            "validation_fail_count": self.validation_fail_count,
            "json_parse_fail_count": self.json_parse_fail_count,
            "estimated_input_tokens": self.estimated_input_tokens,
            "estimated_output_tokens": self.estimated_output_tokens,
            "estimated_cost_cny": self.estimated_cost_cny,
            "actual_input_tokens": self.actual_input_tokens,
            "actual_output_tokens": self.actual_output_tokens,
            "actual_cost_cny": self.actual_cost_cny,
            "provider": self.provider,
            "model": self.model,
            "polish_version": self.polish_version,
            "budget_cny": self.budget_cny,
            "elapsed_seconds": round(time.time() - self.started_at, 3),
        }


@dataclass
class PolishResult:
    items: list[TextItem]
    stats: PolishStats


class DeepSeekPolisher:
    MAX_BATCH_ITEMS = 80
    MAX_BATCH_INPUT_TOKENS = 2600
    MAX_CONCURRENCY = 24
    DEFAULT_BUDGET_CNY = 1.0

    async def polish_items(
        self,
        items: list[TextItem],
        *,
        budget_cny: float = DEFAULT_BUDGET_CNY,
        on_progress=None,
    ) -> PolishResult:
        stats = PolishStats(total_texts=len(items), budget_cny=float(budget_cny))
        candidates = self._select_candidates(items, stats, budget_cny)
        stats.eligible_texts = len(candidates)
        if not candidates:
            info("文本润色: 没有需要润色的条目")
            return PolishResult(items, stats)

        config = get_config()
        api_key = config.deepseek_api_key or config.openai_api_key
        if not api_key:
            warning("文本润色需要 DeepSeek API Key，已跳过")
            return PolishResult(items, stats)

        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        batches = self._make_batches(candidates)
        sem = asyncio.Semaphore(max(1, int(config.max_concurrency or 1)))
        done = [0]
        total = len(candidates)

        info(f"文本润色: {total} 条候选，{len(batches)} 批，预算约 ¥{budget_cny:.2f}")

        async def run_batch(batch: list[TextItem]) -> None:
            async with sem:
                await self._polish_batch(batch, client, stats)
                done[0] += len(batch)
                if on_progress:
                    try:
                        on_progress(min(done[0], total), total)
                    except Exception:
                        pass

        await asyncio.gather(*(run_batch(batch) for batch in batches))
        for item in candidates:
            _mark_polished(item)
        return PolishResult(items, stats)

    def _select_candidates(self, items: list[TextItem], stats: PolishStats, budget_cny: float) -> list[TextItem]:
        cache = get_cache()
        selected: list[TextItem] = []
        estimated_input = 0
        budget_tokens = int(max(0.05, budget_cny) / DEEPSEEK_V4_FLASH_INPUT_CNY_PER_M * 1_000_000)
        input_cap = int(budget_tokens * 0.65)
        for item in items:
            if not _has_polishable_translation(item):
                continue
            kind = _text_type(item)
            if kind in {"name", "choice", "ui/system"}:
                stats.skipped_names += 1
                continue
            if _is_polish_current(item):
                stats.skipped_already_polished += 1
                continue
            if not self._needs_polish(item):
                stats.skipped_heuristic_ok += 1
                continue
            text_tokens = cache.estimate_tokens(item.translated) + cache.estimate_tokens(item.original[:80]) + 8
            if selected and estimated_input + text_tokens > input_cap:
                stats.budget_limited += 1
                continue
            estimated_input += text_tokens
            selected.append(item)
        return selected

    def _needs_polish(self, item: TextItem) -> bool:
        text = (item.translated or "").strip()
        if len(text) >= 18:
            return True
        if _ROUGH_HINT_RE.search(text):
            return True
        return bool(_KANA_RE.search(text))

    def _make_batches(self, items: list[TextItem]) -> list[list[TextItem]]:
        cache = get_cache()
        batches: list[list[TextItem]] = []
        current: list[TextItem] = []
        for item in items:
            candidate = current + [item]
            if current and (
                len(candidate) > self.MAX_BATCH_ITEMS
                or cache.estimate_tokens(self._build_prompt(candidate)) > self.MAX_BATCH_INPUT_TOKENS
            ):
                batches.append(current)
                current = [item]
            else:
                current = candidate
        if current:
            batches.append(current)
        return batches

    async def _polish_batch(self, batch: list[TextItem], client: Any, stats: PolishStats) -> None:
        prompt = self._build_prompt(batch)
        stats.batch_request_count += 1
        stats.batch_item_count += len(batch)
        max_tokens = max(128, min(2048, int(sum(len(i.translated or "") for i in batch) * 0.8 + 128)))
        try:
            raw = await self._call_json(client, prompt, max_tokens, stats)
            changes = _parse_polish_json(raw)
        except Exception as exc:
            stats.json_parse_fail_count += 1
            warning(f"润色批处理失败，保留原译文: {exc}")
            return

        by_id = {idx: item for idx, item in enumerate(batch, 1)}
        changed_ids = set()
        for rid, polished in changes.items():
            item = by_id.get(rid)
            if item is None:
                continue
            safe = _validate_polished(item, polished)
            if safe is None:
                stats.validation_fail_count += 1
                continue
            if safe != item.translated:
                item.translated = safe
                stats.changed_count += 1
                changed_ids.add(rid)
        stats.unchanged_count += max(0, len(batch) - len(changed_ids))

    async def _call_json(self, client: Any, prompt: str, max_tokens: int, stats: PolishStats) -> str:
        async def call():
            from core.trial_quota import ensure_translation_quota_available

            ensure_translation_quota_available()
            resp = await client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=max_tokens,
                timeout=45,
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
            )
            content = resp.choices[0].message.content.strip()
            stats.add_api_call(prompt, content, getattr(resp, "usage", None))
            return content

        return await retry_with_backoff(call)

    def _build_prompt(self, batch: list[TextItem]) -> str:
        records = []
        for idx, item in enumerate(batch, 1):
            protected, _ = protect_placeholders(item.translated or "")
            records.append({
                "i": idx,
                "o": (item.original or "")[:140],
                "t": protected,
            })
        return (
            "你是中文游戏文本润色器。只审校已有中文译文，不重新翻译原文。\n"
            "目标：让中文更自然、口语、顺畅，去掉翻译腔；保持角色语气。\n"
            "硬规则：不改人名/术语/控制符/占位符/换行语义；不增删剧情信息；不解释；不扩写。\n"
            "只有明显更自然时才返回改写；原译文已经可以接受就不要返回该项。\n"
            "输出 JSON 对象，格式必须是 {\"p\":{\"1\":\"润色后译文\"}}。p 中只放需要改的 id。\n"
            "输入 JSON:\n"
            + json.dumps(records, ensure_ascii=False, separators=(",", ":"))
            + "\n输出 JSON:"
        )


def _parse_polish_json(raw: str) -> dict[int, str]:
    text = (raw or "").strip()
    if text.startswith("```"):
        raise ValueError("response contains markdown fence")
    data = json.loads(text)
    if not isinstance(data, dict) or set(data.keys()) != {"p"} or not isinstance(data["p"], dict):
        raise ValueError("response must be {'p': {...}}")
    result: dict[int, str] = {}
    for key, value in data["p"].items():
        if not isinstance(value, str):
            raise ValueError("polished value must be string")
        rid = int(key)
        result[rid] = value.strip()
    return result


def _validate_polished(item: TextItem, polished: str) -> str | None:
    if not polished or not polished.strip():
        return None
    old = item.translated or ""
    if polished == old:
        return None
    _protected, placeholders = protect_placeholders(old)
    if placeholders:
        polished = restore_placeholders(polished, placeholders)
    polished = _preserve_quote_style(old, polished)
    if polished is None:
        return None
    if _is_low_value_change(old, polished):
        return None
    if _drops_narration_anchor(old, polished):
        return None
    if _drops_terminal_punctuation(old, polished):
        return None
    if _adds_terminal_particle(old, polished):
        return None
    if _drops_repeated_phrase(old, polished):
        return None
    if len(polished) > max(24, int(len(old) * 1.35) + 8):
        return None
    safe, warns = verify_translation(old, polished)
    if safe != polished:
        for warn in warns:
            warning(f"[润色校验][{old[:30]}...] {warn}")
        return None
    if _KANA_RE.search(polished) and not _KANA_RE.search(old):
        return None
    return safe


def _preserve_quote_style(old: str, polished: str) -> str | None:
    old_s = old.strip()
    new_s = polished.strip()
    old_pair = _outer_quote_pair(old_s)
    new_pair = _outer_quote_pair(new_s)
    if old_pair:
        if not new_pair:
            return None
        inner = new_s[1:-1]
        return old_pair[0] + inner + old_pair[1]
    if new_pair:
        return None
    return polished


def _outer_quote_pair(text: str) -> tuple[str, str] | None:
    if len(text) < 2:
        return None
    for opener, closer in _QUOTE_PAIRS:
        if text.startswith(opener) and text.endswith(closer):
            return opener, closer
    return None


def _is_low_value_change(old: str, polished: str) -> bool:
    def normalize(text: str) -> str:
        table = str.maketrans({
            "「": "", "」": "", "『": "", "』": "",
            "“": "", "”": "", "‘": "", "’": "",
            '"': "", "'": "",
        })
        return re.sub(r"\s+", "", text.translate(table))

    return normalize(old) == normalize(polished)


def _drops_narration_anchor(old: str, polished: str) -> bool:
    old_hits = [token for token in _NARRATION_ANCHORS if token in old]
    if not old_hits:
        return False
    return not any(token in polished for token in old_hits)


def _drops_terminal_punctuation(old: str, polished: str) -> bool:
    old_core = _strip_outer_quote(old.strip())
    new_core = _strip_outer_quote(polished.strip())
    if not old_core or not new_core:
        return False
    return old_core[-1] in _TERMINAL_PUNCT and new_core[-1] not in _TERMINAL_PUNCT


def _adds_terminal_particle(old: str, polished: str) -> bool:
    old_core = _strip_terminal_punct(_strip_outer_quote(old.strip()))
    new_core = _strip_terminal_punct(_strip_outer_quote(polished.strip()))
    if not old_core or not new_core:
        return False
    return (
        any(new_core.endswith(particle) for particle in _TERMINAL_PARTICLES)
        and not any(old_core.endswith(particle) for particle in _TERMINAL_PARTICLES)
    )


def _drops_repeated_phrase(old: str, polished: str) -> bool:
    old_core = _strip_outer_quote(old)
    new_core = _strip_outer_quote(polished)
    for match in re.finditer(r"([\u3400-\u4dbf\u4e00-\u9fff]{1,5})([，、,]\1)+", old_core):
        phrase = match.group(1)
        if new_core.count(phrase) < old_core.count(phrase):
            return True
    return False


def _strip_outer_quote(text: str) -> str:
    pair = _outer_quote_pair(text.strip())
    return text.strip()[1:-1] if pair else text.strip()


def _strip_terminal_punct(text: str) -> str:
    return text.rstrip(_TERMINAL_PUNCT + "。！？?!…")


def _has_polishable_translation(item: TextItem) -> bool:
    original = item.original or ""
    translated = item.translated or ""
    if not translated or not translated.strip():
        return False
    if translated == original and not is_acceptable_same_as_source(original, translated):
        return False
    return bool(_CJK_RE.search(translated))


def _text_type(item: TextItem) -> str:
    meta = getattr(item, "meta", {}) or {}
    raw = str(getattr(item, "context", "") or meta.get("kind") or "").lower()
    if raw in {"name", "speaker", "character"}:
        return "name"
    if raw in {"choice", "select", "option"}:
        return "choice"
    if raw in {"ui", "system", "menu", "button", "label"}:
        return "ui/system"
    return "message"


def _translation_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _is_polish_current(item: TextItem) -> bool:
    meta = getattr(item, "meta", {}) or {}
    polish = meta.get("polish") if isinstance(meta.get("polish"), dict) else {}
    return polish.get("version") == POLISH_VERSION and polish.get("hash") == _translation_hash(item.translated or "")


def _mark_polished(item: TextItem) -> None:
    if item.meta is None:
        item.meta = {}
    item.meta["polish"] = {
        "version": POLISH_VERSION,
        "hash": _translation_hash(item.translated or ""),
        "updated_at": int(time.time()),
    }


def _cost(input_tokens: int, output_tokens: int) -> float:
    return cost_cny(input_tokens, output_tokens, "deepseek", MODEL)
