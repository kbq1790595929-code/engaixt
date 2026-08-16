from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any


PSB_SIGNATURE = b"PSB\0"
_KNOWN_PSB_KEYS = (970396437,)


@dataclass(frozen=True)
class PsbTextRef:
    index: int
    text: str
    kind: str
    path: str


@dataclass(frozen=True)
class PsbPatchStats:
    patched: int = 0
    skipped: int = 0
    conflicts: int = 0
    unsupported: bool = False


@dataclass(frozen=True)
class PsbScnExtractResult:
    texts: list[PsbTextRef]
    storage_refs: list[str]


@dataclass
class _PsbString:
    index: int
    value: str
    offset: int


@dataclass(frozen=True)
class _PsbArray:
    count: int
    elem_size: int
    data_offset: int
    array_size: int


@dataclass(frozen=True)
class _PsbChunk:
    offset: int
    length: int


class PsbFormatError(ValueError):
    pass


class _PsbReader:
    def __init__(self, data: bytes):
        self.data = bytearray(data)
        self.version = 0
        self.flags = 0
        self.names = 0
        self.strings = 0
        self.strings_data = 0
        self.chunk_offsets = 0
        self.chunk_lengths = 0
        self.chunk_data = 0
        self.extra_offsets = 0
        self.extra_lengths = 0
        self.extra_data = 0
        self.root_offset = 0
        self.header_start = 8
        self.header_size = 0
        self.name_map: dict[int, str] = {}
        self.root: Any = None
        self._array_cache: dict[int, _PsbArray] = {}
        self._string_values_cache: list[tuple[str, int]] | None = None

    def parse(self) -> None:
        if not self.data.startswith(PSB_SIGNATURE):
            raise PsbFormatError("not a PSB file")
        if self._parse(encrypted=False):
            return
        for key in _KNOWN_PSB_KEYS:
            trial = _PsbReader(bytes(self.data))
            trial._set_key(key)
            if trial._parse(encrypted=True):
                self.__dict__.update(trial.__dict__)
                return
        raise PsbFormatError("unsupported or encrypted PSB")

    def _set_key(self, key: int) -> None:
        self._key = [0x075BCD15, 0x159A55E5, 0x1F123BB5, key, 0, 0]

    def _parse(self, *, encrypted: bool) -> bool:
        try:
            self.version, self.flags = struct.unpack_from("<HH", self.data, 4)
            if encrypted and self.version < 3:
                self.flags = 2
            self.header_size = 0x30 if self.version > 3 else 0x20
            header = bytearray(self.data[self.header_start:self.header_start + self.header_size])
            if len(header) != self.header_size:
                return False
            if encrypted and (self.flags & 1):
                if self.version > 3:
                    self._decrypt(header, 0, 0x24)
                    self._decrypt(header, 0x24, 0x0C)
                else:
                    self._decrypt(header, 0, 0x20)
            self.names = _u32(header, 0x04)
            self.strings = _u32(header, 0x08)
            self.strings_data = _u32(header, 0x0C)
            self.chunk_offsets = _u32(header, 0x10)
            self.chunk_lengths = _u32(header, 0x14)
            self.chunk_data = _u32(header, 0x18)
            self.root_offset = _u32(header, 0x1C)
            if self.version > 3:
                self.extra_offsets = _u32(header, 0x24)
                self.extra_lengths = _u32(header, 0x28)
                self.extra_data = _u32(header, 0x2C)
            if not self._header_offsets_are_valid():
                return False
            if encrypted and (self.flags & 2):
                self._decrypt(self.data, self.names, self.chunk_offsets - self.names)
            if self.data[self.root_offset] != 0x21:
                return False
            self.name_map = self._read_names()
            return True
        except Exception:
            return False

    def _header_offsets_are_valid(self) -> bool:
        size = len(self.data)
        min_offset = 0x28
        offsets = [
            self.names,
            self.strings,
            self.strings_data,
            self.chunk_offsets,
            self.chunk_lengths,
            self.chunk_data,
            self.root_offset,
        ]
        return (
            self.version >= 2
            and all(min_offset <= value <= size for value in offsets)
            and self.names < self.chunk_data
            and self.strings < self.chunk_data
            and self.strings_data < self.chunk_data
            and self.chunk_offsets < self.chunk_data
            and self.chunk_lengths < self.chunk_data
            and self.root_offset < self.chunk_data
        )

    def _read_names(self) -> dict[int, str]:
        nm1 = self._get_array(self.names)
        nm2 = self._get_array(self.names + nm1.array_size)
        lookup: dict[int, bytes] = {0: b""}
        next_lookup: dict[int, bytes] = {}
        result: dict[int, str] = {}
        while lookup:
            for prefix_index, prefix in lookup.items():
                first = self._get_array_elem(nm1, prefix_index)
                for value in range(256):
                    idx = value + first
                    if idx >= nm2.count:
                        break
                    if self._get_array_elem(nm2, idx) != prefix_index:
                        continue
                    if value == 0:
                        result[self._get_array_elem(nm1, idx)] = prefix.decode("utf-8", errors="replace")
                    else:
                        next_lookup[idx] = prefix + bytes([value])
            lookup, next_lookup = next_lookup, {}
        return result

    def _get_array(self, offset: int) -> _PsbArray:
        cached = self._array_cache.get(offset)
        if cached is not None:
            return cached
        data_offset = self.data[offset] - 10
        count = self._get_integer(offset, 0x0C)
        elem_size = self.data[offset + data_offset - 1] - 12
        if count == 0:
            elem_size = 1
        if elem_size not in (1, 2, 3, 4):
            raise PsbFormatError("invalid PSB array element size")
        array_size = count * elem_size + data_offset
        result = _PsbArray(count=count, elem_size=elem_size, data_offset=offset + data_offset, array_size=array_size)
        self._array_cache[offset] = result
        return result

    def _get_array_elem(self, array: _PsbArray, index: int) -> int:
        offset = array.data_offset + index * array.elem_size
        return _read_int(self.data, offset, array.elem_size, signed=False)

    def _set_array_elem(self, array: _PsbArray, index: int, value: int) -> None:
        offset = array.data_offset + index * array.elem_size
        max_value = (1 << (array.elem_size * 8)) - 1
        if value < 0 or value > max_value:
            raise PsbFormatError("PSB string table grew beyond offset array capacity")
        _write_int(self.data, offset, array.elem_size, value)

    def _get_object(self, offset: int) -> Any:
        tag = self.data[offset]
        if tag == 1:
            return None
        if tag == 2:
            return True
        if tag == 3:
            return False
        if 4 <= tag <= 8:
            return self._get_integer(offset, 4)
        if 9 <= tag <= 0x0C:
            return self._get_long(offset)
        if 0x15 <= tag <= 0x18:
            return self._get_string(offset)
        if 0x19 <= tag <= 0x1C:
            return self._get_chunk(offset, extra=False)
        if tag in (0x1D, 0x1E):
            return self._get_float(offset)
        if tag == 0x1F:
            return self._get_double(offset)
        if tag == 0x20:
            return self._get_list(offset)
        if tag == 0x21:
            return self._get_dict(offset)
        if 0x22 <= tag <= 0x25:
            return self._get_chunk(offset, extra=True)
        raise PsbFormatError(f"unknown PSB object type 0x{tag:02X}")

    def _get_integer(self, offset: int, base_type: int) -> int:
        size = self.data[offset] - base_type
        if size <= 0:
            return 0
        if size > 4:
            raise PsbFormatError("invalid PSB integer size")
        return _read_int(self.data, offset + 1, size, signed=False)

    def _get_long(self, offset: int) -> int:
        tag = self.data[offset]
        if tag == 0x09:
            low = _read_int(self.data, offset + 1, 4, signed=False)
            high = struct.unpack_from("<b", self.data, offset + 5)[0]
            return low | (high << 32)
        if tag == 0x0A:
            low = _read_int(self.data, offset + 1, 4, signed=False)
            high = struct.unpack_from("<h", self.data, offset + 5)[0]
            return low | (high << 32)
        if tag == 0x0B:
            low = _read_int(self.data, offset + 1, 4, signed=False)
            mid = _read_int(self.data, offset + 5, 2, signed=False)
            high = struct.unpack_from("<b", self.data, offset + 7)[0]
            return low | (mid << 32) | (high << 48)
        if tag == 0x0C:
            return struct.unpack_from("<q", self.data, offset + 1)[0]
        return 0

    def _get_string(self, offset: int) -> _PsbString:
        index = self._get_integer(offset, 0x14)
        strings = self._get_string_values()
        if index < 0 or index >= len(strings):
            raise PsbFormatError("PSB string index out of range")
        value, data_offset = strings[index]
        return _PsbString(index=index, value=value, offset=data_offset)

    def _get_string_values(self) -> list[tuple[str, int]]:
        if self._string_values_cache is not None:
            return self._string_values_cache
        array = self._get_array(self.strings)
        values: list[tuple[str, int]] = []
        for idx in range(array.count):
            data_offset = self.strings_data + self._get_array_elem(array, idx)
            values.append((_read_cstring(self.data, data_offset), data_offset))
        self._string_values_cache = values
        return values

    def get_root_key(self, key: str) -> Any:
        value_offset = self._get_dict_value_offset(self.root_offset, key)
        if value_offset is None:
            return None
        return self._get_object(value_offset)

    def _get_dict_value_offset(self, dict_offset: int, key: str) -> int | None:
        name_offset = self._get_name_offset(key)
        if name_offset is None:
            return None
        array_offset = dict_offset + 1
        keys = self._get_array(array_offset)
        if keys.count == 0:
            return None

        lower = 0
        upper = keys.count
        key_index = 0
        while lower < upper:
            key_index = (upper + lower) >> 1
            candidate = self._get_array_elem(keys, key_index)
            if candidate == name_offset:
                break
            if candidate >= name_offset:
                upper = (upper + lower) >> 1
            else:
                lower = key_index + 1
        if lower >= upper:
            return None

        values = self._get_array(array_offset + keys.array_size)
        value_delta = self._get_array_elem(values, key_index)
        return array_offset + keys.array_size + values.array_size + value_delta

    def _get_name_offset(self, name: str) -> int | None:
        nm1 = self._get_array(self.names)
        nm2 = self._get_array(self.names + nm1.array_size)
        idx = 0
        for name_idx in range(len(name) + 1):
            symbol = ord(name[name_idx]) if name_idx < len(name) else 0
            prev_idx = idx
            idx = symbol + self._get_array_elem(nm1, idx)
            if idx >= nm1.count or self._get_array_elem(nm2, idx) != prev_idx:
                break
            if name_idx >= len(name):
                return self._get_array_elem(nm1, idx)
        return None

    def _get_list(self, offset: int) -> list[Any]:
        array_offset = offset + 1
        array = self._get_array(array_offset)
        base = array_offset + array.array_size
        return [self._get_object(base + self._get_array_elem(array, idx)) for idx in range(array.count)]

    def _get_dict(self, offset: int) -> dict[str, Any]:
        array_offset = offset + 1
        keys = self._get_array(array_offset)
        values = self._get_array(array_offset + keys.array_size)
        base = array_offset + keys.array_size + values.array_size
        result: dict[str, Any] = {}
        for idx in range(keys.count):
            key_id = self._get_array_elem(keys, idx)
            key = self.name_map.get(key_id, str(key_id))
            result[key] = self._get_object(base + self._get_array_elem(values, idx))
        return result

    def _get_chunk(self, offset: int, *, extra: bool) -> _PsbChunk:
        base_type = 0x21 if extra else 0x18
        chunk_index = self._get_integer(offset, base_type)
        offsets = self._get_array(self.extra_offsets if extra else self.chunk_offsets)
        lengths = self._get_array(self.extra_lengths if extra else self.chunk_lengths)
        if chunk_index >= offsets.count:
            raise PsbFormatError("PSB chunk index out of range")
        return _PsbChunk(self._get_array_elem(offsets, chunk_index), self._get_array_elem(lengths, chunk_index))

    def _get_float(self, offset: int) -> float:
        if self.data[offset] == 0x1E:
            return struct.unpack_from("<f", self.data, offset + 1)[0]
        return 0.0

    def _get_double(self, offset: int) -> float:
        if self.data[offset] == 0x1F:
            return struct.unpack_from("<d", self.data, offset + 1)[0]
        return 0.0

    def _decrypt(self, target: bytearray, offset: int, length: int) -> None:
        for idx in range(offset, offset + length):
            if self._key[4] == 0:
                v5 = self._key[3]
                v6 = self._key[0] ^ ((self._key[0] << 11) & 0xFFFFFFFF)
                self._key[0] = self._key[1]
                self._key[1] = self._key[2]
                eax = (v6 ^ v5 ^ (((v6 ^ (v5 >> 11)) >> 8) & 0xFFFFFFFF)) & 0xFFFFFFFF
                self._key[2] = v5
                self._key[3] = eax
                self._key[4] = eax
            target[idx] ^= self._key[4] & 0xFF
            self._key[4] >>= 8

    def rebuild_with_string_replacements(self, replacements: dict[int, str]) -> tuple[bytes, PsbPatchStats]:
        if self.flags & 3:
            return bytes(self.data), PsbPatchStats(unsupported=True, skipped=len(replacements))
        values = self._get_string_values()
        raw_values = [value for value, _offset in values]
        patched = 0
        skipped = 0
        for index, text in replacements.items():
            if index < 0 or index >= len(raw_values):
                skipped += 1
                continue
            new_text = _to_psb_text(text)
            if not new_text or new_text == raw_values[index]:
                skipped += 1
                continue
            raw_values[index] = new_text
            patched += 1
        if not patched:
            return bytes(self.data), PsbPatchStats(skipped=skipped)

        offsets: list[int] = []
        string_blob = bytearray()
        for value in raw_values:
            offsets.append(len(string_blob))
            string_blob.extend(value.encode("utf-8"))
            string_blob.append(0)

        strings_array = self._get_array(self.strings)
        for idx, value in enumerate(offsets):
            self._set_array_elem(strings_array, idx, value)

        old_chunk_offsets = self.chunk_offsets
        delta = len(string_blob) - (self.chunk_offsets - self.strings_data)
        rebuilt = self.data[:self.strings_data] + string_blob + self.data[old_chunk_offsets:]

        self._write_header_offset(rebuilt, 0x10, self.chunk_offsets + delta)
        self._write_header_offset(rebuilt, 0x14, self.chunk_lengths + delta)
        self._write_header_offset(rebuilt, 0x18, self.chunk_data + delta)
        if self.version > 3:
            for header_offset, value in ((0x24, self.extra_offsets), (0x28, self.extra_lengths), (0x2C, self.extra_data)):
                if value >= old_chunk_offsets:
                    self._write_header_offset(rebuilt, header_offset, value + delta)
        return bytes(rebuilt), PsbPatchStats(patched=patched, skipped=skipped)

    def _write_header_offset(self, data: bytearray, relative_offset: int, value: int) -> None:
        struct.pack_into("<I", data, self.header_start + relative_offset, value)


