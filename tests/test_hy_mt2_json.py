from __future__ import annotations

import json

import pytest

from translators.deepseek_batch_json import BatchJsonParseError, BatchShapeError
from translators.hy_mt2_json import (
    parse_local_translation_map,
    parse_partial_local_translation_map,
)


def test_local_json_uses_explicit_ids_even_when_reordered_and_fenced():
    raw = '说明文字\n```json\n{"2:message":"第二条","1:message":"第一条"}\n```\n'

    assert parse_local_translation_map(raw, ["1:message", "2:message"]) == {
        1: "第一条",
        2: "第二条",
    }


def test_local_json_accepts_legacy_numeric_ids_without_positional_binding():
    raw = '{"2":"第二条","1":"第一条"}'

    assert parse_local_translation_map(raw, ["1:message", "2:message"]) == {
        1: "第一条",
        2: "第二条",
    }


def test_local_json_rejects_missing_or_duplicate_ids():
    with pytest.raises(BatchShapeError):
        parse_local_translation_map('{"1:message":"第一条"}', ["1:message", "2:message"])

    with pytest.raises(BatchShapeError):
        parse_local_translation_map(
            '{"1:message":"a","1":"b","2:message":"c"}',
            ["1:message", "2:message"],
        )


def test_local_json_recovers_only_complete_pairs_from_truncated_object():
    raw = '{"2:message":"第二条","1:message":"第一条", "3:message":"未完成'

    assert parse_partial_local_translation_map(raw, ["1:message", "2:message", "3:message"]) == {
        1: "第一条",
        2: "第二条",
    }


def test_local_json_does_not_guess_from_invalid_output():
    with pytest.raises(BatchJsonParseError):
        parse_local_translation_map("不是 JSON", ["1:message"])
