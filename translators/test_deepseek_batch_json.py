"""Standalone regression checks for supported OpenAI-compatible response shapes."""
import pytest

from translators.deepseek_batch_json import BatchShapeError, parse_batch_json


@pytest.mark.parametrize("raw", [
    '{"1":"你好","2":"再见"}',
    '{"t":{"1":"你好","2":"再见"}}',
    '{"type":"json_object","1":"你好","2":"再见"}',
    '{"type":"json_object","content":{"1":"你好","2":"再见"}}',
    '{"type":"json_object","t":{"1":"你好","2":"再见"}}',
])
def test_complete_numeric_translation_maps(raw):
    assert parse_batch_json(raw, [1, 2]) == {1: "你好", 2: "再见"}


def test_flat_numeric_map_must_match_expected_ids_exactly():
    with pytest.raises(BatchShapeError, match="id set mismatch"):
        parse_batch_json('{"1":"你好","3":"多余"}', [1, 2])


def test_unrelated_provider_envelope_is_not_accepted_as_translation():
    with pytest.raises(BatchShapeError):
        parse_batch_json('{"type":"json_object"}', [1])
