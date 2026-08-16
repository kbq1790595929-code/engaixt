"""Godot PCK 容器：载荷读取、重建（标准/Steam 变体）与内嵌 PCK 定位。"""

from __future__ import annotations

import hashlib
import re
import struct
from collections.abc import Iterable
from pathlib import Path

from utils.logger import info


# ---------------------------------------------------------------------------
# PCK rebuilding
# ---------------------------------------------------------------------------

def _pck_path_variants(path: str) -> list[str]:
    norm = path.replace("\\", "/")
    stripped = norm[6:] if norm.startswith("res://") else norm.lstrip("/")
    variants = [
        path,
        norm,
        stripped,
        "res://" + stripped,
        stripped.replace("/", "\\"),
    ]
    seen: set[str] = set()
    out: list[str] = []
    for candidate in variants:
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _lookup_patched_payload(patched_map: dict[str, bytes], pck_path: str) -> bytes | None:
    for candidate in _pck_path_variants(pck_path):
        if candidate in patched_map:
            return patched_map[candidate]
    return None


def read_pck_payloads(pck_path: Path, wanted_paths: Iterable[str]) -> dict[str, bytes]:
    wanted = list(wanted_paths)
    if not wanted:
        return {}

    wanted_by_variant: dict[str, str] = {}
    for wanted_path in wanted:
        for variant in _pck_path_variants(wanted_path):
            wanted_by_variant.setdefault(variant, wanted_path)

    data = pck_path.read_bytes()
    if data[:4] != b"GDPC":
        raise ValueError(f"Not a valid Godot PCK: {pck_path}")

    found: dict[str, bytes] = {}

    def maybe_add(entry_path: str, payload: bytes) -> None:
        for variant in _pck_path_variants(entry_path):
            wanted_key = wanted_by_variant.get(variant)
            if wanted_key is not None:
                found[wanted_key] = payload
                return

    if _looks_like_godot_steam_pck(data):
        for name, offset, size, _flags, _md5 in _parse_steam_entries(data):
            if offset + size <= len(data):
                maybe_add(name, data[offset:offset + size])
        return found

    file_base = struct.unpack_from("<Q", data, 24)[0]
    pos = 48
    while pos < file_base and data[pos:pos + 8] == b"\x00" * 8:
        pos += 8
    while pos + 8 < file_base:
        path_len = struct.unpack_from("<I", data, pos + 4)[0]
        if path_len == 0 or path_len > 2000:
            break
        padded = path_len + (4 - path_len % 4) % 4
        ep = pos + 8 + padded
        if ep + 32 > file_base:
            break
        path = data[pos + 8:pos + 8 + path_len].decode("utf-8", errors="replace").rstrip("\x00")
        entry_offset = struct.unpack_from("<Q", data, ep)[0]
        entry_size = struct.unpack_from("<Q", data, ep + 8)[0]
        abs_pos = file_base + entry_offset
        if abs_pos + entry_size <= len(data):
            maybe_add(path, data[abs_pos:abs_pos + entry_size])
        pos = ep + 32

    return found


