"""Main executable selection regression tests."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.exe_selector import find_main_exe


def _exe(path: Path, size: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"\0" * max(0, size - 2))


def _pe_exe(path: Path, text_size: int, total_size: int | None = None):
    """Write a minimal PE exe with a .text section of the given size (bytes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    total = total_size or (0x1000 + text_size)
    data = bytearray(total)
    data[:2] = b"MZ"
    pe_off = 0x80
    data[0x3C:0x40] = pe_off.to_bytes(4, "little")
    data[pe_off:pe_off + 4] = b"PE\0\0"
    data[pe_off + 4:pe_off + 6] = (0x14C).to_bytes(2, "little")  # i386
    data[pe_off + 6:pe_off + 8] = (1).to_bytes(2, "little")      # 1 section
    data[pe_off + 20:pe_off + 22] = (0xE0).to_bytes(2, "little")  # optional header size
    sec_off = pe_off + 24 + 0xE0
    data[sec_off:sec_off + 8] = b".text"
    data[sec_off + 8:sec_off + 12] = text_size.to_bytes(4, "little")       # virtual size
    data[sec_off + 16:sec_off + 20] = text_size.to_bytes(4, "little")       # raw size
    data[sec_off + 36:sec_off + 40] = (0x60000020).to_bytes(4, "little")    # code section flags
    path.write_bytes(bytes(data))


def test_prefers_full_engine_over_packed_stub():
    with tempfile.TemporaryDirectory() as tmp:
        game = Path(tmp) / "Nemophilia -We pass each other-"
        _pe_exe(game / "Nemophilia.exe", 2 * 1024 * 1024, 4 * 1024 * 1024)
        _pe_exe(game / "Nemophilia_.exe", 22 * 1024, 4 * 1024 * 1024)
        assert find_main_exe(game) == game / "Nemophilia.exe"


def test_packed_stub_still_wins_when_alone():
    with tempfile.TemporaryDirectory() as tmp:
        game = Path(tmp) / "Solo"
        _pe_exe(game / "Solo.exe", 22 * 1024, 3 * 1024 * 1024)
        assert find_main_exe(game) == game / "Solo.exe"


def test_prefers_directory_named_game_exe_over_helpers():
    with tempfile.TemporaryDirectory() as tmp:
        game = Path(tmp) / "Voidigo"
        _exe(game / "netlog.exe", 54 * 1024)
        _exe(game / "config.exe", 200 * 1024)
        _exe(game / "Voidigo.exe", 30 * 1024 * 1024)
        assert find_main_exe(game) == game / "Voidigo.exe"


def test_prefers_unity_data_matching_exe():
    with tempfile.TemporaryDirectory() as tmp:
        game = Path(tmp) / "Some Game"
        (game / "RealGame_Data").mkdir(parents=True)
        _exe(game / "CrashReporter.exe", 128 * 1024)
        _exe(game / "Launcher.exe", 2 * 1024 * 1024)
        _exe(game / "RealGame.exe", 5 * 1024 * 1024)
        assert find_main_exe(game) == game / "RealGame.exe"


def test_skips_nested_tool_exe_when_root_exe_exists():
    with tempfile.TemporaryDirectory() as tmp:
        game = Path(tmp) / "Demo"
        _exe(game / "Demo.exe", 8 * 1024 * 1024)
        _exe(game / "tools" / "tool.exe", 30 * 1024 * 1024)
        assert find_main_exe(game, recursive=True) == game / "Demo.exe"


def test_skips_installer_and_uninstaller_even_when_larger():
    with tempfile.TemporaryDirectory() as tmp:
        game = Path(tmp) / "NSM_MYST_DL"
        _exe(game / "BGIForInstalling.exe", 2198528)
        _exe(game / "UnInstaller.exe", 2834432)
        _exe(game / "BHVC.exe", 333824)
        real = game / "NSM_MYST.exe"
        _exe(real, 2180096)

        assert find_main_exe(game) == real


def test_recursive_scan_skips_translation_runtime_launchers():
    with tempfile.TemporaryDirectory() as tmp:
        game = Path(tmp) / "Runtime Game"
        real = game / "Game.exe"
        _exe(real, 512 * 1024)
        _exe(game / "_translation_meta" / "bgi_native_launcher.exe", 30 * 1024 * 1024)

        assert find_main_exe(game, recursive=True) == real


if __name__ == "__main__":
    for fn in [
        test_prefers_directory_named_game_exe_over_helpers,
        test_prefers_unity_data_matching_exe,
        test_skips_nested_tool_exe_when_root_exe_exists,
        test_skips_installer_and_uninstaller_even_when_larger,
        test_recursive_scan_skips_translation_runtime_launchers,
        test_prefers_full_engine_over_packed_stub,
        test_packed_stub_still_wins_when_alone,
    ]:
        fn()
    print("Exe selector tests passed")
