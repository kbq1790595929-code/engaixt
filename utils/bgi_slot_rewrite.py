"""AI-assisted fixed-slot rewrites for BGI script strings."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from config import get_config
from translators.base import retry_with_backoff
from utils.arc20 import parse_arc20
from utils.bgi_dsc import (
    BGI_V1_MAGIC,
    SjisTunnelEncoder,
    clean_bgi_display_translation,
    decompress_dsc,
    inspect_bgi_v1_script,
)
from utils.fixed_slot import clean_translation_text, fit_fixed_slot
from utils.logger import info, warning


_PH_RE = re.compile(
    r"(%[0-9.]*[A-Za-z]|\\[A-Za-z]+\[[^\]]+\]|\\[A-Za-z]|<[^>]+>|\[[A-Za-z0-9_./:#=-]+\]|\{[^}]+\})"
)
_KANA_RE = re.compile(r"[\u3040-\u30ff\uff66-\uff9f]")


@dataclass
class BgiSlotRewriteCandidate:
    original: str
    current: str
    slot_bytes: int
    current_bytes: int
    deterministic: str | None = None
    deterministic_bytes: int | None = None
    count: int = 0
    examples: list[dict[str, object]] = field(default_factory=list)


def collect_bgi_slot_rewrite_candidates(
    arc_paths: Iterable[Path],
    game_dir: Path,
    translations: dict[str, str],
    table: bytes = b"",
    arc_filters: list[str] | None = None,
) -> list[BgiSlotRewriteCandidate]:
    """Find unique translated strings that exceed their fixed BGI slots."""
    filters = [item.lower() for item in (arc_filters or [])]
    candidates: dict[tuple[str, str, int], BgiSlotRewriteCandidate] = {}
    encoder = SjisTunnelEncoder(table)

    for arc_path in arc_paths:
        rel = arc_path.relative_to(game_dir)
        rel_text = rel.as_posix().lower()
        if filters and not any(f in rel_text or f in arc_path.name.lower() for f in filters):
            continue
        try:
            entries = parse_arc20(arc_path.read_bytes())
        except Exception:
            continue
        for entry_name, _offset, _size, data in entries:
            script = decompress_dsc(data)
            if not script.startswith(BGI_V1_MAGIC):
                continue
            script_info = inspect_bgi_v1_script(
                script,
                include_internal=False,
                require_japanese=False,
                include_empty=True,
            )
            if not script_info:
                continue
            for ref in script_info.refs:
                translated = translations.get(ref.text)
                if not translated or translated == ref.text:
                    continue
                translated = clean_bgi_display_translation(translated)
                if not translated or translated == ref.text:
                    continue
                end = script.find(b"\x00", ref.text_offset)
                if end < 0:
                    continue
                slot_bytes = end - ref.text_offset
                current_bytes = len(encoder.encode(translated))
                if current_bytes <= slot_bytes:
                    continue

                deterministic = fit_fixed_slot(
                    translated,
                    slot_bytes,
                    encoder.encode,
                )
                deterministic_bytes = len(encoder.encode(deterministic)) if deterministic else None
                key = (ref.text, translated, slot_bytes)
                item = candidates.get(key)
                if item is None:
                    item = BgiSlotRewriteCandidate(
                        original=ref.text,
                        current=translated,
                        slot_bytes=slot_bytes,
                        current_bytes=current_bytes,
                        deterministic=deterministic,
                        deterministic_bytes=deterministic_bytes,
                    )
                    candidates[key] = item
                item.count += 1
                if len(item.examples) < 3:
                    item.examples.append(
                        {
                            "arc": rel.as_posix(),
                            "entry": entry_name,
                            "kind": ref.kind,
                            "text_offset": ref.text_offset,
                        }
                    )

    return list(candidates.values())


def build_bgi_tunnel_table_for_translations(
    arc_paths: Iterable[Path],
    game_dir: Path,
    translations: dict[str, str],
    arc_filters: list[str] | None = None,
) -> bytes:
    """Return the tunnel table produced by encoding all strings patching may use."""
    filters = [item.lower() for item in (arc_filters or [])]
    encoder = SjisTunnelEncoder()
    for arc_path in arc_paths:
        rel = arc_path.relative_to(game_dir)
        rel_text = rel.as_posix().lower()
        if filters and not any(f in rel_text or f in arc_path.name.lower() for f in filters):
            continue
        try:
            entries = parse_arc20(arc_path.read_bytes())
        except Exception:
            continue
        for _entry_name, _offset, _size, data in entries:
            script = decompress_dsc(data)
            if not script.startswith(BGI_V1_MAGIC):
                continue
            script_info = inspect_bgi_v1_script(
                script,
                include_internal=False,
                require_japanese=False,
                include_empty=True,
            )
            if not script_info:
                continue
            for ref in script_info.refs:
                translated = translations.get(ref.text)
                if not translated or translated == ref.text:
                    continue
                translated = clean_bgi_display_translation(translated)
                if translated and translated != ref.text:
                    encoder.encode(translated)
    return encoder.mapping_table()


def rewrite_bgi_slots_sync(
    candidates: list[BgiSlotRewriteCandidate],
    table: bytes = b"",
    cache_path: Path | None = None,
    limit: int = 0,
    concurrency: int | None = None,
    model: str = "deepseek-chat",
) -> tuple[dict[tuple[str, int], str], dict[str, object]]:
    """Run AI slot rewrites from synchronous engine/script code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            rewrite_bgi_slots(
                candidates,
                table=table,
                cache_path=cache_path,
                limit=limit,
                concurrency=concurrency,
                model=model,
            )
        )

    box: dict[str, object] = {}

    def runner() -> None:
        box["value"] = asyncio.run(
            rewrite_bgi_slots(
                candidates,
                table=table,
                cache_path=cache_path,
                limit=limit,
                concurrency=concurrency,
                model=model,
            )
        )

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    return box["value"]  # type: ignore[return-value]


