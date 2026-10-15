from utils.rgss_scripts import (
    extract_ruby_string_literals, extract_rgss_script_literals,
    replace_rgss_script_literals, _write_long,
)
import os
import shutil
import subprocess
import zlib

import pytest


def test_interpolation_exports_mtool_compatible_text_fragments():
    source = 'print("\u30d5\u30a1\u30a4\u30eb #{filename} \u304c\u898b\u3064\u304b\u308a\u307e\u305b\u3093\u3002")'
    literals = extract_ruby_string_literals(source)
    assert [item.original for item in literals] == [
        '\u30d5\u30a1\u30a4\u30eb ', ' \u304c\u898b\u3064\u304b\u308a\u307e\u305b\u3093\u3002',
    ]
    assert all(item.content_only for item in literals)


def test_interpolation_rewrite_preserves_ruby_expression_and_escapes_text():
    source = 'print("\u30d5\u30a1\u30a4\u30eb #{filename} \u304c\u898b\u3064\u304b\u308a\u307e\u305b\u3093\u3002")'
    compressed = zlib.compress(source.encode('utf-8'))
    data = b'\x04\x08[\x06[\x08i\x06"\x09Main"' + _write_long(len(compressed)) + compressed
    items = extract_rgss_script_literals(data)
    updates = [dict(item, original=item['source'], translated=translated)
               for item, translated in zip(items, ['\u6587\u4ef6 ', ' \u672a\u627e\u5230\u3002'])]
    output = replace_rgss_script_literals(data, updates)
    from utils.rgss_scripts import parse_marshal, _iter_scripts
    script = next(_iter_scripts(parse_marshal(output), output))
    assert script.source_text == 'print("\u6587\u4ef6 #{filename} \u672a\u627e\u5230\u3002")'
    assert replace_rgss_script_literals(data, []) == data


def test_interpolation_rewrite_is_valid_for_independent_ruby_parser(tmp_path):
    ruby = os.environ.get("ENGAIXT_TEST_RUBY") or shutil.which("ruby")
    if not ruby:
        pytest.skip("independent Ruby parser is not installed on this test host")
    source = 'print("\u30d5\u30a1\u30a4\u30eb #{filename} \u304c\u898b\u3064\u304b\u308a\u307e\u305b\u3093\u3002")'
    compressed = zlib.compress(source.encode('utf-8'))
    data = b'\x04\x08[\x06[\x08i\x06"\x09Main"' + _write_long(len(compressed)) + compressed
    items = extract_rgss_script_literals(data)
    updates = [dict(item, original=item['source'], translated=text)
               for item, text in zip(items, ['\u6587\u4ef6 "#{literal}" ', ' \u672a\u627e\u5230\u3002'])]
    output = replace_rgss_script_literals(data, updates)
    path = tmp_path / 'Scripts.rvdata2'
    path.write_bytes(output)
    program = "require 'zlib';require 'ripper';s=Zlib::Inflate.inflate(Marshal.load(File.binread(ARGV[0]))[0][2]);exit(Ripper.sexp(s.force_encoding('UTF-8')) ? 0 : 1)"
    result = subprocess.run([ruby, '-e', program, str(path)], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
