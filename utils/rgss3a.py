"""RGSS3A encrypted archive decoder for RPG Maker VX Ace.

Format (matching rpgmad-lib / rpgm-archive-decrypter):
  Header:   8 bytes  "RGSSAD" + 0x00 + 0x03
  BaseKey:  4 bytes  raw key from archive
  MagicKey = base_key * 9 + 3

  File table (starts at offset 12):
    Each entry: 4 x uint32 LE (all XOR'd with MagicKey)
      offset, size, file_key, name_len
    Then name_len bytes (each XOR'd with MagicKey & 0xFF)

  File data: byte-level XOR with rotating key (matching xor_data in rpgmad-lib)
    key = file_key; for each byte: byte ^ key_bytes[pos%4];
    key rotates every 4 bytes: key = key * 7 + 3
"""

import struct
from pathlib import Path


def _xor_data(key: int, data: bytes | bytearray) -> bytearray:
    """Byte-level XOR with rotating key, matching rpgmad-lib's xor_data.

    Every byte is XOR'd with the current key's corresponding byte.
    Key rotates every 4 bytes: key = key * 7 + 3.
    """
    result = bytearray()
    key_bytes = struct.pack("<I", key)
    for i, b in enumerate(data):
        if i > 0 and i % 4 == 0:
            key = (key * 7 + 3) & 0xFFFFFFFF
            key_bytes = struct.pack("<I", key)
        result.append(b ^ key_bytes[i % 4])
    return result


def extract_rgss3a(archive_path: Path, output_dir: Path) -> list[Path]:
    """Decrypt and extract an RGSS3A archive. Returns list of extracted file paths."""
    data = archive_path.read_bytes()

    if data[:6] != b"RGSSAD":
        raise ValueError(f"Not an RGSSAD archive: {archive_path}")
    version = data[7]
    if version != 3:
        raise ValueError(f"Unsupported RGSSAD version: {version} (expected 3 for VX Ace)")

    base_key = struct.unpack_from("<I", data, 8)[0]
    magic_key = (base_key * 9 + 3) & 0xFFFFFFFF

    output_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    pos = 12

    while True:
        if pos + 16 > len(data):
            break

        entry = struct.unpack_from("<IIII", data, pos)
        offset = entry[0] ^ magic_key
        size = entry[1] ^ magic_key
        file_key = entry[2] ^ magic_key
        name_len = entry[3] ^ magic_key

        if offset == 0:
            break

        if offset + size > len(data) or name_len > 1024:
            break

        pos += 16

        # Decrypt filename
        name_enc = data[pos:pos + name_len]
        if len(name_enc) < name_len:
            break
        name_bytes = bytearray()
        key_bytes = struct.pack("<I", magic_key)
        for i in range(name_len):
            name_bytes.append(name_enc[i] ^ key_bytes[i % 4])
        filename = name_bytes.decode("utf-8", errors="replace")
        pos += name_len

        # Decrypt file data (byte-level XOR, matching rpgmad-lib)
        file_enc = data[offset:offset + size]
        decrypted = _xor_data(file_key, file_enc)

        out_path = output_dir / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(bytes(decrypted))
        extracted.append(out_path)

    return extracted


def get_archive_metadata(archive_path: Path) -> tuple[list[str], dict[str, int], int]:
    """Read file order, original file_keys, and base_key from an existing RGSS3A archive.

    Returns (file_order, file_keys, base_key).
    """
    data = archive_path.read_bytes()
    base_key = struct.unpack_from("<I", data, 8)[0]
    magic_key = (base_key * 9 + 3) & 0xFFFFFFFF
    files = []
    file_keys = {}
    pos = 12
    while True:
        if pos + 16 > len(data):
            break
        entry = struct.unpack_from("<IIII", data, pos)
        offset = entry[0] ^ magic_key
        size = entry[1] ^ magic_key
        file_key = entry[2] ^ magic_key
        name_len = entry[3] ^ magic_key
        if offset == 0:
            break
        if offset + size > len(data) or name_len > 1024:
            break
        pos += 16
        name_enc = data[pos:pos + name_len]
        if len(name_enc) < name_len:
            break
        key_bytes = struct.pack("<I", magic_key)
        name_bytes = bytearray()
        for i in range(name_len):
            name_bytes.append(name_enc[i] ^ key_bytes[i % 4])
        filename = name_bytes.decode("utf-8", errors="replace")
        pos += name_len
        files.append(filename)
        file_keys[filename] = file_key
    return files, file_keys, base_key


def get_archive_file_order(archive_path: Path) -> list[str]:
    """Read the file order from an existing RGSS3A archive."""
    files, _, _ = get_archive_metadata(archive_path)
    return files


def pack_rgss3a(input_dir: Path, archive_path: Path, base_key: int | None = None,
                file_order: list[str] | None = None,
                file_keys: dict[str, int] | None = None):
    """Pack files back into an RGSS3A archive. Uses original base_key if available.

    If file_order is provided, files are written in that order (preserving
    the original archive's file table order, which some games require).

    If file_keys is provided, those keys are reused for encryption.
    Preserving original file_keys is critical for VX Ace compatibility.
    """
    files: list[tuple[str, bytes]] = []
    if file_order:
        file_set = {str(f.relative_to(input_dir)).replace("/", "\\") for f in input_dir.rglob("*") if f.is_file()}
        for name in file_order:
            if name in file_set:
                fpath = input_dir / name
                if fpath.is_file():
                    files.append((name, fpath.read_bytes()))
    else:
        for f in sorted(input_dir.rglob("*")):
            if f.is_file():
                rel = str(f.relative_to(input_dir)).replace("/", "\\")
                files.append((rel, f.read_bytes()))

    if base_key is None:
        import random
        base_key = random.randint(0, 0x7FFFFFFF)

    magic_key = (base_key * 9 + 3) & 0xFFFFFFFF

    header = b"RGSSAD\x00\x03" + struct.pack("<I", base_key)

    table_size = 0
    for name, _data in files:
        table_size += 16 + len(name.encode("utf-8"))

    data_offset = 12 + table_size + 16

    table_bytes = bytearray()
    data_chunks: list[bytes] = []

    for name, raw in files:
        name_enc = bytearray(name.encode("utf-8"))
        key_bytes = struct.pack("<I", magic_key)
        for i in range(len(name_enc)):
            name_enc[i] ^= key_bytes[i % 4]

        if file_keys and name in file_keys:
            file_key = file_keys[name]
        else:
            import random
            file_key = random.randint(0, 0x7FFFFFFF)

        # Encrypt file data (byte-level XOR, matching rpgmad-lib)
        encrypted = _xor_data(file_key, raw)

        entry = struct.pack("<IIII",
            data_offset ^ magic_key,
            len(raw) ^ magic_key,
            file_key ^ magic_key,
            len(name_enc) ^ magic_key,
        )
        table_bytes.extend(entry)
        table_bytes.extend(name_enc)

        data_chunks.append(bytes(encrypted))
        data_offset += len(encrypted)

    terminator = struct.pack("<IIII",
        0 ^ magic_key,
        0 ^ magic_key,
        0 ^ magic_key,
        0 ^ magic_key,
    )

    with open(archive_path, "wb") as f:
        f.write(header)
        f.write(table_bytes)
        f.write(terminator)
        for chunk in data_chunks:
            f.write(chunk)
