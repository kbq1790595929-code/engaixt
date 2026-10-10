from __future__ import annotations

import struct

from utils.bgi_dsc import extract_bgi_strings, patch_bgi_script


def _raw_v1_choice_script(choices: list[str]) -> bytes:
    code = bytearray()
    string_data = bytearray()
    offsets: list[int] = []
    cursor = 0
    for text in choices:
        offsets.append(cursor)
        raw = text.encode("cp932") + b"\x00"
        string_data += raw
        cursor += len(raw)

    text_offset = 0x40
    for off in offsets:
        code += struct.pack("<II", 0x0003, text_offset + off)
    code += struct.pack("<I", 0x0160)
    code += struct.pack("<I", 0x00F4)
    code += b"\x00" * (text_offset - len(code))
    code += string_data
    return bytes(code)


def test_headerless_ethornell_v1_extracts_choices():
    script = _raw_v1_choice_script(["\u884c\u304f", "\u884c\u304b\u306a\u3044"])

    refs = extract_bgi_strings(script)

    assert [(ref.text, ref.kind) for ref in refs] == [
        ("\u884c\u304f", "choice"),
        ("\u884c\u304b\u306a\u3044", "choice"),
    ]


def test_headerless_ethornell_v1_patches_choices():
    script = _raw_v1_choice_script(["\u884c\u304f", "\u884c\u304b\u306a\u3044"])

    patched, stats = patch_bgi_script(script, {"\u884c\u304f": "\u53bb", "\u884c\u304b\u306a\u3044": "\u4e0d\u53bb"})

    assert stats.patched == 2
    assert [ref.text for ref in extract_bgi_strings(patched)] == ["\u53bb", "\u4e0d\u53bb"]
