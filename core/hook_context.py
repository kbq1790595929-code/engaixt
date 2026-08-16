"""RPG Maker Runtime Hook — Python-side translation pipeline (v3).

Handles:
  1. Load v3 scan format: [safe, text, context, count, types]
  2. Deduplicate by safe_text (type-protected)
  3. Translate via DeepSeek
  4. Validate structural constraints: control code types/counts preserved
  5. Build {safe: translated_safe} map for JS replace mode
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def load_collected(hook_path: str) -> list[dict]:
    """Load hook_text.json. Supports legacy formats."""
    data = json.loads(Path(hook_path).read_text(encoding="utf-8"))

    if isinstance(data, dict):
        return [{"safe": k, "text": k, "context": "unknown", "count": v, "types": []}
                for k, v in data.items()]

    if isinstance(data, list) and len(data) > 0:
        first = data[0]
        if isinstance(first, list):
            if len(first) >= 5:  # v3: [safe, text, context, count, types]
                return [{"safe": it[0], "text": it[1], "context": it[2],
                         "count": it[3], "types": it[4]} for it in data]
            if len(first) >= 4:  # v2: [safe, text, context, count]
                return [{"safe": it[0], "text": it[1], "context": it[2],
                         "count": it[3], "types": []} for it in data]
            return [{"safe": it[0], "text": it[1], "context": "unknown",
                     "count": 1, "types": []} for it in data]

    return []


def build_unique(items: list[dict]) -> list[dict]:
    """Deduplicate by safe_text (already type-protected). No filtering."""
    seen: dict[str, dict] = {}
    for it in items:
        safe = it["safe"]
        if safe in seen:
            seen[safe]["count"] += it.get("count", 1)
        else:
            seen[safe] = it
    return list(seen.values())


# ---- 4. Structural constraint validation ----

def _extract_ctrl_types(safe_text: str) -> dict[str, int]:
    """Count control tokens by type: {'COLOR': 2, 'WAIT': 1, ...}"""
    tokens = re.findall(r"__([A-Z][A-Z0-9]*(?:_[a-zA-Z0-9]+)?)__", safe_text)
    counts: dict[str, int] = {}
    for t in tokens:
        # Strip numeric suffix: COLOR_5 → COLOR
        base = re.sub(r"_\d+$", "", t)
        counts[base] = counts.get(base, 0) + 1
    return counts


def validate_translation(orig_safe: str, trans_safe: str) -> tuple[bool, str]:
    """Check that control code types and counts are preserved after translation.
    Returns (ok, reason)."""
    orig_types = _extract_ctrl_types(orig_safe)
    if not orig_types:
        return True, ""

    trans_types = _extract_ctrl_types(trans_safe)

    # Check for missing types
    for typ, count in orig_types.items():
        trans_count = trans_types.get(typ, 0)
        if trans_count < count:
            return False, f"缺少控制符 {typ}: 期望{count} 实际{trans_count}"

    # Check for extra types (AI hallucinated new control codes)
    for typ, count in trans_types.items():
        if typ not in orig_types:
            return False, f"AI 产生了不存在的控制符 {typ}"

    return True, ""


def build_translation_map(pairs: list[dict], translated_items: list) -> dict[str, str]:
    """Build {safe: translated_safe} map.
    Structural constraint validation happens in verify_translation()
    during translate_one() — failed items trigger AI retry automatically.
    Items that still fail after retry fall back to original text."""
    trans_map: dict[str, str] = {}
    for it in translated_items:
        if it.translated and it.translated != it.original:
            trans_map[it.key] = it.translated
    return trans_map
