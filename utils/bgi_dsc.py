"""BGI/Ethornell DSC and script text helpers."""
from __future__ import annotations

from collections import Counter, deque
from heapq import heappop, heappush
import re
import struct
from dataclasses import dataclass, field

from utils.fixed_slot import clean_translation_text, fit_fixed_slot


DSC_MAGIC = b"DSC FORMAT 1.00\x00"
BGI_V1_MAGIC = b"BurikoCompiledScriptVer1.00\x00"


@dataclass(frozen=True)
class BgiStringRef:
    operand_offset: int
    text_offset: int
    text: str
    kind: str


@dataclass(frozen=True)
class BgiScriptInfo:
    code_offset: int
    code_end: int
    refs: list[BgiStringRef]


@dataclass
class BgiPatchStats:
    patched: int = 0
    direct: int = 0
    compressed: int = 0
    ai_rewritten: int = 0
    appended: int = 0
    skipped_too_long: int = 0
    skipped_details: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class _DscToken:
    symbol: int
    offset_bits: int | None = None


class SjisTunnelEncoder:
    """Encode text as CP932, tunneling unsupported chars into unused SJIS pairs."""

    def __init__(self, table: bytes | None = None):
        self._map: dict[str, int] = {}
        if table:
            self.set_mapping_table(table)

    def encode(self, text: str) -> bytes:
        output = bytearray()
        for char in text:
            tunnel_code = self._map.get(char)
            if tunnel_code is not None:
                output += tunnel_code.to_bytes(2, "big")
                continue
            try:
                raw = char.encode("cp932")
                if len(raw) == 2 and raw[0] >= 0xF0:
                    raise UnicodeEncodeError("cp932", char, 0, 1, "private CP932 range")
                output += raw
            except UnicodeEncodeError:
                output += self._get_tunnel_code(char).to_bytes(2, "big")
        return bytes(output)

    def mapping_table(self) -> bytes:
        output = bytearray()
        for char in self._map:
            code = ord(char)
            output.append(code & 0xFF)
            output.append((code >> 8) & 0xFF)
        return bytes(output)

    def set_mapping_table(self, table: bytes) -> None:
        if len(table) % 2:
            raise ValueError("SJIS tunnel table length must be even")
        self._map.clear()
        for i in range(0, len(table), 2):
            self._get_tunnel_code(chr(table[i] | (table[i + 1] << 8)))

    def _get_tunnel_code(self, char: str) -> int:
        tunnel_code = self._map.get(char)
        if tunnel_code is not None:
            return tunnel_code
        idx = len(self._map)
        valid_lows = _sjis_tunnel_lows()
        if idx == 0x0D * len(valid_lows):
            raise ValueError("SJIS tunnel limit exceeded")
        high_idx, low_idx = divmod(idx, len(valid_lows))
        high = 0xF0 + high_idx
        low = valid_lows[low_idx]
        tunnel_code = (high << 8) | low
        self._map[char] = tunnel_code
        return tunnel_code


def decode_sjis_tunnel_bytes(data: bytes, table: bytes | None = None) -> str:
    """Decode CP932 bytes that may contain BGI SJIS tunnel code points."""
    mappings: list[str] = []
    if table:
        if len(table) % 2:
            raise ValueError("SJIS tunnel table length must be even")
        for i in range(0, len(table), 2):
            mappings.append(chr(table[i] | (table[i + 1] << 8)))

    out: list[str] = []
    raw = bytearray()

    def flush_raw() -> None:
        if not raw:
            return
        out.append(bytes(raw).decode("cp932", errors="replace"))
        raw.clear()

    pos = 0
    while pos < len(data):
        byte = data[pos]
        if _is_sjis_lead(byte) and pos + 1 < len(data):
            low = data[pos + 1]
            idx = _sjis_tunnel_index(byte, low)
            if 0 <= idx < len(mappings):
                flush_raw()
                out.append(mappings[idx])
                pos += 2
                continue
            raw.extend((byte, low))
            pos += 2
            continue
        raw.append(byte)
        pos += 1
    flush_raw()
    return "".join(out)


def clean_bgi_display_translation(text: str) -> str:
    """BGI text boxes often wrap trailing full stops to the next line."""
    raw = (text or "").strip()
    if raw and not re.search(r"[\w\u3400-\u9fff]", raw, re.UNICODE):
        return raw
    text = clean_translation_text(text)
    if not text:
        return text
    if text.endswith("。"):
        return text[:-1].rstrip()
    for closer in ("」", "』", "”", "）", ")"):
        suffix = "。" + closer
        if text.endswith(suffix):
            return (text[:-len(suffix)].rstrip() + closer).strip()
    return text


_V1_OPERANDS: dict[int, str] = {
    0x0000: "i", 0x0001: "c", 0x0002: "i", 0x0003: "m",
    0x0008: "i", 0x0009: "i", 0x000A: "i",
    0x0010: "", 0x0011: "", 0x0015: "", 0x0016: "", 0x0017: "i",
    0x0018: "", 0x0019: "i", 0x001A: "", 0x001B: "", 0x001C: "",
    0x001D: "", 0x001E: "", 0x001F: "",
    0x0020: "", 0x0021: "", 0x0022: "", 0x0023: "", 0x0024: "",
    0x0025: "", 0x0026: "", 0x0027: "", 0x0028: "", 0x0029: "",
    0x002A: "", 0x002B: "",
    0x0030: "", 0x0031: "", 0x0032: "", 0x0033: "", 0x0034: "",
    0x0035: "", 0x0038: "", 0x0039: "", 0x003A: "", 0x003E: "",
    0x003F: "i", 0x0040: "", 0x0048: "",
    0x007B: "iii", 0x007E: "i", 0x007F: "ii",
    0x0080: "", 0x0081: "", 0x0082: "", 0x0083: "",
    0x0090: "", 0x0091: "", 0x0092: "", 0x0093: "", 0x0094: "",
    0x0095: "", 0x0098: "", 0x0099: "", 0x00A0: "", 0x00A8: "",
    0x00AA: "", 0x00AC: "", 0x00C0: "", 0x00C1: "", 0x00C2: "",
    0x00D0: "",
}
for _op in range(0x00E0, 0x010B):
    _V1_OPERANDS.setdefault(_op, "")
