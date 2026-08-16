"""Patch translated text directly in LZ4-compressed data streams.

Key insight: LZ4 stores literal bytes directly. By replacing the Japanese text
within the LZ4 literal sequences with padded Chinese text of the same byte length,
we can modify content without changing ANY file structure.

Produces byte-identical compressed data except for the modified literal bytes.
Compressed size, block sizes, total_size, and all offsets remain unchanged.
"""

import struct
import io
from typing import Optional


def decompress_lz4_stream(data: bytes, uncomp_size: int) -> Optional[bytes]:
    """Decompress LZ4 data and return mapping of decompressed positions -> compressed positions."""
    result = bytearray()
    pos = 0  # position in compressed data

    while pos < len(data) and len(result) < uncomp_size:
        token = data[pos]
        pos += 1

        # Literal length
        literal_len = token >> 4
        if literal_len == 15:
            while True:
                extra = data[pos]
                pos += 1
                literal_len += extra
                if extra < 255:
                    break

        # Copy literals
        if pos + literal_len > len(data):
            break
        result.extend(data[pos:pos + literal_len])
        pos += literal_len

        # If we've reached the end
        if len(result) >= uncomp_size or pos >= len(data):
            break

        # Match offset
        if pos + 2 > len(data):
            break
        offset = struct.unpack_from('<H', data, pos)[0]
        pos += 2

        # Match length
        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while True:
                extra = data[pos]
                pos += 1
                match_len += extra
                if extra < 255:
                    break

        # Copy from previous output
        copy_from = len(result) - offset
        for _ in range(match_len):
            if copy_from < 0 or copy_from >= len(result):
                break
            result.append(result[copy_from])
            copy_from += 1

    return bytes(result[:uncomp_size])


def build_decomp_to_comp_map(data: bytes, uncomp_size: int) -> Optional[dict]:
    """Build mapping: decompressed_position -> compressed_position for literal bytes.

    Returns dict[int, int] where dict[decomp_pos] = comp_pos.
    Only literal bytes are mapped; match-copied bytes are not in the mapping.
    """
    mapping = {}
    result = bytearray()
    pos = 0  # position in compressed data

    while pos < len(data) and len(result) < uncomp_size:
        token = data[pos]
        comp_token_pos = pos
        pos += 1

        # Literal length
        literal_len = token >> 4
        if literal_len == 15:
            while pos < len(data):
                extra = data[pos]
                pos += 1
                literal_len += extra
                if extra < 255:
                    break

        # Map literal bytes
        if pos + literal_len > len(data):
            break
        for i in range(literal_len):
            if len(result) + i < uncomp_size:
                mapping[len(result) + i] = pos + i
        result.extend(data[pos:pos + literal_len])
        pos += literal_len

        if len(result) >= uncomp_size or pos >= len(data):
            break

        # Match offset
        if pos + 2 > len(data):
            break
        offset = struct.unpack_from('<H', data, pos)[0]
        pos += 2

        # Match length
        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while pos < len(data):
                extra = data[pos]
                pos += 1
                match_len += extra
                if extra < 255:
                    break

        # Copy from previous output
        copy_from = len(result) - offset
        for _ in range(match_len):
            if copy_from < 0 or copy_from >= len(result):
                break
            result.append(result[copy_from])
            copy_from += 1

    return mapping if len(result) >= uncomp_size else None


def patch_lz4_stream(comp_data: bytes, uncomp_size: int,
                     search: bytes, replace: bytes) -> Optional[bytes]:
    """Replace `search` with `replace` in an LZ4-compressed block.

    Both search and replace must have the SAME byte length!
    Modifies the literal bytes in the compressed stream directly.
    Returns modified compressed data, or None if search not found.
    """
    if len(search) != len(replace):
        raise ValueError(f"search and replace must have same length: {len(search)} vs {len(replace)}")

    # First decompress to find the position of search text
    decompressed = decompress_lz4_stream(comp_data, uncomp_size)
    if decompressed is None:
        return None

    pos = decompressed.find(search)
    if pos < 0:
        return None

    # Build mapping from decomp positions to compressed positions
    mapping = build_decomp_to_comp_map(comp_data, uncomp_size)
    if mapping is None:
        return None

    # Check that ALL bytes of `search` are in literal positions (not match-copied)
    # If a byte is match-copied, we can't modify it directly
    comp_positions = []
    for i in range(len(search)):
        decomp_pos = pos + i
        if decomp_pos not in mapping:
            # This byte is from a match copy, can't patch directly
            return None
        comp_positions.append(mapping[decomp_pos])

    # Verify the compressed positions match what we expect
    verify = bytes(comp_data[cp] for cp in comp_positions)
    if verify != search:
        return None

    # Patch the compressed data
    result = bytearray(comp_data)
    for i, cp in enumerate(comp_positions):
        result[cp] = replace[i]

    # Verify roundtrip
    new_decomp = decompress_lz4_stream(bytes(result), uncomp_size)
    if new_decomp is None:
        return None
    expected = decompressed[:pos] + replace + decompressed[pos + len(search):]
    if new_decomp[:len(expected)] != expected:
        return None

    return bytes(result)


def patch_translations_in_blocks(raw_compressed_blocks: list[bytes],
                                 blocks: list[tuple[int, int, int]],
                                 original: str, translated: str,
                                 pad_byte: int = 0x00) -> Optional[list[bytes]]:
    """Try to patch translations directly in LZ4 compressed blocks.

    Pads `translated` to match `original` byte length.
    Returns modified list of compressed blocks, or None if patching failed
    (in which case the caller should fall back to normal recompression).
    """
    old_bytes = original.encode('utf-8')
    new_bytes = translated.encode('utf-8')

    if len(new_bytes) > len(old_bytes):
        # Translation is longer than original — can't pad shorter
        return None

    # Pad to match
    padded = new_bytes + bytes([pad_byte] * (len(old_bytes) - len(new_bytes)))

    result = []
    for i, (comp_data, (u_size, c_size, flags)) in enumerate(zip(raw_compressed_blocks, blocks)):
        comp_type = flags & 0x3F
        if comp_type in (2, 3):  # LZ4 or LZ4HC
            patched = patch_lz4_stream(bytes(comp_data), u_size, old_bytes, padded)
            if patched is not None:
                result.append(patched)
                continue
        # Couldn't patch — must recompress
        result.append(None)

    # Check if all blocks were patched
    if all(r is not None for r in result):
        return result
    return None
