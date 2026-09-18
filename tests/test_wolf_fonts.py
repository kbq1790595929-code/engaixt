from __future__ import annotations

from pathlib import Path

from core.open_source_fonts import bundled_source_han_sans_path
from engines.wolf.fonts import (
    _ALIAS_NAME_IDS,
    _NameRecord,
    _Table,
    _build_name_table,
    _build_sfnt,
    _checksum,
    _parse_name_table,
    _parse_sfnt,
    _table_data,
    build_sfnt_name_alias,
    find_wolf_font_targets,
)


def test_wolf_font_alias_keeps_requested_family_and_valid_checksum():
    source = bundled_source_han_sans_path().read_bytes()
    version, tables = _parse_sfnt(source)
    original_records = _parse_name_table(_table_data(tables, b"name"))
    alias_records = [
        _NameRecord(
            record.platform,
            record.encoding,
            record.language,
            record.name_id,
            "Wolf Test Font" if record.name_id in _ALIAS_NAME_IDS else record.text,
        )
        for record in original_records
    ]
    alias_name = _build_name_table(alias_records)
    alias_font = _build_sfnt(
        version,
        [_Table(table.tag, alias_name if table.tag == b"name" else table.data) for table in tables],
    )

    result = build_sfnt_name_alias(source, alias_font)
    _result_version, result_tables = _parse_sfnt(result)
    result_names = _parse_name_table(_table_data(result_tables, b"name"))

    assert _checksum(result) == 0xB1B0AFBA
    assert any(record.name_id == 1 and record.text == "Wolf Test Font" for record in result_names)
    assert len(result) > 500_000


def test_wolf_font_targets_are_shallow_and_ignore_small_or_nested_fonts(tmp_path: Path):
    data = tmp_path / "Data"
    nested = data / "Picture"
    nested.mkdir(parents=True)
    root_font = tmp_path / "dialog.ttf"
    data_font = data / "system.otf"
    root_font.write_bytes(b"x" * 500_000)
    data_font.write_bytes(b"y" * 600_000)
    (tmp_path / "tiny.ttf").write_bytes(b"small")
    (nested / "icon.ttf").write_bytes(b"z" * 700_000)

    assert find_wolf_font_targets(tmp_path) == sorted(
        [root_font, data_font], key=lambda path: str(path).casefold()
    )