def rebuild_pck(original_pck_path: Path, patched_map: dict[str, bytes],
                output_path: Path, decrypted_dir: Path = None):
    """Rebuild a Godot 4 PCK with patched file contents.

    Args:
        original_pck_path: Path to original PCK file (for header + entry index)
        patched_map: {file_path_in_pck: new_file_data} — only changed files
        output_path: Where to write the rebuilt PCK
        decrypted_dir: If set, read unpatched file data from this dir
                       (used when original PCK is encrypted)
    """
    orig = bytearray(original_pck_path.read_bytes())

    # Read PCK header
    if orig[:4] != b"GDPC":
        raise ValueError(f"Not a valid Godot PCK: {original_pck_path}")

    orig_fb = struct.unpack_from("<Q", orig, 24)[0]  # Original file_base

    # Find entry start (skip padding)
    entry_start = 48
    while entry_start < orig_fb and orig[entry_start:entry_start + 8] == b"\x00" * 8:
        entry_start += 8
    pad_bytes = entry_start - 48

    # Parse all entries
    p_entries = []
    pos = entry_start
    while pos + 8 < orig_fb:
        path_len = struct.unpack_from("<I", orig, pos + 4)[0]
        if path_len == 0 or path_len > 2000:
            break
        padded = path_len + (4 - path_len % 4) % 4
        ep = pos + 4 + 4 + padded
        if ep + 32 > orig_fb:
            break
        path_bytes = bytes(orig[pos + 8:pos + 8 + path_len])
        path = path_bytes.decode("utf-8", errors="replace").rstrip("\x00")
        entry_offset = struct.unpack_from("<Q", orig, ep)[0]
        entry_size = struct.unpack_from("<Q", orig, ep + 8)[0]
        abs_pos = orig_fb + entry_offset
        file_data = bytes(orig[abs_pos:abs_pos + entry_size])
        p_entries.append((bytes(orig[pos:pos + 4 + 4 + padded]), path, file_data))
        pos = ep + 32

    # Calculate new file_base
    new_fb_raw = 48 + pad_bytes
    for ep, _, _ in p_entries:
        new_fb_raw += len(ep) + 32
    new_fb = (new_fb_raw + 15) & ~15  # 16-byte align
    entry_pad = new_fb - new_fb_raw

    # Update header
    # Update header: clear encryption flag (bit 0 at offset 20)
    flags = struct.unpack_from("<I", orig, 20)[0]
    if flags & 1:
        struct.pack_into("<I", orig, 20, flags & ~1)
        info("  已清除 PCK 加密标志")

    struct.pack_into("<Q", orig, 24, new_fb)

    # Write output
    with open(output_path, "wb") as out:
        out.write(bytes(orig[:48]))
        out.write(b"\x00" * pad_bytes)

        cur_off = new_fb
        chunks = []
        for ep, p, fd in p_entries:
            out.write(ep)
            d = _lookup_patched_payload(patched_map, p)
            if d is None:
                d = fd
            # If decrypted_dir is set and this file is NOT patched,
            # read decrypted version instead of original encrypted data
            if decrypted_dir and _lookup_patched_payload(patched_map, p) is None:
                dec_file = decrypted_dir / p
                if dec_file.exists():
                    d = dec_file.read_bytes()
            out.write(struct.pack("<Q", cur_off - new_fb))
            out.write(struct.pack("<Q", len(d)))
            out.write(hashlib.md5(d).digest())
            cur_off += len(d)
            chunks.append(d)

        out.write(b"\x00" * entry_pad)
        for c in chunks:
            out.write(c)


def rebuild_pck_steam(original_pck_path: Path, patched_map: dict[str, bytes],
                      output_path: Path, extract_dir: Path = None):
    """Rebuild a GodotSteam-format PCK (entries at end, GDPC footer).

    GodotSteam layout:
      [Header: 48 bytes]
      [Data section: file contents concatenated]
      [Entry list: entries in forward order, name_len(4)+name(N)+pad4+offset(8)+size(8)+md5(16)+flags(4)]
      [Footer: data_size(8) + GDPC(4)]

    Entry offsets are relative to PCK start (offset 0).
    """
    orig = original_pck_path.read_bytes()
    if orig[:4] != b"GDPC":
        raise ValueError(f"Not a valid Godot PCK: {original_pck_path}")

    header = bytearray(orig[:48])
    orig_fb = struct.unpack_from("<Q", header, 24)[0]

    # Clear encryption flag
    flags = struct.unpack_from("<I", header, 20)[0]
    if flags & 1:
        struct.pack_into("<I", header, 20, flags & ~1)

    # Parse ALL entries from the end (not just the ones extracted earlier)
    steam_entries = _parse_steam_entries(orig)
    if not steam_entries:
        raise ValueError("No GodotSteam entries found in PCK")

    info(f"  GodotSteam 重建: {len(steam_entries)} 个文件")

    # Build data section and new entries
    new_entries = []  # [(encoded_entry_bytes, file_data)]
    data_offset = orig_fb  # data starts at file_base

    for name, orig_offset, orig_size, orig_flags, orig_md5 in steam_entries:
        patched_data = _lookup_patched_payload(patched_map, name)
        if patched_data is not None:
            file_data = patched_data
        elif extract_dir is not None:
            f = extract_dir / name
            if f.exists():
                file_data = f.read_bytes()
            else:
                file_data = orig[orig_offset:orig_offset + orig_size]
        else:
            file_data = orig[orig_offset:orig_offset + orig_size]

        # Build entry: name_len(4) + name(N) + pad_to_4 + offset(8) + size(8) + md5(16) + flags(4)
        name_bytes = name.encode("utf-8")
        name_len = len(name_bytes)
        padded = (4 + name_len + 3) // 4 * 4
        entry_bin = bytearray(padded + 36)
        struct.pack_into("<I", entry_bin, 0, name_len)
        entry_bin[4:4 + name_len] = name_bytes
        # padding already zero
        struct.pack_into("<Q", entry_bin, padded, data_offset)
        struct.pack_into("<Q", entry_bin, padded + 8, len(file_data))
        entry_bin[padded + 16:padded + 32] = hashlib.md5(file_data).digest()
        struct.pack_into("<I", entry_bin, padded + 32, orig_flags)

        new_entries.append((bytes(entry_bin), file_data))
        data_offset += len(file_data)

    # Calculate footer values
    entry_list_size = sum(len(e) for e, _ in new_entries)
    total_data_size = orig_fb + sum(len(d) for _, d in new_entries) + entry_list_size

    # Write output
    with open(output_path, "wb") as out:
        out.write(bytes(header))
        if orig_fb > 48:
            out.write(b"\x00" * (orig_fb - 48))
        # Data section: all file contents
        for _, file_data in new_entries:
            out.write(file_data)
        # Entry list
        for entry_bin, _ in new_entries:
            out.write(entry_bin)
        # Footer
        out.write(struct.pack("<Q", total_data_size))
        out.write(b"GDPC")


