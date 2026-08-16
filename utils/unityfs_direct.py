"""Direct byte-level manipulation of UnityFS sub-bundles.

Bypasses UnityPy's bf.save() which produces incompatible output for some Unity versions.
Works by modifying the Lua string in the decompressed serialized data, then rebuilding
the UnityFS wrapper with the same compression algorithm.

Key strategy: preserves original compressed data for blocks whose decompressed content
hasn't changed. This avoids LZ4 library version differences producing different compressed
output for identical input, which the game engine rejects.
"""

import io
import struct
import re
import hashlib
from pathlib import Path

import lz4.block
from UnityPy.helpers import CompressionHelper
from UnityPy.enums import ArchiveFlags, CompressionFlags
from UnityPy.streams import EndianBinaryReader, EndianBinaryWriter

from utils.lz4_patch import patch_translations_in_blocks


def _parse_version(ver_engine: str) -> tuple:
    """Parse Unity version string to tuple."""
    m = re.match(r"(\d+)\.(\d+)\.(\d+)\w.+", ver_engine or "0.0.0")
    if m:
        return tuple(int(x) for x in m.groups())
    return (0, 0, 0)


def parse_unityfs(data: bytes) -> dict:
    """Parse UnityFS header and decompress blocks.

    Returns dict with all structural info plus 'decompressed' (raw serialized data).
    Also stores original compressed data to enable zero-diff rebuilds.
    """
    reader = EndianBinaryReader(data)
    sig = reader.read_string_to_null()
    version = reader.read_u_int()
    ver_player = reader.read_string_to_null()
    ver_engine = reader.read_string_to_null()
    total_size = reader.read_long()
    compressed_bi_size = reader.read_u_int()
    uncompressed_bi_size = reader.read_u_int()
    data_flags = reader.read_u_int()

    if sig != "UnityFS" and version >= 6:
        reader.read_byte()

    v_tuple = _parse_version(ver_engine)
    uses_alignment = version >= 7 or (v_tuple[0] == 2019 and v_tuple >= (2019, 4, 15))
    if uses_alignment:
        reader.align_stream(16)

    blocks_at_end = bool(data_flags & 0x80)
    header_end = reader.Position

    if blocks_at_end:
        bi_start = len(data) - compressed_bi_size
        bi_bytes = data[bi_start:bi_start + compressed_bi_size]
        data_start = header_end
    else:
        bi_start = reader.Position
        bi_bytes = data[bi_start:bi_start + compressed_bi_size]
        data_start = bi_start + compressed_bi_size
        if data_flags & 0x200:
            while data_start % 16 != 0:
                data_start += 1

    # Decompress block info
    comp_type = data_flags & 0x3F
    bi_raw = decompress_bytes(bi_bytes, uncompressed_bi_size, comp_type)

    # Parse block info
    bi_reader = EndianBinaryReader(bi_raw)
    _hash = bi_reader.read_bytes(16)
    block_count = bi_reader.read_int()
    blocks = []
    for _ in range(block_count):
        u_size = bi_reader.read_u_int()
        c_size = bi_reader.read_u_int()
        flags = bi_reader.read_u_short()
        blocks.append((u_size, c_size, flags))

    # Parse directory info
    node_count = bi_reader.read_int()
    dir_entries = []
    for _ in range(node_count):
        offset = bi_reader.read_long()
        size = bi_reader.read_long()
        flags = bi_reader.read_u_int()
        path = bi_reader.read_string_to_null()
        dir_entries.append((offset, size, flags, path))

    # Decompress data blocks, preserving raw compressed bytes
    decompressed_chunks = []  # list of bytearrays, one per block
    raw_compressed_blocks = []  # list of bytes, original compressed data per block
    pos = data_start
    for u_size, c_size, flags in blocks:
        block_comp_type = flags & 0x3F
        block_data = data[pos:pos + c_size]
        raw_compressed_blocks.append(bytes(block_data))
        pos += c_size
        decompressed_chunks.append(bytearray(decompress_bytes(block_data, u_size, block_comp_type)))

    # Combine all chunks into one decompressed blob
    decompressed = bytearray()
    for chunk in decompressed_chunks:
        decompressed.extend(chunk)

    # Store chunk boundaries for tracking which blocks need recompression
    chunk_offsets = []
    off = 0
    for chunk in decompressed_chunks:
        chunk_offsets.append(off)
        off += len(chunk)

    # Capture trailing data after the UnityFS block
    trailing = data[total_size:] if total_size < len(data) else b""

    # Store original data and content hash for zero-diff detection
    original_data = bytes(data[:total_size] if total_size <= len(data) else data)

    return {
        'signature': sig,
        'version': version,
        'player': ver_player,
        'engine': ver_engine,
        'total_size': total_size,
        'data_flags': data_flags,
        'header_end': header_end,
        'blocks_at_end': blocks_at_end,
        'compression_type': comp_type,
        'blocks': blocks,
        'dir_entries': dir_entries,
        'uses_alignment': uses_alignment,
        'decompressed': decompressed,
        'decompressed_chunks': decompressed_chunks,  # per-block decompressed data (bytearray, modifiable)
        '_orig_chunk_data': [bytes(c) for c in decompressed_chunks],  # immutable snapshot for diff detection
        'chunk_offsets': chunk_offsets,  # byte offset of each chunk in decompressed
        'raw_compressed_blocks': raw_compressed_blocks,  # original compressed bytes
        'compressed_bi_bytes': bi_bytes,  # original compressed block info bytes
        'uncompressed_bi_raw': bi_raw,  # decompressed block info bytes
        'data_start': data_start,  # byte offset where data blocks start in original
        'trailing': trailing,
        '_original_data': original_data,
        '_original_hash': hashlib.md5(bytes(decompressed)).hexdigest(),
    }


