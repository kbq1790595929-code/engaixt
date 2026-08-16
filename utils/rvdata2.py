"""Ruby Marshal (.rvdata2) parser for RPG Maker VX Ace.

Extracts and replaces translatable strings from Ruby serialized data.
Format spec: Ruby Marshal 4.8

Type bytes:
  '0' = nil      'T' = true      'F' = false
  'i' = fixnum   'l' = bignum    'f' = float
  ':' = symbol   '"' = string    'I' = string (with encoding/ivars)
  '[' = array    '{' = hash      'o' = object (class + ivars)
  'u' = userdef  'U' = usermarshal  'C' = userclass
  ';' = symbol link   '@' = object link
  'e' = extended
"""

import struct
import re
from pathlib import Path


# Fields that reference image filenames (must NOT be translated)
FILENAME_IVARS = {
    "@character_name",
    "@battler_name",
    "@tileset_name",
    "@panorama_name",
    "@parallax_name",
    "@title1_name",
    "@title2_name",
    "@battleback_name",
    "@icon_name",
}

# Resource filenames that must not be translated when they appear as values
# (e.g., picture filenames in event commands, audio filenames)
_RESOURCE_NAMES: set[str] = set()


def set_resource_names(names: set[str]):
    """Set the resource filenames to protect from translation."""
    global _RESOURCE_NAMES
    _RESOURCE_NAMES = names

# Regex for Japanese text detection
JP_RE = re.compile(r"[぀-ゟ゠-ヿ一-鿿豈-﫿]+")


def is_translatable(text: str) -> bool:
    """Check if text contains Japanese characters and is worth translating."""
    if not text or len(text) < 2:
        return False
    return bool(JP_RE.search(text))