async def rewrite_bgi_slots(
    candidates: list[BgiSlotRewriteCandidate],
    table: bytes = b"",
    cache_path: Path | None = None,
    limit: int = 0,
    concurrency: int | None = None,
    model: str = "deepseek-chat",
) -> tuple[dict[tuple[str, int], str], dict[str, object]]:
    """Ask AI for shorter Chinese strings, accepting only byte-valid results."""
    cfg = get_config()
    cache_path = cache_path or _default_cache_path()
    cache = _load_cache(cache_path)
    selected = _prioritize_candidates(candidates)[: limit or None]
    rewrites: dict[tuple[str, int], str] = {}
    handled: set[tuple[str, int]] = set()
    report_items: list[dict[str, object]] = []
    rejected_items: list[dict[str, object]] = []
    stats = {
        "candidates": len(candidates),
        "selected": len(selected),
        "cache_hits": 0,
        "api_requested": 0,
        "accepted": 0,
        "applied": 0,
        "same_as_rule": 0,
        "rejected": 0,
        "no_key": 0,
    }

    for candidate in selected:
        cached = _cached_valid(cache, candidate, table)
        if cached:
            handled.add((candidate.original, candidate.slot_bytes))
            stats["cache_hits"] += 1
            if cached != candidate.deterministic:
                rewrites[(candidate.original, candidate.slot_bytes)] = cached
                stats["applied"] += 1
                report_items.append(_report_item(candidate, cached, "cache", table))
            else:
                stats["same_as_rule"] += 1

    pending = [
        candidate for candidate in selected
        if (candidate.original, candidate.slot_bytes) not in handled
    ]
    api_key = cfg.deepseek_api_key or cfg.openai_api_key
    if not pending:
        report = {**stats, "items": report_items}
        _save_cache(cache_path, cache)
        return rewrites, report
    if not api_key:
        stats["no_key"] = len(pending)
        warning(f"BGI AI slot rewrite skipped: no DeepSeek/OpenAI API key ({len(pending)} pending)")
        report = {**stats, "items": report_items}
        _save_cache(cache_path, cache)
        return rewrites, report

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
    sem = asyncio.Semaphore(max(1, concurrency or cfg.max_concurrency or 1))
    completed = 0
    dirty = False

    async def run_one(candidate: BgiSlotRewriteCandidate) -> None:
        nonlocal completed, dirty
        async with sem:
            result, rejected = await _rewrite_one(client, candidate, table, model)
            if rejected and len(rejected_items) < 100:
                rejected_items.append(
                    {
                        "original": candidate.original,
                        "current": candidate.current,
                        "slot_bytes": candidate.slot_bytes,
                        "deterministic": candidate.deterministic,
                        "attempts": rejected,
                    }
                )
            stats["api_requested"] += 1
            completed += 1
            key = _cache_key(candidate)
            if result:
                handled.add((candidate.original, candidate.slot_bytes))
                cache["items"][key] = {
                    "original": candidate.original,
                    "current": candidate.current,
                    "slot_bytes": candidate.slot_bytes,
                    "result": result,
                    "result_bytes": _encoded_len(result, table),
                    "model": model,
                }
                stats["accepted"] += 1
                if result != candidate.deterministic:
                    rewrites[(candidate.original, candidate.slot_bytes)] = result
                    stats["applied"] += 1
                    report_items.append(_report_item(candidate, result, "api", table))
                else:
                    stats["same_as_rule"] += 1
                dirty = True
            else:
                stats["rejected"] += 1
            if completed % 25 == 0 or completed == len(pending):
                info(
                    "BGI AI slot rewrite: "
                    f"{completed}/{len(pending)} requested, applied={stats['applied']}, "
                    f"cache={stats['cache_hits']}"
                )
                if dirty:
                    _save_cache(cache_path, cache)
                    dirty = False

    await asyncio.gather(*(run_one(candidate) for candidate in pending))
    _save_cache(cache_path, cache)
    report = {**stats, "items": report_items, "rejected_items": rejected_items}
    return rewrites, report