def decompress_bytes(data: bytes, uncomp_size: int, comp_type: int) -> bytes:
    """Decompress data using specified compression type."""
    if comp_type == CompressionFlags.NONE:
        return data
    elif comp_type == CompressionFlags.LZMA:
        return CompressionHelper.decompress_lzma(data, uncomp_size)
    elif comp_type in (CompressionFlags.LZ4, CompressionFlags.LZ4HC):
        return CompressionHelper.decompress_lz4(data, uncomp_size)
    else:
        raise NotImplementedError(f"Decompression type: {comp_type}")


def compress_bytes(data: bytes, comp_type: int) -> bytes:
    """Compress data using specified compression type."""
    if comp_type == CompressionFlags.NONE:
        return data
    elif comp_type == CompressionFlags.LZMA:
        return CompressionHelper.compress_lzma(data)
    elif comp_type in (CompressionFlags.LZ4, CompressionFlags.LZ4HC):
        return lz4.block.compress(data, mode="high_compression", compression=9, store_size=False)
    else:
        raise NotImplementedError(f"Compression type: {comp_type}")


def _patch_individual_strings(info, raw_blocks, orig_blocks,
                              orig_code, new_code):
    """Try to patch individual translated strings within LZ4 blocks.

    Instead of patching the entire Lua code (which may span blocks),
    finds each translated text string and patches it in-place.
    """
    import difflib
    orig_bytes = orig_code.encode('utf-8')
    new_bytes = new_code.encode('utf-8')

    # Find differing segments using difflib
    sm = difflib.SequenceMatcher(None, orig_bytes, new_bytes)
    patches = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'replace' or tag == 'insert':
            old_seg = orig_bytes[i1:i2]
            new_seg = new_bytes[j1:j2]
            if len(new_seg) > len(old_seg):
                return None  # Can't handle longer translations with patching
            patches.append((old_seg, new_seg, i1))
        elif tag == 'delete':
            old_seg = orig_bytes[i1:i2]
            new_seg = b''
            patches.append((old_seg, new_seg, i1))

    if not patches:
        return None

    # Try to patch each segment in the LZ4 blocks
    patched_blocks = [bytes(b) for b in raw_blocks]  # copy
    for old_seg, new_seg, orig_pos_in_code in patches:
        if len(old_seg) == 0:
            continue
        # Pad new_seg to match old_seg length
        if len(new_seg) < len(old_seg):
            new_seg = new_seg + b'\x00' * (len(old_seg) - len(new_seg))

        # Try patching in each block
        patched = False
        for i, (comp_data, (u_size, c_size, flags)) in enumerate(zip(patched_blocks, orig_blocks)):
            comp_type = flags & 0x3F
            if comp_type not in (2, 3):  # not LZ4
                continue
            from utils.lz4_patch import patch_lz4_stream
            result = patch_lz4_stream(comp_data, u_size, old_seg, new_seg)
            if result is not None:
                patched_blocks[i] = result
                patched = True
                break

        if not patched:
            return None  # This segment couldn't be patched

    return patched_blocks


