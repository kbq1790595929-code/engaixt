from __future__ import annotations

import hashlib
import json
from pathlib import Path


def snapshot_custom_database_keys(json_root: Path) -> tuple[int, str]:
    path = _custom_database_path(json_root)
    if path is None:
        return 0, ""
    data = json.loads(path.read_text(encoding="utf-8"))
    records: list[tuple[object, ...]] = []
    for type_index, database_type in enumerate(data.get("types", [])):
        if not isinstance(database_type, dict):
            continue
        for record_index, record in enumerate(database_type.get("data", [])):
            if not isinstance(record, dict) or not isinstance(record.get("name"), str):
                continue
            record_name = record["name"]
            records.append(("record", type_index, record_index, record_name))
            for field_index, field in enumerate(record.get("data", [])):
                if (
                    isinstance(field, dict)
                    and bool(record_name)
                    and isinstance(field.get("value"), str)
                    and field["value"] == record_name
                ):
                    records.append(("alias", type_index, record_index, field_index, field["value"]))
    payload = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return len(records), hashlib.sha256(payload).hexdigest()


def verify_custom_database_keys(
    json_root: Path,
    expected_count: int,
    expected_fingerprint: str,
) -> dict[str, object]:
    count, fingerprint = snapshot_custom_database_keys(json_root)
    if count != expected_count or fingerprint != expected_fingerprint:
        raise RuntimeError(
            "WOLF CDataBase runtime keys changed during translation; refusing to deploy the archive"
        )
    return {
        "checked": bool(expected_fingerprint),
        "count": count,
        "fingerprint_match": True,
    }


def _custom_database_path(json_root: Path) -> Path | None:
    database_root = json_root / "databases"
    if not database_root.is_dir():
        return None
    for path in database_root.iterdir():
        if path.is_file() and path.name.casefold() == "cdatabase.json":
            return path
    return None