def _parse_steam_entries(data: bytes) -> list[tuple[str, int, int, int, bytes]]:
    """Parse all entries from GodotSteam PCK format.

    Returns list of (name, offset, size, flags, md5_bytes) in forward order.
    """
    footer_start = len(data) - 12
    data_size_val = struct.unpack_from("<Q", data, footer_start)[0]
    file_base = struct.unpack_from("<Q", data, 24)[0]

    pos = footer_start
    entries_rev = []
    failed = 0

    while pos > file_base and failed < 5:
        meta_start = pos - 36
        if meta_start < 0:
            break

        foffset = struct.unpack_from("<Q", data, meta_start)[0]
        fsize = struct.unpack_from("<Q", data, meta_start + 8)[0]
        md5_bytes = bytes(data[meta_start + 16:meta_start + 32])
        eflags = struct.unpack_from("<I", data, pos - 4)[0]

        if foffset >= data_size_val or fsize > 500_000_000:
            failed += 1
            break

        found = False
        for padded_block in range(4, 2048, 4):
            name_len_pos = meta_start - padded_block
            if name_len_pos < 0:
                break
            potential_len = struct.unpack_from("<I", data, name_len_pos)[0]
            if potential_len < 1 or potential_len > 1000:
                continue
            if (4 + potential_len + 3) // 4 * 4 != padded_block:
                continue
            name_start = name_len_pos + 4
            name_end = name_start + potential_len
            if name_end > meta_start:
                continue
            padding = data[name_end:meta_start]
            if not all(b == 0 for b in padding):
                continue
            try:
                name = data[name_start:name_end].rstrip(b'\x00').decode("utf-8")
            except UnicodeDecodeError:
                continue
            if not name:
                continue
            entries_rev.append((name, foffset, fsize, eflags, md5_bytes))
            pos = name_len_pos
            found = True
            break

        if not found:
            failed += 1
            break

    entries_rev.reverse()
    return entries_rev


def _looks_like_godot_steam_pck(data: bytes) -> bool:
    """Return true for PCKs with GodotSteam-style trailing entry tables."""
    if len(data) < 60 or data[:4] != b"GDPC" or data[-4:] != b"GDPC":
        return False
    try:
        footer_start = len(data) - 12
        data_size_val = struct.unpack_from("<Q", data, footer_start)[0]
        file_base = struct.unpack_from("<Q", data, 24)[0]
    except struct.error:
        return False
    if not (48 <= file_base < data_size_val <= len(data)):
        return False
    try:
        return bool(_parse_steam_entries(data))
    except Exception:
        return False


def _standard_pck_end(data: bytes, start: int = 0) -> int | None:
    """Return the byte end of a standard front-index Godot PCK inside data."""
    view = data[start:]
    if len(view) < 48 or view[:4] != b"GDPC":
        return None
    try:
        file_base = struct.unpack_from("<Q", view, 24)[0]
    except struct.error:
        return None
    if file_base < 48 or file_base > len(view):
        return None

    entry_start = 48
    while entry_start < file_base and view[entry_start:entry_start + 8] == b"\x00" * 8:
        entry_start += 8

    max_end = file_base
    pos = entry_start
    parsed = 0
    while pos + 8 < file_base:
        try:
            path_len = struct.unpack_from("<I", view, pos + 4)[0]
        except struct.error:
            return None
        if path_len == 0 or path_len > 2000:
            break
        padded = path_len + (4 - path_len % 4) % 4
        ep = pos + 4 + 4 + padded
        if ep + 32 > file_base:
            break
        try:
            entry_offset = struct.unpack_from("<Q", view, ep)[0]
            entry_size = struct.unpack_from("<Q", view, ep + 8)[0]
        except struct.error:
            return None
        abs_end = file_base + entry_offset + entry_size
        if abs_end > len(view):
            return None
        max_end = max(max_end, abs_end)
        parsed += 1
        pos = ep + 32

    return start + max_end if parsed else None