for _op in range(0x010D, 0x012B):
    _V1_OPERANDS.setdefault(_op, "")
for _op in range(0x012C, 0x0141):
    _V1_OPERANDS.setdefault(_op, "")
for _op in range(0x0141, 0x0154):
    _V1_OPERANDS.setdefault(_op, "")
for _op in (0x0156, 0x0157, 0x0158, 0x0159, 0x015A, 0x015C, 0x015D, 0x015E, 0x015F):
    _V1_OPERANDS.setdefault(_op, "")
for _op in range(0x0160, 0x0177):
    _V1_OPERANDS.setdefault(_op, "")
for _op in (0x0178, 0x0179, 0x017A, 0x017D, 0x017E, 0x017F):
    _V1_OPERANDS.setdefault(_op, "")
for _op in range(0x0180, 0x018C):
    _V1_OPERANDS.setdefault(_op, "")
for _op in (0x018D, 0x018E, 0x018F, 0x0190, 0x0191, 0x0194, 0x0195, 0x0196, 0x0197, 0x0198, 0x0199, 0x019C, 0x019D, 0x019E, 0x019F):
    _V1_OPERANDS.setdefault(_op, "")
for _op in range(0x01A0, 0x01B3):
    _V1_OPERANDS.setdefault(_op, "")
for _op in (0x01B4, 0x01B5, 0x01B6, 0x01B7, 0x01BF, 0x01D0, 0x01D4, 0x01D8, 0x01D9, 0x01E0, 0x01F0, 0x0200, 0x0204, 0x0205, 0x0208, 0x0209, 0x020A, 0x020C, 0x020E, 0x020F, 0x0210, 0x0220, 0x0222, 0x0225, 0x0226, 0x0228, 0x0229, 0x022A, 0x0230, 0x0231, 0x0232, 0x0233, 0x0234, 0x0235, 0x0236, 0x0237, 0x0238, 0x0239, 0x023A, 0x023B, 0x023C, 0x023D, 0x0240, 0x0241, 0x0242, 0x0244, 0x0245, 0x0248, 0x024C, 0x024D, 0x024E, 0x0250, 0x0251, 0x0252, 0x0254, 0x0255, 0x0256, 0x0257, 0x0258, 0x025E, 0x025F, 0x0260, 0x0261, 0x0262, 0x0266, 0x0268, 0x027F, 0x0280, 0x0281, 0x0284, 0x0288, 0x0289, 0x028A, 0x0290, 0x0294, 0x0295, 0x0296, 0x0297, 0x0298, 0x0299, 0x029C, 0x02A0, 0x02A1, 0x02A2, 0x02A3, 0x02A4, 0x02A8, 0x02C0, 0x02C1, 0x02C2, 0x02C3, 0x02C4, 0x02C5, 0x02C6, 0x02C7, 0x02C8, 0x02CA, 0x02CB, 0x02CC, 0x02CD, 0x02CE, 0x02CF, 0x02D0, 0x02D2, 0x02D4, 0x02D5, 0x02D6, 0x02D7, 0x02D8, 0x02D9, 0x02DA, 0x02DB, 0x02DC, 0x02DD, 0x02DE, 0x02DF, 0x02E0, 0x02E1, 0x02E2, 0x02E3, 0x02E4, 0x02E5, 0x02E6, 0x02E7, 0x02E8, 0x02E9, 0x02EA, 0x02EB, 0x02EC, 0x02EE, 0x02F0, 0x02F1, 0x02F3, 0x02F4, 0x02F8, 0x02FA, 0x02FC, 0x02FD, 0x0300, 0x0301, 0x0302, 0x0303, 0x0304, 0x0306, 0x0307, 0x0308, 0x0309, 0x030A, 0x030C, 0x030D, 0x030E, 0x0310, 0x0311, 0x0314, 0x031E, 0x031F, 0x0320, 0x0328, 0x032C, 0x0330, 0x0331, 0x0334, 0x0335, 0x0336, 0x0337, 0x0338, 0x0339, 0x033F, 0x0340, 0x0341, 0x0348, 0x0350, 0x0351, 0x0352, 0x0353, 0x0354, 0x0355, 0x0358, 0x0360, 0x0368, 0x0380, 0x0388, 0x038D, 0x038E, 0x038F, 0x0390, 0x0391, 0x0392, 0x0393, 0x0394, 0x03AF, 0x03C0, 0x03C1, 0x03C2, 0x03C4, 0x03C5, 0x03C6, 0x03C7, 0x03C8, 0x03C9, 0x03CA, 0x03D0, 0x03D2, 0x03D4, 0x03D5, 0x03D6, 0x03D8, 0x03DC, 0x03F0, 0x03F1, 0x03F4, 0x03F5, 0x03F6, 0x03F7, 0x03F8, 0x03FA, 0x03FB, 0x03FC, 0x03FD, 0x03FE, 0x03FF, 0x0400, 0x0401, 0x0402, 0x0403, 0x0404, 0x0405, 0x0408, 0x0409, 0x040A, 0x040B, 0x040C, 0x040D, 0x040F, 0x0410, 0x0411, 0x0412, 0x0413, 0x0418, 0x0427, 0x0428, 0x0429, 0x042A, 0x042B, 0x042C, 0x042D, 0x042F, 0x0430, 0x0431, 0x0432, 0x0440, 0x0441, 0x0442, 0x0444, 0x0448, 0x0449, 0x0450, 0x0451, 0x0452, 0x0453, 0x0454, 0x0455, 0x0458, 0x0459, 0x045C, 0x045D, 0x045E, 0x0480, 0x0481, 0x0482, 0x0483, 0x0484, 0x0485, 0x04C0, 0x04C1, 0x04C2, 0x04C3, 0x04C4, 0x04C5, 0x04C6, 0x04C7, 0x04C8, 0x04C9, 0x04CA, 0x04CB, 0x04D0, 0x04D1, 0x04D5, 0x04D8, 0x04D9, 0x04DA, 0x04E0, 0x04E4, 0x04E5, 0x04E8, 0x04E9, 0x04EA, 0x04EB):
    _V1_OPERANDS.setdefault(_op, "")

