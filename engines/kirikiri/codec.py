"""KiriKiri 文本编解码、加扰/解扰、SJIS tunnel、静态 XP3 过滤器与脚本质量判定。"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from engines.base import TextItem
from utils.kirikiri_psb import is_kirikiri_scn
from utils.logger import debug, info, warning

from engines.kirikiri.spans import _extract_kirikiri_text_spans

if TYPE_CHECKING:
    from engines.kirikiri.xp3 import _Xp3Entry


@dataclass(frozen=True)
class _DecodedText:
    text: str
    encoding: str
    newline: str
    scramble_mode: int | None = None


@dataclass(frozen=True)
class _Xp3FilterDecode:
    data: bytes
    key: int
    decoded: _DecodedText


_KIRIKIRI_SJIS_TUNNEL_BASE = 0xE000
_KIRIKIRI_SJIS_TUNNEL_ALPHABET = "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
_KIRIKIRI_SJIS_TUNNEL_PREFIX = "〓"
_KIRIKIRI_SJIS_TUNNEL_LIMIT = len(_KIRIKIRI_SJIS_TUNNEL_ALPHABET) ** 2


class _KirikiriSjisTunnelEncoder:
    """Represent unsupported Chinese as ordinary CP932 full-width token text."""

    def __init__(self, table: bytes | None = None):
        self._map: dict[str, int] = {}
        if table:
            self.set_mapping_table(table)

    def encode(self, text: str) -> str:
        out: list[str] = []
        for char in text:
            if _kirikiri_char_needs_sjis_tunnel(char):
                out.append(_kirikiri_sjis_tunnel_token(self._get_index(char)))
            else:
                out.append(char)
        return "".join(out)

    def mapping_table(self) -> bytes:
        out = bytearray()
        for char in self._map:
            code = ord(char)
            out.append(code & 0xFF)
            out.append((code >> 8) & 0xFF)
        return bytes(out)

    def set_mapping_table(self, table: bytes) -> None:
        if len(table) % 2:
            raise ValueError("KiriKiri SJIS tunnel table length must be even")
        self._map.clear()
        for i in range(0, len(table), 2):
            self._get_index(chr(table[i] | (table[i + 1] << 8)))

    def _get_index(self, char: str) -> int:
        existing = self._map.get(char)
        if existing is not None:
            return existing
        idx = len(self._map)
        if idx >= _KIRIKIRI_SJIS_TUNNEL_LIMIT:
            raise ValueError("KiriKiri SJIS tunnel limit exceeded")
        self._map[char] = idx
        return idx


def _kirikiri_char_needs_sjis_tunnel(char: str) -> bool:
    try:
        raw = char.encode("cp932")
    except UnicodeEncodeError:
        return True
    return len(raw) == 2 and raw[0] >= 0xF0


def _kirikiri_sjis_tunnel_token(index: int) -> str:
    base = len(_KIRIKIRI_SJIS_TUNNEL_ALPHABET)
    hi, lo = divmod(index, base)
    return (
        _KIRIKIRI_SJIS_TUNNEL_PREFIX
        + _KIRIKIRI_SJIS_TUNNEL_ALPHABET[hi]
        + _KIRIKIRI_SJIS_TUNNEL_ALPHABET[lo]
    )


def _kirikiri_text_needs_sjis_tunnel(text: str) -> bool:
    return any(_kirikiri_char_needs_sjis_tunnel(char) for char in text)


def _kirikiri_should_use_sjis_tunnel(items: list[TextItem]) -> bool:
    if _kirikiri_should_use_ascii_placeholder(items):
        return False
    has_filtered_xp3 = any(isinstance((item.meta or {}).get("xp3_filter"), dict) for item in items)
    if has_filtered_xp3:
        return False
    if not has_filtered_xp3:
        return False
    for item in items:
        if str((item.meta or {}).get("format") or "") == "kirikiri_psb_scn":
            continue
        translated = str(getattr(item, "translated", "") or "")
        if translated and _kirikiri_text_needs_sjis_tunnel(translated):
            return True
    return False


def _kirikiri_should_use_ascii_placeholder(items: list[TextItem]) -> bool:
    return False


def _kirikiri_placeholder_token(item: TextItem) -> str:
    seed = f"{item.file}\n{item.key}\n{item.line}\n{item.original}".encode("utf-8", errors="ignore")
    digest = hashlib.sha1(seed).hexdigest()[:12].upper()
    return f"GT{digest}"


def _write_kirikiri_placeholder_map(
    game_dir: Path,
    placeholder_map: dict[str, str],
    modified_files: list[Path],
    original_dir: Path,
) -> None:
    if not placeholder_map:
        return
    import base64

    meta_dir = game_dir / "_translation_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# ascii_placeholder\tbase64_utf8_translated"]
    for token, translated in sorted(placeholder_map.items()):
        encoded = base64.b64encode(translated.encode("utf-8")).decode("ascii")
        lines.append(f"{token}\t{encoded}")
    out = meta_dir / "kirikiri_placeholder_map.tsv"
    out.write_text("\n".join(lines) + "\n", encoding="ascii")
    diag = {
        "engine": "kirikiri",
        "strategy": "ascii_placeholder_render_replace",
        "map": "_translation_meta/kirikiri_placeholder_map.tsv",
        "placeholder_count": len(placeholder_map),
        "patched_files": sorted({_norm_rel(path.relative_to(original_dir)) for path in modified_files}),
    }
    (meta_dir / "kirikiri_placeholder_map.json").write_text(
        json.dumps(diag, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    info(f"KiriKiri ASCII placeholder map written: {out.name} ({len(placeholder_map)} entries)")


def _load_kirikiri_sjis_tunnel_encoder(game_dir: Path) -> _KirikiriSjisTunnelEncoder:
    meta_dir = game_dir / "_translation_meta"
    for table_path in (meta_dir / "kirikiri_sjis_ext.bin", game_dir / "kirikiri_sjis_ext.bin"):
        if table_path.exists():
            try:
                return _KirikiriSjisTunnelEncoder(table_path.read_bytes())
            except Exception as exc:
                warning(f"KiriKiri SJIS tunnel table ignored ({table_path.name}): {exc}")
    return _KirikiriSjisTunnelEncoder()


def _write_kirikiri_sjis_tunnel_table(
    game_dir: Path,
    encoder: _KirikiriSjisTunnelEncoder,
    patched_files: list[str],
) -> None:
    table = encoder.mapping_table()
    if not table:
        return
    meta_dir = game_dir / "_translation_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    table_path = meta_dir / "kirikiri_sjis_ext.bin"
    table_path.write_bytes(table)
    diag = {
        "engine": "kirikiri",
        "strategy": "cp932_fullwidth_token_tunnel",
        "table": "_translation_meta/kirikiri_sjis_ext.bin",
        "char_count": len(table) // 2,
        "patched_files": sorted(set(patched_files)),
    }
    (meta_dir / "kirikiri_sjis_tunnel.json").write_text(
        json.dumps(diag, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    info(f"KiriKiri SJIS tunnel table written: {table_path.name} ({len(table) // 2} chars)")


def _decode_kirikiri_sjis_tunnel_text(text: str, table: bytes) -> str:
    if len(table) % 2:
        raise ValueError("KiriKiri SJIS tunnel table length must be even")
    chars = [chr(table[i] | (table[i + 1] << 8)) for i in range(0, len(table), 2)]
    out: list[str] = []
    reverse = {char: idx for idx, char in enumerate(_KIRIKIRI_SJIS_TUNNEL_ALPHABET)}
    i = 0
    while i < len(text):
        if i + 2 < len(text) and text[i] == _KIRIKIRI_SJIS_TUNNEL_PREFIX:
            hi = reverse.get(text[i + 1])
            lo = reverse.get(text[i + 2])
            if hi is not None and lo is not None:
                idx = hi * len(_KIRIKIRI_SJIS_TUNNEL_ALPHABET) + lo
                if idx < len(chars):
                    out.append(chars[idx])
                    i += 3
                    continue
        char = text[i]
        idx = ord(char) - _KIRIKIRI_SJIS_TUNNEL_BASE
        if 0 <= idx < len(chars):
            out.append(chars[idx])
        else:
            out.append(char)
        i += 1
    return "".join(out)


def _kirikiri_patch_text_value(
    item: TextItem,
    sjis_tunnel_encoder: _KirikiriSjisTunnelEncoder | None = None,
    placeholder_map: dict[str, str] | None = None,
) -> str:
    translated = str(getattr(item, "translated", "") or "")
    if placeholder_map is not None and translated:
        token = _kirikiri_placeholder_token(item)
        placeholder_map[token] = translated
        return token
    if sjis_tunnel_encoder is None:
        return translated
    if str((item.meta or {}).get("format") or "") == "kirikiri_psb_scn":
        return translated
    if not _kirikiri_text_needs_sjis_tunnel(translated):
        return translated
    return sjis_tunnel_encoder.encode(translated)


def _read_text_guess(path: Path) -> _DecodedText | None:
    data = path.read_bytes()
    scrambled = _descramble_kirikiri_text(data)
    if scrambled is not None:
        text, mode = scrambled
        return _DecodedText(
            text=_normalize_newlines(text),
            encoding=f"kirikiri-scramble-{mode}",
            newline=_detect_newline(text),
            scramble_mode=mode,
        )

    encodings = ["utf-8-sig"] if data.startswith(b"\xef\xbb\xbf") else ["utf-8"]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.append("utf-16")
    encodings.extend(["cp932", "shift_jis"])
    if "utf-16" not in encodings and _looks_like_utf16(data):
        encodings.append("utf-16")
    for encoding in encodings:
        try:
            text = data.decode(encoding, errors="strict")
            return _DecodedText(text=_normalize_newlines(text), encoding=encoding, newline=_detect_newline(text))
        except UnicodeDecodeError:
            continue
    try:
        text = data.decode("utf-8", errors="ignore")
        return _DecodedText(text=_normalize_newlines(text), encoding="utf-8", newline=_detect_newline(text))
    except Exception:
        return None


def _is_kirikiri_scrambled_text(data: bytes) -> bool:
    return len(data) >= 5 and data[0:2] == b"\xfe\xfe" and data[3:5] == b"\xff\xfe"


def _descramble_kirikiri_text(data: bytes) -> tuple[str, int] | None:
    if not _is_kirikiri_scrambled_text(data):
        return None
    mode = data[2]
    body = data[5:]
    try:
        if mode == 0:
            decoded = _descramble_kirikiri_mode0(body)
        elif mode == 1:
            decoded = _descramble_kirikiri_mode1(body)
        elif mode == 2:
            decoded = _decompress_kirikiri_mode2(body)
        else:
            warning(f"KiriKiri 脚本使用暂不支持的加扰模式: {mode}")
            return None
        return decoded.decode("utf-16le", errors="strict"), mode
    except Exception as exc:
        debug(f"KiriKiri 加扰脚本解码失败 mode={mode}: {exc}")
        return None


def _descramble_kirikiri_mode0(body: bytes) -> bytes:
    data = bytearray(body)
    end = len(data) - (len(data) % 2)
    for i in range(0, end, 2):
        if data[i + 1] == 0 and data[i] < 0x20:
            continue
        data[i + 1] ^= data[i] & 0xFE
        data[i] ^= 1
    return bytes(data)


def _descramble_kirikiri_mode1(body: bytes) -> bytes:
    data = bytearray(body)
    end = len(data) - (len(data) % 2)
    for i in range(0, end, 2):
        c = data[i] | (data[i + 1] << 8)
        c = ((c & 0xAAAA) >> 1) | ((c & 0x5555) << 1)
        data[i] = c & 0xFF
        data[i + 1] = (c >> 8) & 0xFF
    return bytes(data)


def _decompress_kirikiri_mode2(body: bytes) -> bytes:
    if len(body) < 18:
        raise ValueError("mode2 header is too short")
    compressed_length, uncompressed_length = struct.unpack_from("<QQ", body, 0)
    zlib_payload = body[16:16 + compressed_length]
    decoded = zlib.decompress(zlib_payload)
    if len(decoded) != uncompressed_length:
        raise ValueError("mode2 uncompressed size mismatch")
    return decoded


def _looks_like_utf16(data: bytes) -> bool:
    if len(data) < 4:
        return False
    sample = data[: min(len(data), 4096)]
    even_nuls = sample[0::2].count(0)
    odd_nuls = sample[1::2].count(0)
    slots = max(1, len(sample) // 2)
    return even_nuls / slots > 0.25 or odd_nuls / slots > 0.25


def _xor_bytes(data: bytes, key: int) -> bytes:
    key &= 0xFF
    return bytes(byte ^ key for byte in data)


def _detect_xp3_static_filter_schemes(game_dir: Path) -> set[str]:
    schemes: set[str] = set()
    candidates: list[Path] = []
    plugin_dir = game_dir / "plugin"
    for root in (plugin_dir, game_dir):
        if root.exists():
            for pattern in ("*.tpm", "*.dll"):
                candidates.extend(root.glob(pattern))
    for path in candidates:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if (
            b"\\xp3dec\\Release\\xp3dec.pdb" in data
            or (
                b"TVPSetXP3ArchiveExtractionFilter" in data
                and b"Incompatible tTVPXP3ExtractionFilterInfo size" in data
                and b"V2Link" in data
            )
        ):
            schemes.add("koihazi_xp3dec")
    return schemes


def _koihazi_xp3dec_key(file_hash: int) -> bytes:
    eax = int(file_hash) & 0x7FFFFFFF
    eax = (eax | ((eax & 1) << 31)) & 0xFFFFFFFF
    key = bytearray()
    for _ in range(31):
        key.append(eax & 0xFF)
        eax = ((eax >> 8) | ((eax & 0x1FE) << 23)) & 0xFFFFFFFF
    return bytes(key)


def _apply_koihazi_xp3dec_filter(data: bytes, file_hash: int, offset: int = 0) -> bytes:
    key = _koihazi_xp3dec_key(file_hash)
    if not key:
        return data
    start = int(offset) % len(key)
    return bytes(byte ^ key[(start + idx) % len(key)] for idx, byte in enumerate(data))


def _try_decode_xp3_single_byte_xor_filter(data: bytes) -> _Xp3FilterDecode | None:
    if len(data) < 8:
        return None
    keys: list[int] = []
    for bom in (b"\xff\xfe", b"\xfe\xff", b"\xef\xbb"):
        key = data[0] ^ bom[0]
        if key not in keys and all((data[idx] ^ key) == bom[idx] for idx in range(min(len(bom), len(data)))):
            keys.append(key)
    for key in keys:
        decoded_data = _xor_bytes(data, key)
        decoded = _decode_script_bytes_for_quality(decoded_data)
        if decoded is None:
            continue
        if _is_probably_protected_text_script(decoded_data) and not _looks_like_kirikiri_script_text(decoded.text):
            continue
        return _Xp3FilterDecode(data=decoded_data, key=key, decoded=decoded)
    return None


def _looks_like_kirikiri_script_text(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 8:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    sample = lines[:200]
    markers = sum(1 for line in sample if line.startswith(("@", "[", "*", ";", "#", "//", "/*", "*/")))
    spans = sum(len(_extract_kirikiri_text_spans(line, allow_plain_kag=True)) for line in sample)
    tjs_hints = sum(1 for line in sample if "function" in line or "var " in line or "class " in line or "=>" in line)
    return markers >= 2 or spans > 0 or tjs_hints >= 2


def _decode_xp3_script_filter_for_static_extract(
    data: bytes,
    *,
    entry: _Xp3Entry | None = None,
    schemes: set[str] | None = None,
) -> tuple[bytes, dict[str, object] | None]:
    schemes = schemes or set()
    if entry is not None and entry.adler is not None and "koihazi_xp3dec" in schemes:
        decoded_data = _apply_koihazi_xp3dec_filter(data, entry.adler)
        decoded = _decode_script_bytes_for_quality(decoded_data)
        if decoded is not None and (
            is_kirikiri_scn(decoded_data)
            or _looks_like_kirikiri_script_text(decoded.text)
            or not _is_probably_protected_text_script(decoded_data)
        ):
            return decoded_data, {
                "kind": "koihazi_xp3dec",
                "file_hash": entry.adler,
                "encoding": "kirikiri-koihazi-xp3dec",
            }

    filtered = _try_decode_xp3_single_byte_xor_filter(data)
    if filtered is None:
        return data, None
    return filtered.data, {
        "kind": "single_byte_xor",
        "key": filtered.key,
        "encoding": f"kirikiri-xor-{filtered.key}",
    }


def _write_text(path: Path, text: str, encoding: str) -> None:
    scramble_match = re.fullmatch(r"kirikiri-scramble-(\d+)", encoding)
    if scramble_match:
        mode = int(scramble_match.group(1))
        path.write_bytes(_scramble_kirikiri_text(text, mode))
        return

    xor_match = re.fullmatch(r"kirikiri-xor-(\d+)", encoding)
    if xor_match:
        key = int(xor_match.group(1))
        path.write_bytes(_xor_bytes(text.encode("utf-16"), key))
        return

    enc = "utf-8-sig" if encoding == "utf-8-sig" else encoding
    try:
        path.write_text(text, encoding=enc, newline="")
    except UnicodeEncodeError:
        warning(f"KiriKiri 原编码 {encoding} 无法容纳译文，改用 UTF-16 BOM 写入: {path.name}")
        path.write_text(text, encoding="utf-16", newline="")


def _scramble_kirikiri_text(text: str, mode: int) -> bytes:
    utf16 = text.encode("utf-16le")
    if mode == 0:
        body = _scramble_kirikiri_mode0(utf16)
    elif mode == 1:
        body = _scramble_kirikiri_mode1(utf16)
    elif mode == 2:
        body = _compress_kirikiri_mode2(utf16)
    else:
        raise ValueError(f"unsupported KiriKiri scramble mode: {mode}")
    return b"\xfe\xfe" + bytes([mode]) + b"\xff\xfe" + body


def _scramble_kirikiri_mode0(body: bytes) -> bytes:
    data = bytearray(body)
    end = len(data) - (len(data) % 2)
    for i in range(0, end, 2):
        if data[i + 1] == 0 and data[i] < 0x20:
            continue
        data[i] ^= 1
        data[i + 1] ^= data[i] & 0xFE
    return bytes(data)


def _scramble_kirikiri_mode1(body: bytes) -> bytes:
    return _descramble_kirikiri_mode1(body)


def _compress_kirikiri_mode2(body: bytes) -> bytes:
    payload = zlib.compress(body, level=6)
    return struct.pack("<QQ", len(payload), len(body)) + payload


def _detect_newline(text: str) -> str:
    rn = text.count("\r\n")
    n = text.count("\n") - rn
    r = text.count("\r") - rn
    if rn >= n and rn >= r and rn > 0:
        return "\r\n"
    if r > n:
        return "\r"
    return "\n"


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _norm_rel(path: Path | str) -> str:
    return str(path).replace("\\", "/")


def _safe_output_path(root: Path, entry_name: str) -> Path | None:
    rel = entry_name.replace("\\", "/").lstrip("/")
    if not rel or rel.startswith("../") or "/../" in rel:
        return None
    try:
        out = (root / rel).resolve()
        resolved_root = root.resolve()
        out.relative_to(resolved_root)
        return out
    except Exception:
        return None


def _is_probably_binary_script(data: bytes) -> bool:
    if _is_kirikiri_scrambled_text(data):
        return False
    if data.startswith((b"TJS2", b"\x00T\x00J\x00S\x002")):
        return True
    sample = data[: min(len(data), 4096)]
    if not sample:
        return True
    nul_ratio = sample.count(0) / len(sample)
    return nul_ratio > 0.20 and not _looks_like_utf16(sample)


def _is_probably_protected_text_script(data: bytes) -> bool:
    decoded = _decode_script_bytes_for_quality(data)
    if decoded is None:
        return True
    text = decoded.text
    stripped = text.strip()
    if not stripped:
        return True
    if len(stripped) < 16:
        return False

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    sample_lines = lines[:200]
    if not sample_lines:
        return True
    text_sample = "\n".join(sample_lines)
    kana_count = sum(1 for ch in text_sample if "\u3040" <= ch <= "\u30ff" or "\uff66" <= ch <= "\uff9f")
    cjk_count = sum(1 for ch in text_sample if "\u3400" <= ch <= "\u9fff")
    latin_count = sum(1 for ch in text_sample if "A" <= ch <= "Z" or "a" <= ch <= "z")
    replacement_count = text_sample.count("\ufffd") + text_sample.count("?")
    control_count = sum(1 for ch in text_sample if ord(ch) < 32 and ch not in "\n\r\t")
    printable_count = sum(1 for ch in text_sample if ch.isprintable() and not ch.isspace())
    kag_markers = sum(1 for line in sample_lines if line.startswith(("@", "[", "*", ";", "#")))
    text_spans = 0
    for line in sample_lines:
        text_spans += len(_extract_kirikiri_text_spans(line))

    natural_chars = kana_count + cjk_count + latin_count
    if control_count:
        return True
    if printable_count >= 20 and natural_chars / max(1, printable_count) < 0.35:
        return True
    if replacement_count >= 4 and replacement_count / max(1, printable_count) > 0.15:
        return True
    if kag_markers == 0 and text_spans == 0 and len(sample_lines) >= 3:
        return True
    return False


def _is_probably_scriptless_kirikiri_payload(path: Path) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if is_kirikiri_scn(data):
        return True
    decoded = _decode_script_bytes_for_quality(data)
    if decoded is None:
        return False
    text = decoded.text
    if len(text.strip()) < 8:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    sample = lines[:200]
    kag_markers = sum(1 for line in sample if line.startswith(("@", "[", "*", ";", "#")))
    spans = sum(len(_extract_kirikiri_text_spans(line, allow_plain_kag=True)) for line in sample)
    return kag_markers > 0 or spans > 0


def _decode_script_bytes_for_quality(data: bytes) -> _DecodedText | None:
    scrambled = _descramble_kirikiri_text(data)
    if scrambled is not None:
        text, mode = scrambled
        return _DecodedText(
            text=_normalize_newlines(text),
            encoding=f"kirikiri-scramble-{mode}",
            newline=_detect_newline(text),
            scramble_mode=mode,
        )

    encodings = ["utf-8-sig"] if data.startswith(b"\xef\xbb\xbf") else ["utf-8"]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or _looks_like_utf16(data):
        encodings.append("utf-16")
    encodings.extend(["cp932", "shift_jis"])
    seen: set[str] = set()
    for encoding in encodings:
        if encoding in seen:
            continue
        seen.add(encoding)
        try:
            text = data.decode(encoding, errors="strict")
            return _DecodedText(text=_normalize_newlines(text), encoding=encoding, newline=_detect_newline(text))
        except UnicodeDecodeError:
            continue
    return None


def _xp3_may_contain_script_markers(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            while True:
                data = f.read(1024 * 1024)
                if not data:
                    return False
                if any(marker in data for marker in (b".ks", b".tjs", b"TJS2")):
                    return True
    except Exception:
        return False


def _count_files(path: Path) -> int:
    try:
        return sum(1 for p in path.rglob("*") if p.is_file())
    except Exception:
        return 0


def _count_kirikiri_script_files(path: Path) -> int:
    try:
        files = [p for p in path.rglob("*") if p.is_file()]
    except Exception:
        return 0
    count = 0
    for file_path in files:
        suffix = file_path.suffix.lower()
        if suffix in {".ks", ".tjs", ".scn"}:
            count += 1
        elif not suffix and _is_probably_scriptless_kirikiri_payload(file_path):
            count += 1
    return count