class MarshalStream:
    """Read Ruby Marshal data from bytes."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.symbols: list[str] = []  # symbol table for links

    def read_byte(self) -> int:
        b = self.data[self.pos]
        self.pos += 1
        return b

    def read_long(self) -> int:
        """Read a Marshal long (variable-length integer)."""
        b = self.read_byte()
        if b == 0:
            return 0
        if 5 < b < 128:  # 1-123 stored as n+5
            return b - 5
        if b == 1:  # 1 byte follows
            return self.read_byte()
        if b == 2:  # 2 bytes LE
            lo = self.read_byte()
            hi = self.read_byte()
            return lo | (hi << 8)
        if b == 3:  # 3 bytes LE
            b0 = self.read_byte()
            b1 = self.read_byte()
            b2 = self.read_byte()
            return b0 | (b1 << 8) | (b2 << 16)
        if b == 4:  # 4 bytes LE
            return struct.unpack_from("<I", self.data, self.pos - 1)[0] >> 8
        if b == 255:  # negative step
            neg_step = self.read_byte()
            return -(256 - neg_step)
        # For larger values
        return b

    def read_bytes(self, n: int) -> bytes:
        result = self.data[self.pos:self.pos + n]
        self.pos += n
        return result

    def read_type(self) -> str:
        return chr(self.read_byte())


def extract_strings_from_marshal(data: bytes) -> list[str]:
    """Extract all translatable strings from Ruby Marshal data."""
    try:
        stream = MarshalStream(data)
        _verify_version(stream)
        strings = []
        _read_value(stream, strings)
        return strings
    except Exception:
        return []


def replace_strings_in_marshal(data: bytes, translations: dict[str, str]) -> tuple[bytes, list[str]]:
    """Replace translatable strings using direct binary find-and-replace.

    Tries both UTF-8 and Shift-JIS encodings for finding source strings,
    since RPG Maker VX Ace data may use either encoding.

    Returns (modified_data, skipped_sources) where skipped_sources lists
    translations that couldn't be applied (e.g., target longer than source).
    """
    raw = bytearray(data)
    skipped: list[str] = []
    for src, dst in translations.items():
        dst_bytes = dst.encode("utf-8")
        found_any = False
        for encoding in ("utf-8", "shift-jis", "cp932"):
            try:
                src_bytes = src.encode(encoding)
            except UnicodeEncodeError:
                continue
            if len(dst_bytes) > len(src_bytes):
                continue
            padded_dst = dst_bytes + b" " * (len(src_bytes) - len(dst_bytes))
            pos = 0
            while True:
                idx = raw.find(src_bytes, pos)
                if idx < 0:
                    break
                raw[idx:idx + len(src_bytes)] = padded_dst
                found_any = True
                pos = idx + len(src_bytes)
            if found_any:
                break
        if not found_any:
            skipped.append(src)
    return bytes(raw), skipped


def replace_strings_in_marshal_full(data: bytes, translations: dict[str, str]) -> bytes:
    """Replace translatable strings using proper Marshal re-serialization.

    Unlike binary replace, this handles translations of ANY length by
    rewriting the entire Marshal structure with correct length prefixes.

    Returns the modified Marshal data.
    """
    stream = MarshalStream(data)
    _verify_version(stream)
    output = bytearray(b"\x04\x08")
    _read_and_replace(stream, output, translations)
    return bytes(output)


def replace_any_strings_in_marshal_full(data: bytes, translations: dict[str, str]) -> bytes:
    """Replace any string key present in translations, without language filtering."""
    stream = MarshalStream(data)
    _verify_version(stream)
    output = bytearray(b"\x04\x08")
    _read_and_replace(stream, output, translations, only_translatable=False)
    return bytes(output)


def _verify_version(stream: MarshalStream):
    major = stream.read_byte()
    minor = stream.read_byte()
    if major != 4 or minor != 8:
        raise ValueError(f"Unsupported Marshal version: {major}.{minor}")


def _read_value(stream: MarshalStream, strings: list[str], skip: bool = False):
    """Read a single value, collecting translatable strings.

    When skip=True, strings are not collected (used for filename fields).
    """
    type_byte = stream.read_type()

    if type_byte == '0':  # nil
        pass
    elif type_byte == 'T':  # true
        pass
    elif type_byte == 'F':  # false
        pass
    elif type_byte == 'i':  # fixnum
        _read_fixnum(stream)
    elif type_byte == 'l':  # bignum
        _read_bignum(stream)
    elif type_byte == 'f':  # float
        stream.read_bytes(stream.read_long())  # float as string
    elif type_byte == ':':  # symbol
        length = stream.read_long()
        raw = stream.read_bytes(length)
        try:
            stream.symbols.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            stream.symbols.append(raw.decode("latin-1"))
    elif type_byte == ';':  # symbol link
        stream.read_long()  # index
    elif type_byte == '"':  # string
        length = stream.read_long()
        raw = stream.read_bytes(length)
        if not skip:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("shift-jis", errors="replace")
            if is_translatable(text):
                strings.append(text)
    elif type_byte == 'I':  # string with encoding/ivars (wraps a regular string)
        type_byte2 = stream.read_type()
        if type_byte2 == '"':
            length = stream.read_long()
            raw = stream.read_bytes(length)
            if not skip:
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("shift-jis", errors="replace")
                if is_translatable(text):
                    strings.append(text)
        else:
            # Fallback: not a regular string, skip
            pass
        # Read ivars count and skip ivars
        ivar_count = stream.read_long()
        for _ in range(ivar_count):
            _read_value(stream, strings, skip)
            _read_value(stream, strings, skip)
    elif type_byte == '[':  # array
        count = stream.read_long()
        for _ in range(count):
            _read_value(stream, strings, skip)
    elif type_byte == '{':  # hash
        count = stream.read_long()
        for _ in range(count):
            _read_value(stream, strings, skip)  # key
            _read_value(stream, strings, skip)  # value
    elif type_byte == 'o':  # object
        # Read class name (symbol)
        _read_value(stream, strings, skip)
        # Read instance variables
        ivar_count = stream.read_long()
        for _ in range(ivar_count):
            # Read ivar name to check if it's a filename field
            ivar_name = _read_symbol_name(stream)
            value_skip = skip or (ivar_name in FILENAME_IVARS)
            _read_value(stream, strings, value_skip)  # value
    elif type_byte == 'C':  # userclass
        _read_value(stream, strings, skip)  # class name
        _read_value(stream, strings, skip)  # wrapped value
    elif type_byte == 'u':  # userdef
        _read_value(stream, strings, skip)  # class name
        data_len = stream.read_long()
        stream.read_bytes(data_len)
    elif type_byte == 'U':  # usermarshal
        _read_value(stream, strings, skip)  # class name
        _read_value(stream, strings, skip)  # marshaled data
    elif type_byte == 'e':  # extended
        _read_value(stream, strings, skip)  # module name
        _read_value(stream, strings, skip)  # wrapped value
    elif type_byte == '@':  # object link
        stream.read_long()  # index
    else:
        raise ValueError(f"Unknown type byte: 0x{ord(type_byte):02X} at offset {stream.pos - 1}")


def _read_fixnum(stream: MarshalStream) -> int:
    """Read a fixnum from the stream."""
    b = stream.read_byte()
    if b == 0:
        return 0
    if 5 < b < 128:
        return b - 5
    if b == 1:
        return stream.read_byte()
    if b == 2:
        return stream.read_byte() | (stream.read_byte() << 8)
    if b == 3:
        return stream.read_byte() | (stream.read_byte() << 8) | (stream.read_byte() << 16)
    if b == 4:
        return struct.unpack_from("<I", stream.data, stream.pos - 1)[0] >> 8
    # Negative
    if 252 > b > 128:
        return b - 256
    return b


def _read_bignum(stream: MarshalStream) -> int:
    sign = stream.read_byte()
    if sign not in (ord('+'), ord('-')):
        raise ValueError(f"Invalid bignum sign: {sign}")
    length = stream.read_long() * 2
    raw = stream.read_bytes(length)
    val = int.from_bytes(raw, "little", signed=(sign == ord('-')))
    return val


# ---- Replace mode ----

def _read_and_replace(stream: MarshalStream, output: bytearray, translations: dict[str, str],
                       skip: bool = False, only_translatable: bool = True):
    """Read a value and write it back, replacing translatable strings.

    When skip=True, strings are copied verbatim (used for filename fields).
    """
    type_byte = stream.read_type()
    output.append(ord(type_byte))

    if type_byte in ('0', 'T', 'F'):
        pass  # already written type byte

    elif type_byte == 'i':
        _copy_fixnum(stream, output)

    elif type_byte == 'l':
        _copy_bignum(stream, output)

    elif type_byte == 'f':
        length = stream.read_long()
        _write_long(output, length)
        output.extend(stream.read_bytes(length))

    elif type_byte == ':':
        length = stream.read_long()
        raw = stream.read_bytes(length)
        _write_long(output, length)
        output.extend(raw)
        try:
            stream.symbols.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            stream.symbols.append(raw.decode("latin-1"))

    elif type_byte == ';':
        idx = stream.read_long()
        _write_long(output, idx)

    elif type_byte == '"':
        length = stream.read_long()
        raw = stream.read_bytes(length)
        if skip:
            _write_long(output, length)
            output.extend(raw)
        else:
            _write_replaced_string(output, raw, length, translations, only_translatable)

    elif type_byte == 'I':
        # I-string wraps a regular string + encoding ivars
        type_byte2 = stream.read_type()
        output.append(ord(type_byte2))
        if type_byte2 == '"':
            length = stream.read_long()
            raw = stream.read_bytes(length)
            if skip:
                _write_long(output, length)
                output.extend(raw)
            else:
                _write_replaced_string(output, raw, length, translations, only_translatable)
        else:
            pass
        # Copy ivars
        ivar_count = stream.read_long()
        _write_long(output, ivar_count)
        for _ in range(ivar_count):
            _read_and_replace(stream, output, translations, skip, only_translatable)
            _read_and_replace(stream, output, translations, skip, only_translatable)

    elif type_byte == '[':
        count = stream.read_long()
        _write_long(output, count)
        for _ in range(count):
            _read_and_replace(stream, output, translations, skip, only_translatable)

    elif type_byte == '{':
        count = stream.read_long()
        _write_long(output, count)
        for _ in range(count):
            _read_and_replace(stream, output, translations, skip, only_translatable)
            _read_and_replace(stream, output, translations, skip, only_translatable)

    elif type_byte == 'o':
        _read_and_replace(stream, output, translations, skip, only_translatable)  # class name
        ivar_count = stream.read_long()
        _write_long(output, ivar_count)
        for _ in range(ivar_count):
            # Read ivar name symbol and write to output
            ivar_name = _read_and_write_symbol(stream, output)
            value_skip = skip or (ivar_name in FILENAME_IVARS)
            _read_and_replace(stream, output, translations, value_skip, only_translatable)

    elif type_byte == 'C':
        _read_and_replace(stream, output, translations, skip, only_translatable)
        _read_and_replace(stream, output, translations, skip, only_translatable)

    elif type_byte in ('u', 'U'):
        _read_and_replace(stream, output, translations, skip, only_translatable)
        if type_byte == 'u':
            data_len = stream.read_long()
            _write_long(output, data_len)
            output.extend(stream.read_bytes(data_len))
        else:
            _read_and_replace(stream, output, translations, skip, only_translatable)

    elif type_byte == 'e':
        _read_and_replace(stream, output, translations, skip, only_translatable)
        _read_and_replace(stream, output, translations, skip, only_translatable)

    elif type_byte == '@':
        idx = stream.read_long()
        _write_long(output, idx)

    else:
        raise ValueError(f"Unknown type byte: 0x{ord(type_byte):02X}")


def _write_replaced_string(output: bytearray, raw: bytes, original_length: int,
                           translations: dict[str, str], only_translatable: bool = True):
    """Write a string, replacing it if a translation exists.

    Skips translation if the string is a known resource filename
    (e.g., picture filenames used in event commands).
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("shift-jis")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

    if (not only_translatable or is_translatable(text)) and text in translations and text not in _RESOURCE_NAMES:
        new_text = translations[text]
        new_bytes = new_text.encode("utf-8")
        _write_long(output, len(new_bytes))
        output.extend(new_bytes)
    else:
        _write_long(output, original_length)
        output.extend(raw)


