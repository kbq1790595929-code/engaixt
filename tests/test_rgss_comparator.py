from tools.rgss_compare.__main__ import compare_scripts


def test_comparator_detects_script_occurrence_difference(tmp_path):
    import zlib
    from utils.rgss_archive import RGSSArchive, RGSSEntry, rebuild_rgss_archive
    from utils.rgss_scripts import _write_long

    text = '\u9078\u629e\u3057\u3066\u304f\u3060\u3055\u3044'
    source = f'puts "{text}"; puts "{text}"'.encode('utf-8')
    compressed = zlib.compress(source)
    data = b'\x04\x08[\x06[\x08i\x06"\x09Main"' + _write_long(len(compressed)) + compressed
    archive = RGSSArchive(1, (RGSSEntry('Data/Scripts.rvdata', b'Data\\Scripts.rvdata', data),))
    path = tmp_path / 'Game.rgss2a'
    path.write_bytes(rebuild_rgss_archive(archive, {}))
    reference = {'scripts': [{'index': 0}]}
    result = compare_scripts(reference, [{'index': 0, 'strings': [text]}], path)
    assert result['missing'] == result['extra'] == []
    assert not result['occurrences_equal']
    assert result['mtool_occurrences'] == 1
    assert result['engaixt_occurrences'] == 2