def _rebuild_with_patched_blocks(info: dict, patched_blocks: list[bytes], trailing: bytes = b"") -> bytes:
    """Rebuild UnityFS with patched LZ4 blocks (all sizes unchanged, delta=0).

    Uses the original UnityFS structure but with modified compressed data blocks.
    Since all block sizes are identical to originals, the structure is unchanged.
    """
    # Block info is identical (same block sizes), reuse original compressed BI
    compressed_bi = info.get('compressed_bi_bytes', b'')

    # Rebuild header - identical to original
    from UnityPy.streams import EndianBinaryWriter
    writer = EndianBinaryWriter()
    writer.write_string_to_null(info['signature'])
    writer.write_u_int(info['version'])
    writer.write_string_to_null(info['player'])
    writer.write_string_to_null(info['engine'])

    size_pos = writer.Position
    writer.write_long(0)
    writer.write_u_int(len(compressed_bi))
    writer.write_u_int(len(info.get('uncompressed_bi_raw', b'')))
    writer.write_u_int(info['data_flags'])

    if info['signature'] != "UnityFS" and info['version'] >= 6:
        writer.write_byte(0)

    if info.get('uses_alignment', False):
        writer.align_stream(16)

    original_data_flags = info['data_flags']

    if info.get('blocks_at_end', False):
        if original_data_flags & 0x200:
            writer.align_stream(16)
        for block_data in patched_blocks:
            writer.write(block_data)
        writer.write(compressed_bi)
    else:
        writer.write(compressed_bi)
        if original_data_flags & 0x200:
            writer.align_stream(16)
        for block_data in patched_blocks:
            writer.write(block_data)

    end_pos = writer.Position
    writer.Position = size_pos
    writer.write_long(end_pos)

    result = writer.bytes
    if trailing:
        result += trailing
    return result


