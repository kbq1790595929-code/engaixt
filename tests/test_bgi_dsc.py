import random
import struct
from pathlib import Path

from utils.bgi_dsc import (
    clean_bgi_display_translation,
    compress_dsc,
    decode_sjis_tunnel_bytes,
    decompress_dsc,
    extract_bgi_strings,
    get_dsc_key,
    patch_bgi_script,
    SjisTunnelEncoder,
)
from utils.arc20 import build_arc20
from utils.bgi_packfile import build_packfile, parse_packfile
from engines.base import TextItem


def test_dsc_roundtrip_literal_and_lz_samples():
    rng = random.Random(1234)
    samples = [
        b"",
        b"a",
        b"abcdefg" * 32,
        b"\x00\x01\x02" * 300 + b"tail",
        bytes(range(256)) * 8,
        bytes(rng.randrange(256) for _ in range(4096)),
    ]

    for sample in samples:
        compressed = compress_dsc(sample, key=0x12345678)
        assert get_dsc_key(compressed) == 0x12345678
        assert decompress_dsc(compressed) == sample


def test_dsc_roundtrip_without_lz():
    sample = b"literal-only path" * 100
    compressed = compress_dsc(sample, key=0x87654321, use_lz=False)
    assert decompress_dsc(compressed) == sample


def _raw_v1_script(message: str = "テスト本文") -> bytes:
    text_offset = 0x20
    data = bytearray()
    data += struct.pack("<II", 0x0003, text_offset)
    data += struct.pack("<I", 0x0140)
    data += struct.pack("<I", 0x00F4)
    data += b"\x00" * (text_offset - len(data))
    data += message.encode("cp932") + b"\x00"
    return bytes(data)