def _steam_pck_end(data: bytes, start: int = 0) -> int | None:
    """Return the byte end of a GodotSteam trailing-index PCK inside data."""
    view = data[start:]
    if len(view) < 60 or view[:4] != b"GDPC":
        return None
    try:
        file_base = struct.unpack_from("<Q", view, 24)[0]
    except struct.error:
        return None
    search_pos = 48
    while True:
        footer_idx = view.find(b"GDPC", search_pos)
        if footer_idx < 0:
            return None
        if footer_idx >= 8:
            candidate = view[:footer_idx + 4]
            try:
                data_size_val = struct.unpack_from("<Q", view, footer_idx - 8)[0]
            except struct.error:
                data_size_val = 0
            if data_size_val == footer_idx - 8 and _looks_like_godot_steam_pck(candidate):
                return start + footer_idx + 4
        search_pos = footer_idx + 1


def _embedded_pck_end(data: bytes, start: int) -> int | None:
    """Return the exclusive end offset for an embedded PCK."""
    if start < 0 or start >= len(data):
        return None
    steam_end = _steam_pck_end(data, start)
    standard_end = _standard_pck_end(data, start)
    candidates = [end for end in (steam_end, standard_end) if end and end > start]
    return min(candidates) if candidates else None


# ---------------------------------------------------------------------------
# Engine class
# ---------------------------------------------------------------------------

def _is_valid_pck_header(data: bytes, idx: int) -> bool:
    """Check if GDPC at idx is a real PCK header (not machine code containing 'GDPC')."""
    if idx + 32 > len(data):
        return False
    version = struct.unpack_from("<I", data, idx + 4)[0]
    if version < 1 or version > 10:
        return False
    file_base = struct.unpack_from("<Q", data, idx + 24)[0]
    if file_base < 48 or file_base > len(data) - idx:
        return False
    return True


def _find_embedded_pck(data: bytes) -> int:
    """Find embedded PCK in EXE data. Returns offset to valid GDPC header, or -1.

    Returns the FIRST valid GDPC header — the real PCK header is at the start
    of the embedded data. Later GDPC occurrences in file data are false positives.
    """
    pos = 0
    while True:
        idx = data.find(b"GDPC", pos)
        if idx < 0:
            break
        if _is_valid_pck_header(data, idx):
            # Check that this isn't the footer GDPC at the very end
            # Footer GDPC has data_size(8) before it, not valid PCK entry data
            if idx + 48 <= len(data):
                return idx
        pos = idx + 1
    return -1


def _find_embedded_pck_range(data: bytes) -> tuple[int, int] | None:
    """Find an embedded PCK range inside an executable.

    Returns (start, end) so callers can preserve any executable overlay bytes
    that follow the embedded pack.
    """
    pos = 0
    while True:
        start = data.find(b"GDPC", pos)
        if start < 0:
            return None
        if _is_valid_pck_header(data, start):
            end = _embedded_pck_end(data, start)
            if end is not None:
                return start, end
        pos = start + 1


def _file_starts_with(path: Path, magic: bytes) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(magic)) == magic
    except OSError:
        return False


def _exe_has_embedded_pck_marker_quick(path: Path) -> bool:
    """Cheap detection-only probe for Godot executables.

    Full embedded PCK parsing reads the whole executable and is intentionally
    kept for unpack(); GUI engine detection should stay responsive.
    """
    head_size = 2 * 1024 * 1024
    tail_size = 16 * 1024 * 1024
    markers = (b"GDPC", b"Godot Engine", b"godot.windows", b"godot_")
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(min(head_size, size))
            if any(marker in head for marker in markers):
                return True
            if size <= len(head):
                return False
            handle.seek(max(0, size - tail_size))
            tail = handle.read(tail_size)
            return b"GDPC" in tail or b"Godot Engine" in tail
    except OSError:
        return False


def _count_files(path: Path) -> int:
    try:
        return sum(1 for p in path.rglob("*") if p.is_file())
    except Exception:
        return 0


def _scn_bytes_contain_japanese(data: bytes) -> bool:
    """Fast guard for exported editor resources that may still contain game text."""
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        return False
    return bool(re.search(r"[\u3040-\u30ff\uff66-\uff9f]", text))