def rebuild_unityfs(info: dict, use_compression: bool = True,
                   arc_offset: int = None,
                   patch_original: str = None, patch_translated: str = None) -> bytes:
    """Rebuild UnityFS bytes from modified decompressed data.

    If data unchanged, returns original bytes. If patch_original/patch_translated
    provided, tries LZ4-in-place patching first (delta=0). Falls back to
    recompressing only changed blocks.
    """
    decompressed = bytes(info['decompressed'])
    original_data_flags = info['data_flags']
    original_blocks = info['blocks']
    dir_entries = info['dir_entries']
    trailing = info.get('trailing', b'')
    old_total_size = info.get('total_size', 0)

    # Check if decompressed data actually changed
    new_hash = hashlib.md5(decompressed).hexdigest()
    if new_hash == info.get('_original_hash', ''):
        result = info.get('_original_data')
        if result is not None:
            result = bytes(result)
            if trailing:
                result += trailing
            return result

    # Data changed — try LZ4 in-place patching first (delta=0)
    raw_compressed = info.get('raw_compressed_blocks', [])
    if patch_original and patch_translated and raw_compressed:
        # Try patching the ENTIRE code first (works for small scripts in one block)
        patched_blocks = patch_translations_in_blocks(
            raw_compressed, original_blocks, patch_original, patch_translated)
        if patched_blocks is not None:
            return _rebuild_with_patched_blocks(info, patched_blocks, trailing)

        # Full code didn't fit in one block — try individual string patches
        # The decompressed data was modified by apply_translations_direct
        # We need to find each changed substring and patch it individually
        patched_blocks = _patch_individual_strings(
            info, raw_compressed, original_blocks,
            patch_original, patch_translated)
        if patched_blocks is not None:
            return _rebuild_with_patched_blocks(info, patched_blocks, trailing)

    # Fallback: identify changed blocks for recompression
    orig_chunks_snapshot = info.get('_orig_chunk_data', [])
    chunk_offsets = info.get('chunk_offsets', [])

    changed_blocks = set()
    if orig_chunks_snapshot and chunk_offsets and len(orig_chunks_snapshot) == len(original_blocks):
        for i, (off, orig_snapshot) in enumerate(zip(chunk_offsets, orig_chunks_snapshot)):
            new_chunk = decompressed[off:off + len(orig_snapshot)]
            if bytes(new_chunk) != orig_snapshot:
                changed_blocks.add(i)

    comp_type = info['compression_type']
    base_flags = original_blocks[0][2] if original_blocks else 0
    base_flags_no_comp = (base_flags & ~0x3F) | CompressionFlags.NONE

    # Build new blocks, reusing original compressed data where possible
    new_blocks = []
    compressed_data = bytearray()

    if orig_chunks_snapshot and chunk_offsets and len(orig_chunks_snapshot) == len(original_blocks):
        # Per-block: reuse original compressed if unchanged, recompress if changed
        for i in range(len(original_blocks)):
            off = chunk_offsets[i]
            orig_snapshot = orig_chunks_snapshot[i]
            new_chunk = decompressed[off:off + len(orig_snapshot)]

            if i in changed_blocks:
                # Recompress this block
                compressed = compress_bytes(bytes(new_chunk), comp_type)
                if len(compressed) > len(new_chunk):
                    compressed_data.extend(new_chunk)
                    new_blocks.append((len(new_chunk), len(new_chunk), base_flags_no_comp))
                else:
                    compressed_data.extend(compressed)
                    new_blocks.append((len(new_chunk), len(compressed), base_flags))
            else:
                # Reuse original compressed data
                compressed_data.extend(raw_compressed[i])
                new_blocks.append(original_blocks[i])
    else:
        # Fallback: recompress everything
        if comp_type in (CompressionFlags.LZ4, CompressionFlags.LZ4HC):
            chunk_size = 0x00020000
            offset = 0
            while offset < len(decompressed):
                chunk = decompressed[offset:offset + chunk_size]
                compressed = compress_bytes(chunk, comp_type)
                if len(compressed) > len(chunk):
                    compressed_data.extend(chunk)
                    new_blocks.append((len(chunk), len(chunk), base_flags_no_comp))
                else:
                    compressed_data.extend(compressed)
                    new_blocks.append((len(chunk), len(compressed), base_flags))
                offset += chunk_size
        else:
            compressed_data = compress_bytes(decompressed, comp_type)
            new_flags = base_flags_no_comp if comp_type == CompressionFlags.NONE else base_flags
            new_blocks = [(len(decompressed), len(compressed_data), new_flags)]

    compressed_data = bytes(compressed_data)

    # Update directory entries
    total_file_size = len(decompressed)
    if len(dir_entries) == 1:
        _, _, flags, path = dir_entries[0]
        new_dir_entries = [(0, total_file_size, flags, path)]
    else:
        new_dir_entries = dir_entries

    # Build block info
    bi_writer = EndianBinaryWriter(b"\x00" * 16)
    bi_writer.write_int(len(new_blocks))
    for u_size, c_size, flags in new_blocks:
        bi_writer.write_u_int(u_size)
        bi_writer.write_u_int(c_size)
        bi_writer.write_u_short(flags)

    bi_writer.write_int(len(new_dir_entries))
    for offset, size, flags, path in new_dir_entries:
        bi_writer.write_long(offset)
        bi_writer.write_long(size)
        bi_writer.write_u_int(flags)
        bi_writer.write_string_to_null(path)

    block_info_raw = bi_writer.bytes
    bi_writer.dispose()

    # Compress block info
    compressed_bi = compress_bytes(block_info_raw, comp_type)

    # Write header
    writer = EndianBinaryWriter()
    writer.write_string_to_null(info['signature'])
    writer.write_u_int(info['version'])
    writer.write_string_to_null(info['player'])
    writer.write_string_to_null(info['engine'])

    size_pos = writer.Position
    writer.write_long(0)
    writer.write_u_int(len(compressed_bi))
    writer.write_u_int(len(block_info_raw))
    writer.write_u_int(original_data_flags)

    if info['signature'] != "UnityFS" and info['version'] >= 6:
        writer.write_byte(0)

    if info.get('uses_alignment', False):
        writer.align_stream(16)

    if info.get('blocks_at_end', False):
        if original_data_flags & 0x200:
            writer.align_stream(16)
        writer.write(compressed_data)
        writer.write(compressed_bi)
    else:
        writer.write(compressed_bi)
        if original_data_flags & 0x200:
            writer.align_stream(16)
        writer.write(compressed_data)

    end_pos = writer.Position
    writer.Position = size_pos
    writer.write_long(end_pos)

    result = writer.bytes
    new_total_size = len(result)

    if trailing and arc_offset is not None and new_total_size != old_total_size:
        trailing = update_trailing_index(trailing, arc_offset, old_total_size, new_total_size)

    if trailing:
        result += trailing

    return result