def _write_long(output: bytearray, value: int):
    """Write a Marshal long (variable-length integer)."""
    if value == 0:
        output.append(0)
    elif 0 < value < 123:
        output.append(value + 5)
    elif value < 256:
        output.append(1)
        output.append(value)
    elif value < 65536:
        output.append(2)
        output.append(value & 0xFF)
        output.append((value >> 8) & 0xFF)
    elif value < 16777216:
        output.append(3)
        output.append(value & 0xFF)
        output.append((value >> 8) & 0xFF)
        output.append((value >> 16) & 0xFF)
    else:
        b = value.to_bytes(4, "little")
        output.append(4)
        output.extend(b[:4])
    # Note: simplified - doesn't handle negative values or very large values


def _copy_fixnum(stream: MarshalStream, output: bytearray):
    """Copy a fixnum without modifying."""
    b = stream.read_byte()
    output.append(b)
    if b == 0:
        pass
    elif 5 < b < 128:
        pass
    elif b == 1:
        output.append(stream.read_byte())
    elif b == 2:
        output.append(stream.read_byte())
        output.append(stream.read_byte())
    elif b == 3:
        output.append(stream.read_byte())
        output.append(stream.read_byte())
        output.append(stream.read_byte())
    elif b == 4:
        output.append(stream.read_byte())
        output.append(stream.read_byte())
        output.append(stream.read_byte())
        output.append(stream.read_byte())
    # else: negative or special - just copy what we have