def _raw_v1_script_many(messages: list[str]) -> bytes:
    code_size = len(messages) * 12 + 4
    text_offset = ((code_size + 0x0F) // 0x10) * 0x10
    offsets: list[int] = []
    pool = bytearray()
    for message in messages:
        offsets.append(text_offset + len(pool))
        pool += message.encode("cp932") + b"\x00"

    data = bytearray()
    for offset in offsets:
        data += struct.pack("<II", 0x0003, offset)
        data += struct.pack("<I", 0x0140)
    data += struct.pack("<I", 0x00F4)
    data += b"\x00" * (text_offset - len(data))
    data += pool
    return bytes(data)


def test_headerless_ethornell_v1_extract_and_patch():
    script = _raw_v1_script()
    refs = extract_bgi_strings(script)
    assert [ref.text for ref in refs] == ["テスト本文"]

    patched, stats = patch_bgi_script(script, {"テスト本文": "試験済み"})
    assert stats.patched == 1
    assert extract_bgi_strings(patched)[0].text == "試験済み"


def test_bgi_display_cleanup_drops_wrapping_full_stop():
    assert clean_bgi_display_translation("这是测试。") == "这是测试"
    assert clean_bgi_display_translation("「这是测试。」") == "「这是测试」"
    assert clean_bgi_display_translation("真的吗？").endswith("?")
    assert clean_bgi_display_translation("……") == "……"
    assert clean_bgi_display_translation("洗澡……！？") == "洗澡……!?"
    assert clean_bgi_display_translation("洗澡。!?") == "洗澡!?"


def test_bgi_packfile_roundtrip():
    archive = build_packfile([("ymc000001", compress_dsc(_raw_v1_script(), key=7))])
    entries = parse_packfile(archive)
    assert len(entries) == 1
    assert entries[0][0] == "ymc000001"
    assert extract_bgi_strings(entries[0][3])[0].text == "テスト本文"


def test_bgi_packfile_allows_full_16_byte_names():
    name = "abcdefghijklmnop"
    archive = build_packfile([(name, b"payload")])
    entries = parse_packfile(archive)
    assert len(entries) == 1
    assert entries[0][0] == name
    assert entries[0][3] == b"payload"


def test_bgi_unpack_reads_pre_tool_original_when_active_arc_is_patched(tmp_path: Path):
    from engines.bgi import BGIEngine

    game_dir = tmp_path / "game"
    workspace = tmp_path / "workspace"
    game_dir.mkdir()
    (game_dir / "BGI.gdb").write_bytes(b"")

    arc = game_dir / "data01110.arc"
    original = "\u3053\u3053\u307e\u3067\u30bf\u30a4\u30df\u30f3\u30b0"
    patched = "\u5225\u30c6\u30ad\u30b9\u30c8"
    (arc.with_suffix(".arc.pre_tool")).write_bytes(
        build_arc20([("pg00_com", compress_dsc(_raw_v1_script(original), key=1))])
    )
    arc.write_bytes(build_arc20([("pg00_com", compress_dsc(_raw_v1_script(patched), key=1))]))

    items = BGIEngine().unpack(game_dir, workspace)

    assert [item.original for item in items] == [original]


def test_bgi_unpack_skips_english_only_scenario_text(tmp_path: Path):
    from engines.bgi import BGIEngine

    game_dir = tmp_path / "game"
    workspace = tmp_path / "workspace"
    game_dir.mkdir()
    (game_dir / "BGI.gdb").write_bytes(b"")

    messages = [f"This is a translated English scenario line number {i:03d}." for i in range(120)]
    (game_dir / "data01110.arc").write_bytes(
        build_arc20([("english_scene", _raw_v1_script_many(messages))])
    )

    items = BGIEngine().unpack(game_dir, workspace)

    assert items == []


def _decoded_arc_entry_texts(archive: bytes, entry_name: str, table: bytes = b"") -> list[str]:
    entries = parse_packfile(archive)
    if not entries:
        from utils.arc20 import parse_arc20

        entries = parse_arc20(archive)
    for name, _offset, _size, data in entries:
        if name != entry_name:
            continue
        script = decompress_dsc(data)
        texts = []
        for ref in extract_bgi_strings(script, require_japanese=False):
            end = script.find(b"\x00", ref.text_offset)
            raw = script[ref.text_offset:end]
            texts.append(decode_sjis_tunnel_bytes(raw, table))
        return texts
    raise AssertionError(f"entry not found: {entry_name}")


def test_bgi_repack_scopes_english_short_strings_to_translated_entry(tmp_path: Path):
    from engines.bgi import BGIEngine

    game_dir = tmp_path / "game"
    workspace = tmp_path / "workspace"
    game_dir.mkdir()
    workspace.mkdir()
    (game_dir / "BGI.gdb").write_bytes(b"")
    arc = game_dir / "data01000.arc"
    arc.write_bytes(
        build_arc20(
            [
                ("scene001", compress_dsc(_raw_v1_script_many(["Stop."]), key=1)),
                ("scrmsg_bp", compress_dsc(_raw_v1_script_many(["Stop."]), key=1)),
            ]
        )
    )

    engine = BGIEngine()
    engine._game_dir = game_dir
    engine.repack(
        [
            TextItem(
                file="data01000.arc::scene001",
                key="0",
                original="Stop.",
                translated="停下!",
                context="message",
                meta={"arc": "data01000.arc", "entry": "scene001"},
            )
        ],
        workspace,
    )

    table = (game_dir / "sjis_ext.bin").read_bytes()
    patched_archive = arc.read_bytes()
    assert _decoded_arc_entry_texts(patched_archive, "scene001", table) == ["停下!"]
    assert _decoded_arc_entry_texts(patched_archive, "scrmsg_bp", table) == ["Stop."]


def test_sjis_tunnel_decode_roundtrip_for_verification():
    encoder = SjisTunnelEncoder()
    raw = encoder.encode("\u6211\u7761\u7740\u7684\u65f6\u673a")

    assert decode_sjis_tunnel_bytes(raw, encoder.mapping_table()) == "\u6211\u7761\u7740\u7684\u65f6\u673a"
