import json
from pathlib import Path

import pytest

from core.pipeline_runtime_stage import verify_repack_outputs
from engines.base import TextItem
from engines.wolf.engine import WolfEngine
from engines.wolf.json_text import apply_translations, extract_text_items
from engines.wolf.runtime_keys import (
    snapshot_custom_database_keys,
    verify_custom_database_keys,
)


RUNTIME_NAME = "\u4e3b\uff71\uff78\uff7c\uff6e\uff9d\uff11"


def _write_custom_database(root: Path, alias: str = RUNTIME_NAME) -> Path:
    database_root = root / "databases"
    database_root.mkdir(parents=True)
    path = database_root / "CDataBase.json"
    path.write_text(
        json.dumps(
            {
                "types": [
                    {
                        "data": [
                            {
                                "name": RUNTIME_NAME,
                                "data": [
                                    {"name": "", "value": alias},
                                    {"name": "description", "value": "\u8868\u793a\u6587\u672c"},
                                ],
                            }
                        ]
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_wolf_database_record_alias_is_not_extracted_for_translation(tmp_path: Path):
    json_root = tmp_path / "json"
    _write_custom_database(json_root)
    stats: dict[str, int] = {}

    items = extract_text_items(
        json_root,
        lambda _rel: ("Data.wolf", "Data/BasicData/CDataBase.dat"),
        stats,
    )

    assert [item.original for item in items] == ["\u8868\u793a\u6587\u672c"]
    assert stats["database_record_name"] == 1
    assert stats["database_record_alias"] == 1


def test_wolf_json_patch_ignores_legacy_database_record_alias_translation(tmp_path: Path):
    json_root = tmp_path / "json"
    path = _write_custom_database(json_root)
    legacy_item = TextItem(
        file="Data.wolf",
        key="/types/0/data/0/data/0/value",
        original=RUNTIME_NAME,
        translated="\u4e3b\u884c\u52a81",
        meta={
            "wolf_json": "databases/CDataBase.json",
            "wolf_pointer": "/types/0/data/0/data/0/value",
            "wolf_role": "database_value",
        },
    )

    assert apply_translations(json_root, [legacy_item]) == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["types"][0]["data"][0]["data"][0]["value"] == RUNTIME_NAME


def test_wolf_runtime_key_fingerprint_includes_record_aliases(tmp_path: Path):
    original_root = tmp_path / "original"
    changed_root = tmp_path / "changed"
    _write_custom_database(original_root)
    _write_custom_database(changed_root, alias="\u4e3b\u884c\u52a81")
    count, fingerprint = snapshot_custom_database_keys(original_root)

    assert count == 2
    with pytest.raises(RuntimeError, match="runtime keys changed"):
        verify_custom_database_keys(changed_root, count, fingerprint)


def test_wolf_repack_filter_drops_explicit_database_runtime_key():
    engine = WolfEngine()
    runtime_key = TextItem(
        file="Data.wolf",
        key="/types/0/data/0/data/0/value",
        original=RUNTIME_NAME,
        translated="\u4e3b\u884c\u52a81",
        meta={"wolf_role": "database_runtime_key"},
    )
    dialogue = TextItem(
        file="Data.wolf",
        key="/commands/0/stringArgs/0",
        original="\u3053\u3093\u306b\u3061\u306f",
        translated="\u4f60\u597d",
        meta={"wolf_role": "command"},
    )

    assert engine.filter_repack_items([runtime_key, dialogue]) == [dialogue]


def test_wolf_repack_verification_captures_final_engine_diagnostics(tmp_path: Path):
    engine = WolfEngine()
    engine._last_repack_verification = {
        "bridge_verified": True,
        "archive_roundtrip_verified": True,
        "outputs": ["Data/BasicData.wolf"],
        "applied_translations": 1,
    }
    engine._diagnostics = {
        "stage": "repack",
        "text_safety": {"checked": 1, "changed": 1},
        "fonts": {"deployed": 2},
    }
    item = TextItem(file="Data/BasicData.wolf", original="こんにちは", translated="你好")

    result = verify_repack_outputs(object(), tmp_path, [item], engine)

    assert result["checked"] is True
    assert result["archive_roundtrip_verified"] is True
    assert result["engine_diagnostics"] == engine._diagnostics