_V0_OPERANDS: dict[int, str] = {
    0x0010: "iim", 0x0011: "", 0x0012: "zz", 0x0013: "z", 0x0014: "z", 0x0015: "",
    0x0018: "iiiii", 0x0019: "iiii", 0x001A: "iii", 0x001B: "ziii", 0x001F: "i",
    0x0020: "", 0x0021: "", 0x0022: "i", 0x0024: "iiiii", 0x0025: "ii",
    0x0028: "zi", 0x0029: "zzi", 0x002A: "i", 0x002B: "zi",
    0x002C: "ziiiiiiii", 0x002D: "ziiiiiiii", 0x002E: "iiiii",
    0x0030: "zi", 0x0031: "zii", 0x0032: "i", 0x0033: "i", 0x0034: "ii",
    0x0035: "i", 0x0036: "i", 0x0037: "", 0x0038: "iziiiii", 0x0039: "ii",
    0x003A: "iziiiiiiii", 0x003B: "iiiiii", 0x003C: "iiiiiiiiii",
    0x003D: "iiiiiiiiiii", 0x003F: "i",
    0x0040: "iizii", 0x0041: "iizii", 0x0042: "iizi", 0x0043: "iizi",
    0x0044: "iizi", 0x0045: "iizi", 0x0046: "izi", 0x0047: "izi",
    0x0048: "ii", 0x0049: "ii", 0x004A: "izi", 0x004B: "", 0x004C: "zi",
    0x004D: "zi", 0x004E: "i", 0x004F: "i", 0x0050: "zi", 0x0051: "zzi",
    0x0052: "i", 0x0053: "zi", 0x0054: "zii",
    0x0060: "iiiii", 0x0061: "ii", 0x0062: "iiiiii", 0x0065: "i",
    0x0066: "ii", 0x0067: "i", 0x0068: "i", 0x0069: "i", 0x006A: "i",
    0x006B: "i", 0x006C: "i", 0x006E: "iii", 0x006F: "i", 0x0070: "izi",
    0x0071: "i", 0x0072: "iii", 0x0073: "iii", 0x0074: "izi", 0x0075: "i",
    0x0076: "iii", 0x0078: "izi", 0x0079: "i", 0x007A: "iii",
    0x0080: "izii", 0x0081: "z", 0x0082: "i", 0x0083: "i", 0x0084: "izi",
    0x0085: "z", 0x0086: "i", 0x0087: "i", 0x0088: "z", 0x008C: "i",
    0x008D: "i", 0x008E: "i", 0x0090: "i", 0x0091: "i", 0x0092: "i",
    0x0093: "i", 0x0094: "i", 0x0098: "ii", 0x0099: "ii", 0x009A: "ii",
    0x009B: "ii", 0x009C: "ii", 0x009D: "ii",
    0x00A0: "c", 0x00A1: "ic", 0x00A2: "ic", 0x00A3: "iic", 0x00A4: "iic",
    0x00A5: "iic", 0x00A6: "iic", 0x00A7: "iic", 0x00A8: "iic",
    0x00AC: "c", 0x00AD: "", 0x00AE: "i", 0x00AF: "", 0x00B8: "",
    0x00B9: "i", 0x00BA: "i", 0x00C0: "z", 0x00C1: "z", 0x00C2: "",
    0x00C4: "i", 0x00C8: "z", 0x00C9: "", 0x00CA: "i", 0x00D0: "",
    0x00D4: "i", 0x00D8: "i", 0x00D9: "i", 0x00DA: "i", 0x00DB: "i",
    0x00DC: "i", 0x00F8: "z", 0x00F9: "zi", 0x00FE: "h",
    0x0110: "zz", 0x0111: "i", 0x0120: "i", 0x0121: "i", 0x0128: "zii",
    0x012A: "ii", 0x0134: "ii", 0x0135: "i", 0x0136: "i",
    0x0138: "iziiiiziii", 0x013B: "iiiiiiii",
    0x0140: "iiziiii", 0x0141: "iiziiii", 0x0142: "iiziii",
    0x0143: "iiziii", 0x0144: "iiziii", 0x0145: "iiziii",
    0x0146: "iziii", 0x0147: "iziii", 0x0148: "ii", 0x0149: "ii",
    0x014B: "ziiz", 0x0150: "zii", 0x0151: "ziii", 0x0152: "ii",
    0x0153: "iii", 0x016E: "iiiiii", 0x016F: "iiiiiii", 0x0170: "izzii",
    0x01C0: "zz", 0x01C1: "zz", 0x0249: "z", 0x024C: "zziii",
    0x024D: "z", 0x024E: "zz", 0x024F: "z",
}

_JP_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")


