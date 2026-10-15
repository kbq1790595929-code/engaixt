"""Run: python -m tools.rgss_compare --mtool PATH --ruby PATH --archive PATH."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from utils.rgss_archive import read_rgss_archive
from utils.rgss_scripts import extract_rgss_script_literals, is_ruby_translatable
from utils.rgss_events import extract_rgss_event_strings
from utils.text_extract import is_translatable as is_visible_text
from utils.rvdata2 import extract_strings_from_marshal, is_translatable


def save_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def compare_data(reference: dict, archive_path: Path) -> dict:
    archive = read_rgss_archive(archive_path.read_bytes())
    by_name = {entry.name: entry for entry in archive.entries}
    rows = []
    for expected in reference["entries"]:
        entry = by_name.get(expected["name"])
        if entry is None:
            raise ValueError(f"MTool archive entry missing in EngAixt: {expected['name']}")
        row = {"name": entry.name, "payload_equal": hashlib.sha256(entry.data).hexdigest() == expected["sha256"]}
        if "raw_strings" in expected:
            # A common Japanese selector makes differences attributable to the
            # reader, not one tool's UI language or resource filtering settings.
            selected = set(filter(is_translatable, expected["raw_strings"]))
            actual = set(extract_strings_from_marshal(entry.data))
            row.update({"mtool_japanese_unique": len(selected), "engaixt_japanese_unique": len(actual),
                        "missing": sorted(selected - actual), "extra": sorted(actual - selected)})
            reference_messages = {text for text in expected.get("event_message_lines", []) if is_visible_text(text)}
            messages = {item.original for item in extract_rgss_event_strings(entry.data) if item.event_code == 401}
            row["event_messages"] = {"mtool_unique": len(reference_messages), "engaixt_unique": len(messages),
                                     "missing": sorted(reference_messages - messages),
                                     "extra": sorted(messages - reference_messages)}
        rows.append(row)
    return {"entries_equal": len(rows) == len(archive.entries)
            and [e["name"] for e in reference["entries"]] == [e.name for e in archive.entries]
            and all(row["payload_equal"] for row in rows), "files": rows}


def compare_scripts(reference: dict, script_results: list, archive_path: Path) -> dict:
    archive = read_rgss_archive(archive_path.read_bytes())
    entry = next(e for e in archive.entries if Path(e.name).stem.casefold() == "scripts")
    actual = extract_rgss_script_literals(entry.data)
    expected_set = {(row["index"], text) for row in script_results
                    for text in row["strings"] if is_ruby_translatable(text)}
    actual_set = {(row["script_index"], row["source"]) for row in actual}
    expected_counts = Counter((row["index"], text) for row in script_results
                              for text in row["strings"] if is_ruby_translatable(text))
    actual_counts = Counter((row["script_index"], row["source"]) for row in actual)
    return {"mtool_script_blocks": len(reference["scripts"]),
            "mtool_japanese_unique": len(expected_set), "engaixt_japanese_unique": len(actual_set),
            "mtool_occurrences": sum(expected_counts.values()), "engaixt_occurrences": sum(actual_counts.values()),
            "missing": sorted(expected_set - actual_set), "extra": sorted(actual_set - expected_set),
            "occurrences_equal": expected_counts == actual_counts,
            "source_spans": actual}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mtool", type=Path, required=True, help="Existing MTool/Tool directory")
    parser.add_argument("--ruby", type=Path, required=True)
    parser.add_argument("--archive", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("The MTool oracle currently requires Windows NW.js")
    root = Path(__file__).resolve().parents[2]
    output = args.output or root / ".codex_runs" / "rgss-compare" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = output.resolve()
    # Original tool resources must never be written to the public source tree.
    if not output.is_relative_to(root / ".codex_runs"):
        parser.error("--output must be inside the ignored .codex_runs directory")
    output.mkdir(parents=True, exist_ok=False)
    from .mtool_session import MToolSession
    reports = []
    with MToolSession(args.mtool, output) as mtool:
        resource_hashes = mtool.resources()
        save_json(output / "provenance.json", {**mtool.provenance, "resources": resource_hashes,
                  "scope": "Original MTool archive/CLoader/RGSSWorker functions; data-only RGSS classes; no window Hook",
                  "selector": "utils.rvdata2.is_translatable for data; is_ruby_translatable for scripts",
                  "limitations": ["Not native RGSS runtime/display validation", "Not MTool cloud translation",
                                  "Text sets and script occurrence counts are compared; metadata/resource policy is separate"]})
        for index, archive_path in enumerate(args.archive):
            archive_path = archive_path.resolve(strict=True)
            prefix = f"{index:02d}-{archive_path.name}"
            exported = output / f"{prefix}.mtool-data.json"
            result = subprocess.run([str(args.ruby.resolve()), str(Path(__file__).with_name("export_data.rb")),
                                     str(output), str(archive_path), str(exported)],
                                    capture_output=True, timeout=60)
            (output / f"{prefix}.ruby-stderr.log").write_bytes(result.stderr)
            if result.returncode:
                raise RuntimeError(f"MTool Ruby export failed for {archive_path.name}: "
                                   + result.stderr.decode("utf-8", errors="replace"))
            reference = json.loads(exported.read_text(encoding="utf-8"))
            script_results = [dict(index=s["index"], name=s["name"], source_sha256=s["source_sha256"],
                                   strings=mtool.script_strings(s["source"])) for s in reference["scripts"]]
            save_json(output / f"{prefix}.mtool-scripts.json", script_results)
            report = {"input": str(archive_path), "input_sha256": reference["archive_sha256"],
                      "data": compare_data(reference, archive_path),
                      "scripts": compare_scripts(reference, script_results, archive_path)}
            report["equal_under_selector"] = (
                report["data"]["entries_equal"]
                and all(not row.get("missing") and not row.get("extra") for row in report["data"]["files"])
                and all(not row.get("event_messages", {}).get("missing")
                        and not row.get("event_messages", {}).get("extra") for row in report["data"]["files"])
                and not report["scripts"]["missing"] and not report["scripts"]["extra"]
                and report["scripts"]["occurrences_equal"])
            save_json(output / f"{prefix}.comparison.json", report)
            reports.append({"archive": archive_path.name, "sha256": reference["archive_sha256"],
                            "entries": len(reference["entries"]),
                            "data_unique": sum(row.get("mtool_japanese_unique", 0) for row in report["data"]["files"]),
                            "script_blocks": len(reference["scripts"]),
                            "script_unique": report["scripts"]["mtool_japanese_unique"],
                            "script_occurrences": report["scripts"]["mtool_occurrences"],
                            "event_message_unique": sum(row.get("event_messages", {}).get("mtool_unique", 0)
                                                        for row in report["data"]["files"]),
                            "script_occurrences_equal": report["scripts"]["occurrences_equal"],
                            "equal_under_selector": report["equal_under_selector"]})
    summary = {"output": str(output), "samples": reports,
               "all_equal_under_selector": all(r["equal_under_selector"] for r in reports)}
    save_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_equal_under_selector"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