def update_trailing_index(trailing: bytes, old_offset: int, old_size: int, new_size: int) -> bytes:
    """Update the size for one sub-bundle entry in the trailing index."""
    result = bytearray(trailing)
    offset_bytes = struct.pack('<q', old_offset)
    pos = result.find(offset_bytes)
    if pos < 0:
        return trailing
    if pos + 16 > len(result):
        return trailing
    stored_size = struct.unpack_from('<q', result, pos + 8)[0]
    if stored_size != old_size:
        next_pos = result.find(offset_bytes, pos + 1)
        if next_pos >= 0 and next_pos + 16 <= len(result):
            stored_size2 = struct.unpack_from('<q', result, next_pos + 8)[0]
            if stored_size2 == old_size:
                pos = next_pos
    if pos + 16 > len(result):
        return trailing
    struct.pack_into('<q', result, pos + 8, new_size)
    return bytes(result)


def parse_trailing_entries(trailing: bytes) -> list[dict]:
    """Parse all entries from @ARCH000 trailing asset bundle index.

    Format:
      Header: [count: int32 LE] [desc_len: byte] [desc: bytes]
      Entry 0 (main bundle, no name): [offset: int64 LE] [size: int64 LE]
      Entries 1+: [marker: 0x4e] [seg1_len: byte] [seg1] [seg2_len: byte] [seg2] [offset: int64 LE] [size: int64 LE]

    Returns list of {name, offset, size, raw_pos} dicts where raw_pos points to the offset field.
    """
    if len(trailing) < 5:
        return []

    entries = []
    count = struct.unpack_from('<I', trailing, 0)[0]
    desc_len = trailing[4]
    pos = 5 + desc_len

    while pos < len(trailing) and len(entries) < count:
        if len(entries) == 0:
            # First entry: no marker, just offset + size
            offset = struct.unpack_from('<q', trailing, pos)[0]
            size = struct.unpack_from('<q', trailing, pos + 8)[0]
            entries.append({'name': '(main)', 'offset': offset, 'size': size, 'raw_pos': pos})
            pos += 16
        else:
            marker = trailing[pos]
            if marker != 0x4e:
                break
            pos += 1

            # Read path segments
            path_parts = []
            while pos < len(trailing):
                seg_len = trailing[pos]
                pos += 1
                if seg_len == 0:
                    if path_parts:
                        break
                    continue
                if pos + seg_len > len(trailing):
                    break
                seg = trailing[pos:pos + seg_len].decode('ascii', errors='replace')
                pos += seg_len
                path_parts.append(seg)

                # Check if next bytes form a valid offset (heuristic: value between 23 and 4GB,
                # and the following 8 bytes form a reasonable size)
                if pos + 16 <= len(trailing):
                    peek_off = struct.unpack_from('<q', trailing, pos)[0]
                    peek_sz = struct.unpack_from('<q', trailing, pos + 8)[0]
                    if 23 <= peek_off <= 4_000_000_000 and 1 <= peek_sz <= 200_000_000:
                        break

            name = '/'.join(path_parts) if path_parts else '?'

            if pos + 16 > len(trailing):
                break
            offset = struct.unpack_from('<q', trailing, pos)[0]
            size = struct.unpack_from('<q', trailing, pos + 8)[0]
            entries.append({'name': name, 'offset': offset, 'size': size, 'raw_pos': pos})
            pos += 16

    return entries