async def _rewrite_one(
    client,
    candidate: BgiSlotRewriteCandidate,
    table: bytes,
    model: str,
) -> tuple[str | None, list[dict[str, str]]]:
    feedback = ""
    rejected: list[dict[str, str]] = []
    for _attempt in range(3):
        prompt = _prompt(candidate, feedback)

        async def call() -> str:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=160,
                timeout=30,
            )
            return (resp.choices[0].message.content or "").strip()

        try:
            raw = await retry_with_backoff(call)
        except Exception as exc:
            warning(f"BGI AI slot rewrite failed [{candidate.original[:30]}...]: {exc}")
            rejected.append({"raw": "", "output": "", "reason": str(exc)})
            return None, rejected
        result = _clean_ai_result(raw)
        ok, reason = _validate_result(candidate, result, table)
        if ok:
            return result, rejected
        rejected.append({"raw": raw, "output": result, "reason": reason})
        feedback = f"Previous output rejected: {reason}. Output a shorter valid Chinese rewrite only."
    return None, rejected


def _prioritize_candidates(candidates: list[BgiSlotRewriteCandidate]) -> list[BgiSlotRewriteCandidate]:
    def key(candidate: BgiSlotRewriteCandidate) -> tuple[int, int, int, int, int]:
        deterministic_bytes = candidate.deterministic_bytes or 0
        unused_budget = max(0, candidate.slot_bytes - deterministic_bytes)
        over_budget = max(0, candidate.current_bytes - candidate.slot_bytes)
        tiny_penalty = 1 if candidate.slot_bytes < 16 else 0
        return (
            tiny_penalty,
            -unused_budget,
            -over_budget,
            -candidate.slot_bytes,
            -candidate.count,
        )

    return sorted(candidates, key=key)