def extract_kirikiri_scn_texts(data: bytes) -> list[PsbTextRef]:
    return extract_kirikiri_scn_texts_and_storage_refs(data).texts


def extract_kirikiri_scn_texts_and_storage_refs(data: bytes) -> PsbScnExtractResult:
    reader = _PsbReader(data)
    reader.parse()
    scenes = reader.get_root_key("scenes")
    if not isinstance(scenes, list):
        return PsbScnExtractResult([], [])

    text_refs: list[PsbTextRef] = []
    seen: set[tuple[int, str, str]] = set()
    storage_refs: list[str] = []
    storage_seen: set[str] = set()
    for scene_index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        _collect_text_refs(scene.get("texts"), scene_index, text_refs, seen)
        _collect_select_refs(scene.get("selects"), scene_index, text_refs, seen)
        _collect_storage_refs(scene, storage_refs, storage_seen)
    return PsbScnExtractResult(text_refs, storage_refs)


def extract_kirikiri_scn_storage_refs(data: bytes) -> list[str]:
    reader = _PsbReader(data)
    reader.parse()
    scenes = reader.get_root_key("scenes")
    refs: list[str] = []
    seen: set[str] = set()
    _collect_storage_refs(scenes, refs, seen)
    return refs


def _collect_storage_refs(root: Any, refs: list[str], seen: set[str]) -> None:
    def append(value: str) -> None:
        value = value.strip()
        if not value or value in seen:
            return
        if ".ks" not in value.lower() and ".scn" not in value.lower():
            return
        seen.add(value)
        refs.append(value)

    def walk(value: Any) -> None:
        if isinstance(value, _PsbString):
            append(value.value)
        elif isinstance(value, str):
            append(value)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in {"storage", "target", "file", "scenario"}:
                    walk(item)
                elif key in {"nexts", "links", "selects"}:
                    walk(item)

    walk(root)