def _copy_bignum(stream: MarshalStream, output: bytearray):
    sign = stream.read_byte()
    output.append(sign)
    length = stream.read_long() * 2
    _write_long(output, length // 2)
    output.extend(stream.read_bytes(length))


def _read_and_write_symbol(stream: MarshalStream, output: bytearray) -> str | None:
    """Read a symbol, write it to output, and return its name."""
    type_byte = stream.read_type()
    output.append(ord(type_byte))

    if type_byte == ':':
        length = stream.read_long()
        raw = stream.read_bytes(length)
        _write_long(output, length)
        output.extend(raw)
        try:
            name = raw.decode("utf-8")
        except UnicodeDecodeError:
            name = raw.decode("latin-1")
        stream.symbols.append(name)
        return name
    elif type_byte == ';':
        idx = stream.read_long()
        _write_long(output, idx)
        if idx < len(stream.symbols):
            return stream.symbols[idx]
        return None
    return None


def _read_symbol_name(stream: MarshalStream) -> str | None:
    """Read a symbol from stream and return its name (for extraction, no output)."""
    type_byte = stream.read_type()
    if type_byte == ':':
        length = stream.read_long()
        raw = stream.read_bytes(length)
        try:
            name = raw.decode("utf-8")
        except UnicodeDecodeError:
            name = raw.decode("latin-1")
        stream.symbols.append(name)
        return name
    elif type_byte == ';':
        idx = stream.read_long()
        if idx < len(stream.symbols):
            return stream.symbols[idx]
        return None
    return None