def apply_trailing_shifts(trailing: bytes, mods: list[tuple[int, int, int]]) -> bytes:
    """Update @ARCH000 trailing index for size changes in sub-bundles.

    mods: sorted list of (old_offset, old_size, new_size)
    For each modified entry: updates its size.
    For each subsequent entry: shifts its offset by cumulative delta.

    Uses the correct @ARCH000 trailing format (count + 'assetbundle' header,
    variable-length named entries with 0x4e marker).
    """
    entries = parse_trailing_entries(trailing)
    if not entries:
        return trailing

    result = bytearray(trailing)

    # Compute cumulative shift per modification point
    cumulative = 0
    shift_points = []  # (old_offset, cumulative_delta_after_this_point)
    for old_off, old_sz, new_sz in sorted(mods, key=lambda m: m[0]):
        delta = new_sz - old_sz
        if delta != 0:
            cumulative += delta
            shift_points.append((old_off, cumulative))

    if not shift_points:
        return trailing

    for entry in entries:
        # Update size if this entry was modified
        for old_off, old_sz, new_sz in mods:
            if entry['offset'] == old_off and entry['size'] == old_sz:
                struct.pack_into('<q', result, entry['raw_pos'] + 8, new_sz)
                break

        # Shift offset for entries after any modification point
        entry_new_offset = entry['offset']
        for old_off, cum in shift_points:
            if entry['offset'] > old_off:
                entry_new_offset = entry['offset'] + cum

        if entry_new_offset != entry['offset']:
            struct.pack_into('<q', result, entry['raw_pos'], entry_new_offset)

    return bytes(result)


def apply_translations_direct(decompressed: bytearray, original: str, translated: str,
                              info: dict = None) -> bool:
    """Find and replace a Lua script string in decompressed Unity serialized data.

    Searches for `original` as UTF-8 bytes, replaces with `translated` as UTF-8 bytes.
    Updates the 4-byte int32 length prefix before the string.
    If info dict is provided, also updates the per-block decompressed chunks.
    Returns True if replacement was made.
    """
    old_bytes = original.encode('utf-8')
    new_bytes = translated.encode('utf-8')

    pos = decompressed.find(old_bytes)
    if pos < 0:
        return False

    # Replace string content
    delta = len(new_bytes) - len(old_bytes)
    decompressed[pos:pos + len(old_bytes)] = new_bytes

    # Update length prefix
    if pos >= 4:
        old_prefix = struct.unpack('<I', bytes(decompressed[pos-4:pos]))[0]
        new_prefix = old_prefix + delta
        decompressed[pos-4:pos] = struct.pack('<I', max(0, new_prefix))

    # Update per-block chunks so rebuild can identify changed blocks
    if info and 'decompressed_chunks' in info and 'chunk_offsets' in info:
        chunks = info['decompressed_chunks']
        offsets = info['chunk_offsets']
        for i in range(len(chunks)):
            chunk_start = offsets[i]
            chunk_end = chunk_start + len(chunks[i]) if i + 1 < len(offsets) else len(decompressed)
            if pos >= chunk_start and pos < chunk_end:
                # Update this chunk to match the corresponding portion of decompressed
                new_chunk_data = decompressed[chunk_start:chunk_end]
                chunks[i] = bytearray(new_chunk_data)
                break

    return True