def patch_kirikiri_scn_texts(data: bytes, replacements: dict[int, str]) -> tuple[bytes, PsbPatchStats]:
    reader = _PsbReader(data)
    reader.parse()
    return reader.rebuild_with_string_replacements(replacements)


def is_kirikiri_scn(data: bytes) -> bool:
    return data.startswith(PSB_SIGNATURE)


def _collect_text_refs(raw_texts: Any, scene_index: int, refs: list[PsbTextRef], seen: set[tuple[int, str, str]]) -> None:
    if not isinstance(raw_texts, list):
        return
    for text_index, entry in enumerate(raw_texts):
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        real_name = _string_at(entry, 0)
        display_name: _PsbString | None = None
        message: Any
        if isinstance(entry[1], list):
            message = entry[1]
        else:
            display_name = _string_at(entry, 1)
            message = _string_at(entry, 2) if len(entry) > 2 else None
            if message is None and len(entry) > 2:
                message = entry[2]

        if isinstance(message, list) and message:
            language_text = message[0]
            if isinstance(language_text, list) and len(language_text) >= 2:
                display_name = _string_at(language_text, 0)
                message = _string_at(language_text, 1)

        name_ref = display_name or real_name
        if isinstance(name_ref, _PsbString) and name_ref.value and name_ref.value != "＠":
            _append_ref(refs, seen, name_ref, "name", f"scene[{scene_index}].texts[{text_index}].name")
        if isinstance(message, _PsbString):
            _append_ref(refs, seen, message, "message", f"scene[{scene_index}].texts[{text_index}].message")