def decompress_dsc(data: bytes) -> bytes:
    """Decompress BGI ``DSC FORMAT 1.00`` data."""
    if not data.startswith(DSC_MAGIC):
        return data

    key, unpacked_size, decode_count, _reserved = struct.unpack_from("<IIII", data, 16)
    depths: list[tuple[int, int]] = []
    for code, byte in enumerate(data[32:544]):
        key, mask = _update_dsc_key(key)
        depth = (byte - mask) & 0xFF
        if depth:
            depths.append((depth, code))
    depths.sort()

    nodes: list[dict[str, int | bool | None]] = [{"parent": True, "code": None, "left": None, "right": None} for _ in range(2048)]
    current = [0]
    next_node = 1
    index = 0
    depth = 0
    while index < len(depths):
        next_level: list[int] = []
        leaves = 0
        while index < len(depths) and depths[index][0] == depth:
            node = current[leaves]
            nodes[node]["parent"] = False
            nodes[node]["code"] = depths[index][1]
            leaves += 1
            index += 1
        for node in current[leaves:]:
            nodes[node]["left"] = next_node
            next_level.append(next_node)
            next_node += 1
            nodes[node]["right"] = next_node
            next_level.append(next_node)
            next_node += 1
        current = next_level
        depth += 1

    compressed = data[544:]
    bit_pos = 0

    def read_bits(count: int) -> int:
        nonlocal bit_pos
        value = 0
        for _ in range(count):
            if bit_pos // 8 >= len(compressed):
                raise EOFError("DSC bitstream ended early")
            bit = (compressed[bit_pos // 8] >> (7 - bit_pos % 8)) & 1
            bit_pos += 1
            value = (value << 1) | bit
        return value

    output = bytearray()
    for _ in range(decode_count):
        node = 0
        while nodes[node]["parent"]:
            node = int(nodes[node]["right"] if read_bits(1) else nodes[node]["left"])
        code = int(nodes[node]["code"])
        if code >= 256:
            count = (code & 0xFF) + 2
            offset = read_bits(12) + 2
            for _ in range(count):
                output.append(output[-offset])
        else:
            output.append(code)
        if len(output) >= unpacked_size:
            break

    return bytes(output[:unpacked_size])


def compress_dsc(data: bytes, key: int | None = None, use_lz: bool = True) -> bytes:
    """Compress bytes as BGI ``DSC FORMAT 1.00`` data.

    The compressor emits the same Huffman/LZ token stream that
    :func:`decompress_dsc` reads. ``key`` only obfuscates the 512-byte depth
    table; using an original script key is preferred when repacking.
    """
    key = 0 if key is None else key & 0xFFFFFFFF
    tokens = _tokenize_dsc_lz(data) if use_lz else [_DscToken(byte) for byte in data]
    depths = _build_dsc_huffman_depths(tokens)
    codes = _build_dsc_canonical_codes(depths)

    writer = _DscBitWriter()
    for token in tokens:
        value, bit_count = codes[token.symbol]
        writer.write_bits(value, bit_count)
        if token.symbol >= 256:
            if token.offset_bits is None:
                raise ValueError("DSC backref token missing offset")
            writer.write_bits(token.offset_bits, 12)

    header = bytearray(DSC_MAGIC)
    header += struct.pack("<IIII", key, len(data), len(tokens), 0)
    table_key = key
    for depth in depths:
        table_key, mask = _update_dsc_key(table_key)
        header.append((depth + mask) & 0xFF)
    return bytes(header + writer.finish())


def get_dsc_key(data: bytes) -> int | None:
    """Return the DSC table key, or ``None`` for non-DSC data."""
    if len(data) < 32 or not data.startswith(DSC_MAGIC):
        return None
    return struct.unpack_from("<I", data, 16)[0]


def inspect_bgi_v1_script(
    data: bytes,
    include_internal: bool = False,
    require_japanese: bool = True,
    include_empty: bool = False,
    raw_code: bool = False,
) -> BgiScriptInfo | None:
    """Inspect a BurikoCompiledScript v1 payload."""
    if not raw_code and not data.startswith(BGI_V1_MAGIC):
        return None
    if raw_code:
        code_offset = 0
    else:
        header_size = struct.unpack_from("<I", data, len(BGI_V1_MAGIC))[0]
        code_offset = len(BGI_V1_MAGIC) + header_size
    pos = code_offset
    largest_code_addr = 0
    refs: list[tuple[int, int, str]] = []
    string_stack: list[tuple[int, int]] = []
    pending_flushes: list[tuple[int, int, int, str]] = []

    def add_ref(operand_offset: int, addr: int, kind: str, op_pos: int | None = None) -> None:
        refs.append((operand_offset, addr, kind))
        if kind == "internal" and op_pos is not None:
            pending_flushes.append((op_pos, operand_offset, addr, kind))

    def mark_recent_internal_as_choice(choice_op_pos: int) -> None:
        nonlocal refs
        if not pending_flushes:
            return
        recent = {
            (operand_offset, addr)
            for op_pos, operand_offset, addr, _kind in pending_flushes
            if 0 <= choice_op_pos - op_pos <= 0x200 and _JP_RE.search(read_string_at(addr))
        }
        if not recent:
            return
        refs = [
            (operand_offset, addr, "choice" if kind == "internal" and (operand_offset, addr) in recent else kind)
            for operand_offset, addr, kind in refs
        ]

    def read_string_at(addr: int) -> str:
        start = code_offset + addr
        if start < 0 or start >= len(data):
            return ""
        end = data.find(b"\x00", start)
        if end < 0:
            end = len(data)
        return data[start:end].decode("cp932", errors="replace")

    def is_empty(addr: int) -> bool:
        start = code_offset + addr
        return start < 0 or start >= len(data) or data[start:start + 1] == b"\x00"

    def output_internal() -> None:
        while string_stack:
            off, addr = string_stack.pop()
            add_ref(off, addr, "internal", pos)

    while pos + 4 <= len(data):
        opcode = struct.unpack_from("<I", data, pos)[0]
        op_pos = pos
        pos += 4
        if opcode == 0x0003:
            if pos + 4 > len(data):
                break
            string_stack.append((pos, struct.unpack_from("<I", data, pos)[0]))
            pos += 4
        elif opcode == 0x001C:
            if string_stack:
                item = string_stack.pop()
                add_ref(item[0], item[1], "internal", op_pos)
                if read_string_at(item[1]) == "_SelectEx":
                    while string_stack:
                        off, addr = string_stack.pop(0)
                        add_ref(off, addr, "choice")
        elif opcode in (0x0140, 0x0143):
            if string_stack:
                msg = string_stack.pop()
                if string_stack:
                    name = string_stack.pop()
                    add_ref(name[0], name[1], "name" if not is_empty(name[1]) else "internal", op_pos if is_empty(name[1]) else None)
                add_ref(msg[0], msg[1], "message" if not is_empty(msg[1]) else "internal", op_pos if is_empty(msg[1]) else None)
        elif opcode == 0x0160:
            mark_recent_internal_as_choice(op_pos)
            while string_stack:
                off, addr = string_stack.pop(0)
                add_ref(off, addr, "choice")
        else:
            template = _V1_OPERANDS.get(opcode)
            if template is None:
                break
            for item in template:
                if item == "i":
                    pos += 4
                elif item == "c":
                    if pos + 4 > len(data):
                        break
                    largest_code_addr = max(largest_code_addr, struct.unpack_from("<I", data, pos)[0])
                    pos += 4
                elif item == "m":
                    if pos + 4 > len(data):
                        break
                    add_ref(pos, struct.unpack_from("<I", data, pos)[0], "message")
                    pos += 4
                elif item == "z":
                    zero = data.find(b"\x00", pos)
                    pos = len(data) if zero < 0 else zero + 1
                elif item == "h":
                    pos += 2
        if opcode in (0x007E, 0x007F, 0x00FE):
            output_internal()
        if opcode in (0x001B, 0x00F4) and largest_code_addr < pos - code_offset:
            break

    code_end = pos
    output_internal()
    if not raw_code:
        refs.extend(_scan_bgi_v1_string_pool_refs(
            data,
            code_offset,
            refs,
            require_japanese=require_japanese,
            include_empty=include_empty,
        ))

    result: list[BgiStringRef] = []
    seen: set[tuple[int, str]] = set()
    for operand_offset, addr, kind in refs:
        text = read_string_at(addr)
        if not include_internal and kind == "internal":
            continue
        if not include_empty and not text:
            continue
        if require_japanese and not _JP_RE.search(text):
            continue
        key = (addr, kind)
        if key in seen:
            continue
        seen.add(key)
        result.append(BgiStringRef(operand_offset, code_offset + addr, text, kind))
    return BgiScriptInfo(code_offset=code_offset, code_end=code_end, refs=result)


def _scan_bgi_v1_string_pool_refs(
    data: bytes,
    code_offset: int,
    refs: list[tuple[int, int, str]],
    require_japanese: bool,
    include_empty: bool,
) -> list[tuple[int, int, str]]:
    """Supplement headered BGI v1 refs by scanning the trailing string pool.

    Some Ethornell scripts contain many code blocks. A conservative linear VM
    walk can stop at the first block terminator and miss later scenario text,
    while the trailing string pool still stores those strings contiguously.
    These synthetic refs have no safe operand for append retargeting, so their
    operand is ``-1`` and patching is limited to in-slot replacement.
    """
    valid_offsets: list[int] = []
    for _operand_offset, addr, _kind in refs:
        start = code_offset + addr
        if start < code_offset or start >= len(data):
            continue
        end = data.find(b"\x00", start)
        if end < 0 or end == start:
            continue
        valid_offsets.append(start)
    if not valid_offsets:
        return []

    pool_start = min(valid_offsets)
    existing = {addr for _operand_offset, addr, _kind in refs}
    supplemental: list[tuple[int, int, str]] = []
    pos = pool_start
    while pos < len(data):
        end = data.find(b"\x00", pos)
        if end < 0:
            break
        raw = data[pos:end]
        next_pos = end + 1
        if raw or include_empty:
            try:
                text = raw.decode("cp932")
            except UnicodeDecodeError:
                text = raw.decode("cp932", errors="replace")
            rel = pos - code_offset
            if rel not in existing and _looks_like_bgi_user_pool_string(text, require_japanese, include_empty):
                supplemental.append((-1, rel, _infer_bgi_pool_string_kind(text)))
                existing.add(rel)
        pos = next_pos
    return supplemental


def _looks_like_bgi_user_pool_string(text: str, require_japanese: bool, include_empty: bool) -> bool:
    if not text:
        return include_empty
    stripped = text.strip()
    if not stripped:
        return include_empty
    if len(stripped) > 1000:
        return False
    if require_japanese and not _JP_RE.search(stripped):
        return False
    if stripped.startswith(("D:\\", "C:\\", "_")):
        return False
    if re.fullmatch(r"[A-Za-z0-9_./\\:-]+", stripped):
        return False
    return not any(0 < ord(ch) < 32 and ch not in "\n\r\t" for ch in text)


def _infer_bgi_pool_string_kind(text: str) -> str:
    stripped = text.strip()
    if "\n" in text or len(stripped) > 12:
        return "message"
    if re.search(r"[「」『』。、，！？!?…（）()]", stripped):
        return "message"
    return "name"


def extract_bgi_v1_strings(
    data: bytes,
    include_internal: bool = False,
    require_japanese: bool = True,
) -> list[BgiStringRef]:
    """Extract referenced strings from a BurikoCompiledScript v1 payload."""
    info = inspect_bgi_v1_script(
        data,
        include_internal=include_internal,
        require_japanese=require_japanese,
    )
    return info.refs if info else []


def inspect_bgi_v1_raw_script(
    data: bytes,
    include_internal: bool = False,
    require_japanese: bool = True,
    include_empty: bool = False,
) -> BgiScriptInfo | None:
    """Inspect a headerless 32-bit Ethornell bytecode payload."""
    info = inspect_bgi_v1_script(
        data,
        include_internal=include_internal,
        require_japanese=require_japanese,
        include_empty=include_empty,
        raw_code=True,
    )
    if not info or not info.refs:
        return None
    return info


def extract_bgi_v1_raw_strings(
    data: bytes,
    include_internal: bool = False,
    require_japanese: bool = True,
) -> list[BgiStringRef]:
    """Extract strings from headerless 32-bit Ethornell bytecode."""
    info = inspect_bgi_v1_raw_script(
        data,
        include_internal=include_internal,
        require_japanese=require_japanese,
    )
    return info.refs if info else []


def inspect_bgi_v0_script(
    data: bytes,
    include_internal: bool = False,
    require_japanese: bool = True,
    include_empty: bool = False,
) -> BgiScriptInfo | None:
    """Inspect an Ethornell v0 bytecode payload.

    This follows VNTextPatch's EthornellV0Disassembler: opcodes are little
    endian uint16 values, code addresses and string addresses are 32-bit
    absolute offsets from the start of the payload, and string data follows
    the code stream.
    """
    if len(data) < 2 or data.startswith(BGI_V1_MAGIC):
        return None

    pos = 0
    largest_code_addr = 0
    refs: list[tuple[int, int, str]] = []

    def need(size: int) -> bool:
        return 0 <= size and pos + size <= len(data)

    def read_int32() -> int | None:
        nonlocal pos
        if not need(4):
            return None
        value = struct.unpack_from("<i", data, pos)[0]
        pos += 4
        return value

    def read_code_addr() -> bool:
        nonlocal largest_code_addr
        value = read_int32()
        if value is None:
            return False
        largest_code_addr = max(largest_code_addr, value)
        return True

    def read_string_addr(kind: str) -> bool:
        value_pos = pos
        value = read_int32()
        if value is None:
            return False
        refs.append((value_pos, value, kind))
        return True

    def skip_inline_string() -> bool:
        nonlocal pos
        end = data.find(b"\x00", pos)
        if end < 0:
            return False
        pos = end + 1
        return True

    def read_template(template: str) -> bool:
        nonlocal pos
        for item in template:
            if item == "h":
                if not need(2):
                    return False
                pos += 2
            elif item == "i":
                if read_int32() is None:
                    return False
            elif item == "c":
                if not read_code_addr():
                    return False
            elif item == "m":
                if not read_string_addr("message"):
                    return False
            elif item == "z":
                if not skip_inline_string():
                    return False
            else:
                return False
        return True

    while pos + 2 <= len(data):
        opcode = struct.unpack_from("<H", data, pos)[0]
        pos += 2

        if opcode == 0x00A9:
            count = read_int32()
            if count is None or count < 0 or count > 100000:
                return None
            for _ in range(count):
                if not read_code_addr():
                    return None
        elif opcode in (0x00B0, 0x00B4):
            count = read_int32()
            if count is None or count < 0 or count > 100000:
                return None
            for _ in range(count):
                if not skip_inline_string():
                    return None
        elif opcode == 0x00FD:
            count = read_int32()
            if count is None or count < 0 or count > 100000:
                return None
            for _ in range(count):
                if not skip_inline_string() or not read_code_addr():
                    return None
        elif opcode == 0x0248:
            return None
        else:
            template = _V0_OPERANDS.get(opcode)
            if template is None or not read_template(template):
                return None

        if opcode == 0x00C2 and largest_code_addr < pos:
            break

    code_end = pos

    def read_string_at(addr: int) -> str:
        if addr < 0 or addr >= len(data):
            return ""
        end = data.find(b"\x00", addr)
        if end < 0:
            end = len(data)
        return data[addr:end].decode("cp932", errors="replace")

    result: list[BgiStringRef] = []
    seen: set[tuple[int, str]] = set()
    for operand_offset, addr, kind in refs:
        text = read_string_at(addr)
        if not include_internal and kind == "internal":
            continue
        if not include_empty and not text:
            continue
        if require_japanese and not _JP_RE.search(text):
            continue
        key = (operand_offset, kind)
        if key in seen:
            continue
        seen.add(key)
        result.append(BgiStringRef(operand_offset, addr, text, kind))
    return BgiScriptInfo(code_offset=0, code_end=code_end, refs=result)


def extract_bgi_v0_strings(
    data: bytes,
    include_internal: bool = False,
    require_japanese: bool = True,
) -> list[BgiStringRef]:
    """Extract referenced strings from an Ethornell v0 bytecode payload."""
    info = inspect_bgi_v0_script(
        data,
        include_internal=include_internal,
        require_japanese=require_japanese,
    )
    return info.refs if info else []


def patch_bgi_v1_script(
    data: bytes,
    translations: dict[str, str],
    encoder: SjisTunnelEncoder | None = None,
    include_internal: bool = False,
    allow_append: bool = True,
    compress_oversized: bool = True,
    slot_rewrites: dict[tuple[str, int], str] | None = None,
    raw_code: bool = False,
) -> tuple[bytes, BgiPatchStats]:
    """Patch referenced strings while preserving the original script body.

    Short translations are written back into their original NUL-terminated
    slots. Longer translations are appended and the string operand is retargeted
    only when ``allow_append`` is true. BGI games can have tight code-area
    limits, so repack pipelines should normally disable appending.
    """
    info = inspect_bgi_v1_script(
        data,
        include_internal=include_internal,
        require_japanese=False,
        include_empty=True,
        raw_code=raw_code,
    )
    if not info:
        return data, BgiPatchStats()
    encoder = encoder or SjisTunnelEncoder()
    slot_rewrites = slot_rewrites or {}
    output = bytearray(data)
    appended_offsets: dict[bytes, int] = {}
    stats = BgiPatchStats()

    for ref in info.refs:
        translated = translations.get(ref.text)
        if not translated or translated == ref.text:
            continue
        translated = clean_bgi_display_translation(translated)
        if not translated or translated == ref.text:
            continue

        encoded = encoder.encode(translated)
        original_end = data.find(b"\x00", ref.text_offset)
        if ref.text_offset < 0 or original_end < 0:
            continue

        original_len = original_end - ref.text_offset
        can_retarget = ref.operand_offset >= 0
        if len(encoded) <= original_len:
            padded = encoded + b"\x00" + (b"\x00" * (original_len - len(encoded)))
            output[ref.text_offset:original_end + 1] = padded
            text_offset = ref.text_offset
            stats.direct += 1
        else:
            rewritten = slot_rewrites.get((ref.text, original_len))
            if rewritten:
                rewritten = clean_bgi_display_translation(rewritten)
                rewritten_encoded = encoder.encode(rewritten)
                if len(rewritten_encoded) <= original_len:
                    padded = rewritten_encoded + b"\x00" + (b"\x00" * (original_len - len(rewritten_encoded)))
                    output[ref.text_offset:original_end + 1] = padded
                    stats.ai_rewritten += 1
                    stats.patched += 1
                    continue
            if compress_oversized:
                fitted = fit_fixed_slot(translated, original_len, encoder.encode)
                if fitted and fitted != translated:
                    encoded = encoder.encode(fitted)
                    padded = encoded + b"\x00" + (b"\x00" * (original_len - len(encoded)))
                    output[ref.text_offset:original_end + 1] = padded
                    text_offset = ref.text_offset
                    stats.compressed += 1
                    stats.patched += 1
                    continue
            if not allow_append or not can_retarget:
                stats.skipped_too_long += 1
                stats.skipped_details.append(
                    {
                        "original": ref.text,
                        "translated": translated,
                        "slot_bytes": original_len,
                        "encoded_bytes": len(encoded),
                        "kind": ref.kind,
                        "operand_offset": ref.operand_offset,
                        "text_offset": ref.text_offset,
                    }
                )
                continue
            text_offset = appended_offsets.get(encoded)
            if text_offset is None:
                text_offset = len(output)
                output += encoded
                output.append(0)
                appended_offsets[encoded] = text_offset
            relative = text_offset - info.code_offset
            struct.pack_into("<I", output, ref.operand_offset, relative)
            stats.appended += 1
        stats.patched += 1

    if stats.patched == 0:
        return data, stats
    return bytes(output), stats


def patch_bgi_v1_raw_script(
    data: bytes,
    translations: dict[str, str],
    encoder: SjisTunnelEncoder | None = None,
    include_internal: bool = False,
    allow_append: bool = True,
    compress_oversized: bool = True,
    slot_rewrites: dict[tuple[str, int], str] | None = None,
) -> tuple[bytes, BgiPatchStats]:
    """Patch headerless 32-bit Ethornell scripts by rebuilding string data."""
    info = inspect_bgi_v1_script(
        data,
        include_internal=True,
        require_japanese=False,
        include_empty=True,
        raw_code=True,
    )
    if not info:
        return data, BgiPatchStats()

    encoder = encoder or SjisTunnelEncoder()
    output = bytearray(data[:info.code_end])
    string_offsets: dict[bytes, int] = {}
    stats = BgiPatchStats()
    changed = False

    for ref in info.refs:
        text = ref.text
        if ref.kind != "internal" or include_internal:
            translated = translations.get(ref.text)
            if translated and translated != ref.text:
                translated = _clean_expandable_translation(clean_bgi_display_translation(translated))
                if translated and translated != ref.text:
                    text = translated
                    changed = True
                    stats.patched += 1
                    stats.appended += 1

        encoded = encoder.encode(text)
        text_offset = string_offsets.get(encoded)
        if text_offset is None:
            text_offset = len(output)
            output += encoded
            output.append(0)
            string_offsets[encoded] = text_offset
        if ref.operand_offset + 4 <= len(output):
            struct.pack_into("<I", output, ref.operand_offset, text_offset)

    if not changed:
        return data, stats
    return bytes(output), stats


def patch_bgi_v0_script(
    data: bytes,
    translations: dict[str, str],
    encoder: SjisTunnelEncoder | None = None,
    include_internal: bool = False,
    allow_append: bool = True,
    compress_oversized: bool = True,
    slot_rewrites: dict[tuple[str, int], str] | None = None,
) -> tuple[bytes, BgiPatchStats]:
    """Patch Ethornell v0 scripts by rebuilding the trailing string table."""
    info = inspect_bgi_v0_script(
        data,
        include_internal=include_internal,
        require_japanese=False,
        include_empty=True,
    )
    if not info:
        return data, BgiPatchStats()

    encoder = encoder or SjisTunnelEncoder()
    output = bytearray(data[:info.code_end])
    string_offsets: dict[bytes, int] = {}
    stats = BgiPatchStats()
    changed = False

    for ref in info.refs:
        text = ref.text
        translated = translations.get(ref.text)
        if translated and translated != ref.text:
            translated = _clean_expandable_translation(clean_bgi_display_translation(translated))
            if translated and translated != ref.text:
                text = translated
                changed = True
                stats.patched += 1
                stats.appended += 1

        encoded = encoder.encode(text)
        text_offset = string_offsets.get(encoded)
        if text_offset is None:
            text_offset = len(output)
            output += encoded
            output.append(0)
            string_offsets[encoded] = text_offset
        if ref.operand_offset + 4 <= len(output):
            struct.pack_into("<I", output, ref.operand_offset, text_offset)

    if not changed:
        return data, stats
    return bytes(output), stats


def patch_bgi_script(
    data: bytes,
    translations: dict[str, str],
    encoder: SjisTunnelEncoder | None = None,
    include_internal: bool = False,
    allow_append: bool = True,
    compress_oversized: bool = True,
    slot_rewrites: dict[tuple[str, int], str] | None = None,
) -> tuple[bytes, BgiPatchStats]:
    """Patch a decompressed BGI script payload."""
    if data.startswith(BGI_V1_MAGIC):
        return patch_bgi_v1_script(
            data,
            translations,
            encoder,
            include_internal=include_internal,
            allow_append=allow_append,
            compress_oversized=compress_oversized,
            slot_rewrites=slot_rewrites,
        )
    if inspect_bgi_v1_raw_script(data, require_japanese=False, include_empty=True):
        return patch_bgi_v1_raw_script(
            data,
            translations,
            encoder,
            include_internal=include_internal,
            allow_append=True,
            compress_oversized=compress_oversized,
            slot_rewrites=slot_rewrites,
        )
    if inspect_bgi_v0_script(data, require_japanese=False, include_empty=True):
        return patch_bgi_v0_script(
            data,
            translations,
            encoder,
            include_internal=include_internal,
            allow_append=allow_append,
            compress_oversized=compress_oversized,
            slot_rewrites=slot_rewrites,
        )
    return data, BgiPatchStats()


def extract_bgi_strings(
    data: bytes,
    include_internal: bool = False,
    require_japanese: bool = True,
) -> list[BgiStringRef]:
    """Decompress DSC if needed and extract BGI script strings."""
    script = decompress_dsc(data)
    if script.startswith(BGI_V1_MAGIC):
        return extract_bgi_v1_strings(
            script,
            include_internal=include_internal,
            require_japanese=require_japanese,
        )
    refs = extract_bgi_v1_raw_strings(
        script,
        include_internal=include_internal,
        require_japanese=require_japanese,
    )
    if refs:
        return refs
    refs = extract_bgi_v0_strings(
        script,
        include_internal=include_internal,
        require_japanese=require_japanese,
    )
    if refs:
        return refs
    return []


def _update_dsc_key(key: int) -> tuple[int, int]:
    low = (key & 0xFFFF) * 20021
    high = ((((ord("S") << 24) | (ord("D") << 16) | (key >> 16)) * 20021) + key * 346 + (low >> 16)) & 0xFFFF
    key = ((high << 16) + (low & 0xFFFF) + 1) & 0xFFFFFFFF
    return key, high & 0x7FFF


def _sjis_tunnel_lows() -> list[int]:
    return [low for low in range(0x40, 0xFD) if low != 0x7F]


def _is_sjis_lead(byte: int) -> bool:
    return 0x81 <= byte < 0xA0 or 0xE0 <= byte < 0xFD


def _sjis_tunnel_index(high: int, low: int) -> int:
    if high < 0xF0 or high > 0xFC:
        return -1
    if low < 0x40 or low > 0xFC or low == 0x7F:
        return -1
    low_idx = low - 0x40 if low < 0x7F else low - 0x41
    return (high - 0xF0) * len(_sjis_tunnel_lows()) + low_idx


def _clean_expandable_translation(text: str) -> str:
    """Light cleanup for scripts whose string table can grow."""
    return re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


class _DscBitWriter:
    def __init__(self) -> None:
        self._data = bytearray()
        self._byte = 0
        self._used = 0

    def write_bits(self, value: int, count: int) -> None:
        for shift in range(count - 1, -1, -1):
            self._byte = (self._byte << 1) | ((value >> shift) & 1)
            self._used += 1
            if self._used == 8:
                self._data.append(self._byte)
                self._byte = 0
                self._used = 0

    def finish(self) -> bytes:
        if self._used:
            self._data.append(self._byte << (8 - self._used))
            self._byte = 0
            self._used = 0
        return bytes(self._data)


def _tokenize_dsc_lz(data: bytes) -> list[_DscToken]:
    if not data:
        return []

    max_distance = 4097
    max_length = 257
    min_length = 3
    max_candidates = 96
    n = len(data)
    positions: dict[int, deque[int]] = {}
    tokens: list[_DscToken] = []

    def key_at(pos: int) -> int | None:
        if pos + min_length > n:
            return None
        return (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]

    def add_position(pos: int) -> None:
        key = key_at(pos)
        if key is None:
            return
        bucket = positions.setdefault(key, deque())
        bucket.append(pos)
        while bucket and pos - bucket[0] > max_distance:
            bucket.popleft()
        while len(bucket) > max_candidates:
            bucket.popleft()

    def find_match(pos: int) -> tuple[int, int]:
        key = key_at(pos)
        if key is None:
            return 0, 0
        bucket = positions.get(key)
        if not bucket:
            return 0, 0

        best_len = 0
        best_distance = 0
        limit = min(max_length, n - pos)
        for prev in reversed(bucket):
            distance = pos - prev
            if distance < 2:
                continue
            if distance > max_distance:
                break
            length = 0
            while length < limit and data[prev + length] == data[pos + length]:
                length += 1
            if length > best_len:
                best_len = length
                best_distance = distance
                if best_len == limit:
                    break
        if best_len < min_length:
            return 0, 0
        return best_len, best_distance

    pos = 0
    while pos < n:
        length, distance = find_match(pos)
        if length:
            tokens.append(_DscToken(256 + length - 2, distance - 2))
            for item in range(pos, pos + length):
                add_position(item)
            pos += length
            continue

        tokens.append(_DscToken(data[pos]))
        add_position(pos)
        pos += 1

    return tokens


def _build_dsc_huffman_depths(tokens: list[_DscToken]) -> list[int]:
    depths = [0] * 512
    if not tokens:
        return depths

    frequencies = Counter(token.symbol for token in tokens)
    if len(frequencies) == 1:
        depths[next(iter(frequencies))] = 1
        return depths

    heap: list[tuple[int, int, int, list[int]]] = []
    order = 0
    for symbol, frequency in frequencies.items():
        heappush(heap, (frequency, symbol, order, [symbol]))
        order += 1

    while len(heap) > 1:
        freq_a, min_a, _order_a, symbols_a = heappop(heap)
        freq_b, min_b, _order_b, symbols_b = heappop(heap)
        for symbol in symbols_a:
            depths[symbol] += 1
        for symbol in symbols_b:
            depths[symbol] += 1
        merged = symbols_a + symbols_b
        heappush(heap, (freq_a + freq_b, min(min_a, min_b), order, merged))
        order += 1

    if max(depths) > 255:
        return _build_balanced_dsc_depths(frequencies)
    return depths


def _build_balanced_dsc_depths(frequencies: Counter[int]) -> list[int]:
    depths = [0] * 512
    used = sorted(frequencies)
    if not used:
        return depths
    depth = max(1, (len(used) - 1).bit_length())
    for symbol in used:
        depths[symbol] = depth
    return depths


def _build_dsc_canonical_codes(depths: list[int]) -> dict[int, tuple[int, int]]:
    max_depth = max(depths, default=0)
    by_depth: dict[int, list[int]] = {}
    for symbol, depth in enumerate(depths):
        if depth:
            by_depth.setdefault(depth, []).append(symbol)

    current = [0]
    codes: dict[int, tuple[int, int]] = {}
    for depth in range(max_depth + 1):
        leaves = sorted(by_depth.get(depth, []))
        if len(leaves) > len(current):
            raise ValueError("Invalid DSC Huffman depth table")
        for index, symbol in enumerate(leaves):
            codes[symbol] = (current[index], depth)
        next_level: list[int] = []
        for value in current[len(leaves):]:
            next_level.append(value << 1)
            next_level.append((value << 1) | 1)
        current = next_level

    return codes
