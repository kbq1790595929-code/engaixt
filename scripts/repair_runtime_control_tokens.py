"""Repair malformed RPG Maker runtime translation-map control tokens.

Use this only for already-translated games created before CJK speaker tags and
empty braces were protected by the shared text pipeline.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from pathlib import Path


_LEADING_SPEAKER_RE = re.compile(r"^(?P<prefix>(?:\\n|\n)?<[^<>\r\n]{1,256}>)")
_LEADING_ARTIFACT_RE = re.compile(r"^(?:(?:\{\})?(?:\\n|\n)|\{\})+")
_LEADING_ANGLE_RE = re.compile(r"^<[^<>\r\n]{1,256}>")


def repair_runtime_map(mapping: Mapping[str, str]) -> tuple[dict[str, str], dict[str, int]]:
    """Restore leading speaker controls and discard injected empty braces."""
    repaired: dict[str, str] = {}
    stats = {"checked": 0, "speaker_restored": 0, "extra_braces_removed": 0, "changed": 0}

    for source, translated in mapping.items():
        if not isinstance(source, str) or not isinstance(translated, str):
            repaired[source] = translated
            continue

        stats["checked"] += 1
        candidate = translated
        leading = _LEADING_SPEAKER_RE.match(source)
        if leading:
            prefix = leading.group("prefix")
            body = _LEADING_ARTIFACT_RE.sub("", candidate)
            body = _LEADING_ANGLE_RE.sub("", body)
            candidate = prefix + body
            if candidate != translated:
                stats["speaker_restored"] += 1

        expected_braces = source.count("{}")
        extra_braces = candidate.count("{}") - expected_braces
        if extra_braces > 0:
            candidate = candidate.replace("{}", "", extra_braces)
            stats["extra_braces_removed"] += extra_braces

        if candidate != translated:
            stats["changed"] += 1
        repaired[source] = candidate

    return repaired, stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("map", type=Path, help="save/hook_translation_map.json")
    parser.add_argument("--apply", action="store_true", help="write repaired JSON in place")
    args = parser.parse_args()

    data = json.loads(args.map.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise SystemExit("runtime translation map must be a JSON object")

    repaired, stats = repair_runtime_map(data)
    print(json.dumps(stats, ensure_ascii=False))
    if args.apply:
        args.map.write_text(json.dumps(repaired, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