def _collect_select_refs(raw_selects: Any, scene_index: int, refs: list[PsbTextRef], seen: set[tuple[int, str, str]]) -> None:
    if not isinstance(raw_selects, list):
        return
    for select_index, select in enumerate(raw_selects):
        if not isinstance(select, dict):
            continue
        text = None
        language = select.get("language")
        if isinstance(language, list) and language and isinstance(language[0], dict):
            text = language[0].get("text")
        if text is None:
            text = select.get("text")
        if isinstance(text, _PsbString):
            _append_ref(refs, seen, text, "choice", f"scene[{scene_index}].selects[{select_index}].text")


def _string_at(values: list[Any], index: int) -> _PsbString | None:
    if index < len(values) and isinstance(values[index], _PsbString):
        return values[index]
    return None


def _append_ref(refs: list[PsbTextRef], seen: set[tuple[int, str, str]], value: _PsbString, kind: str, path: str) -> None:
    text = value.value.replace("\\n", "\r\n")
    key = (value.index, kind, text)
    if key in seen:
        return
    seen.add(key)
    refs.append(PsbTextRef(index=value.index, text=text, kind=kind, path=path))


def _read_cstring(data: bytearray, offset: int) -> str:
    end = offset
    while end < len(data) and data[end] != 0:
        end += 1
    return bytes(data[offset:end]).decode("utf-8", errors="replace")


def _to_psb_text(text: str) -> str:
    return text.replace("\r\n", "\\n").replace("\n", "\\n")


def _u32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _read_int(data: bytes | bytearray, offset: int, size: int, *, signed: bool) -> int:
    chunk = bytes(data[offset:offset + size])
    if len(chunk) != size:
        raise PsbFormatError("unexpected PSB EOF")
    return int.from_bytes(chunk, "little", signed=signed)


def _write_int(data: bytearray, offset: int, size: int, value: int) -> None:
    data[offset:offset + size] = int(value).to_bytes(size, "little", signed=False)