def _prompt(candidate: BgiSlotRewriteCandidate, feedback: str = "") -> str:
    max_cjk = max(1, candidate.slot_bytes // 2)
    fallback = candidate.deterministic or ""
    return (
        "你是游戏汉化润色器。请把下面的日文游戏文本改写成极短、自然、可读的简体中文。\n"
        "硬性限制：输出写入 CP932/SJIS 固定槽位后不能超字节。\n"
        f"字节上限：{candidate.slot_bytes} bytes。多数中文约 2 bytes，ASCII 数字/字母约 1 byte。\n"
        f"建议长度：最多约 {max_cjk} 个汉字；宁可短，也绝不能超限。\n"
        "只输出译文本身，不要解释、不要编号、不要“译文：”、不要外层引号。\n"
        "如果槽位很小，请输出最短有意义称呼/标签；如果是句子，请尽量保留核心信息。\n"
        "若有 %s、\\n、<tag>、[tag]、{tag} 等占位符/控制码，必须原样保留。\n"
        f"{feedback}\n\n"
        f"日文原文：{candidate.original}\n"
        f"当前中文：{candidate.current}\n"
        f"规则短译参考：{fallback}\n\n"
        "极短中文："
    )


def _clean_ai_result(text: str) -> str:
    text = text.strip().strip("`").strip()
    text = re.sub(r"^```[a-zA-Z0-9_-]*", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) == 1:
        text = lines[0]
    text = re.sub(r"^\s*\d+[\.\)、)]\s*", "", text)
    text = re.sub(r"^\s*(?:短译|译文|翻译|输出|答案)\s*[：:]\s*", "", text)
    text = clean_translation_text(text)
    return text.strip().strip('"').strip("'").strip()


def _validate_result(candidate: BgiSlotRewriteCandidate, result: str, table: bytes) -> tuple[bool, str]:
    if not result:
        return False, "empty"
    if _KANA_RE.search(result):
        return False, "contains Japanese kana"
    if _PH_RE.findall(candidate.current) != _PH_RE.findall(result):
        return False, "placeholder mismatch"
    byte_len = _encoded_len(result, table)
    if byte_len > candidate.slot_bytes:
        return False, f"too long: {byte_len}>{candidate.slot_bytes}"
    return True, ""


def _cached_valid(cache: dict[str, object], candidate: BgiSlotRewriteCandidate, table: bytes) -> str | None:
    items = cache.setdefault("items", {})
    if not isinstance(items, dict):
        cache["items"] = {}
        return None
    row = items.get(_cache_key(candidate))
    if not isinstance(row, dict):
        return None
    result = str(row.get("result") or "")
    ok, _reason = _validate_result(candidate, result, table)
    return result if ok else None


def _cache_key(candidate: BgiSlotRewriteCandidate) -> str:
    payload = json.dumps(
        {
            "original": candidate.original,
            "current": candidate.current,
            "slot_bytes": candidate.slot_bytes,
            "version": 1,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_cache(path: Path) -> dict[str, object]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("version", 1)
                data.setdefault("items", {})
                return data
        except Exception:
            pass
    return {"version": 1, "items": {}}


def _save_cache(path: Path, cache: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _default_cache_path() -> Path:
    return Path.home() / "Downloads" / ".game_translator" / "bgi_slot_ai_cache.json"


def _encoded_len(text: str | None, table: bytes) -> int:
    if not text:
        return 0
    return len(SjisTunnelEncoder(table).encode(text))


def _report_item(
    candidate: BgiSlotRewriteCandidate,
    result: str,
    source: str,
    table: bytes,
) -> dict[str, object]:
    return {
        "source": source,
        "original": candidate.original,
        "current": candidate.current,
        "slot_bytes": candidate.slot_bytes,
        "current_bytes": candidate.current_bytes,
        "deterministic": candidate.deterministic,
        "deterministic_bytes": candidate.deterministic_bytes,
        "ai": result,
        "ai_bytes": _encoded_len(result, table),
        "count": candidate.count,
        "examples": candidate.examples,
    }
