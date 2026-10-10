"""Steam entry resolver regression tests."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.path_resolver import resolve_game_path
from core.steam import extract_app_id, parse_vdf, resolve_steam_entry


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_extract_app_id():
    assert extract_app_id("steam://run/12345") == "12345"
    assert extract_app_id("steam://rungameid/67890") == "67890"
    assert extract_app_id(r'"C:\Steam\steam.exe" -applaunch 24680') == "24680"
    assert extract_app_id("appmanifest_13579.acf") == "13579"


def test_parse_vdf():
    data = parse_vdf(
        '''
        "libraryfolders"
        {
            "0"
            {
                "path" "C:\\\\Program Files (x86)\\\\Steam"
            }
            "1" "D:\\\\SteamLibrary"
        }
        '''
    )
    folders = data["libraryfolders"]
    assert folders["0"]["path"].endswith("Steam")
    assert folders["1"].endswith("SteamLibrary")


def test_resolve_steam_url_to_library_game_dir():
    old_roots = os.environ.get("GT_STEAM_ROOTS")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        steam = root / "Steam"
        library = root / "Library"
        game_dir = library / "steamapps" / "common" / "Demo Game"
        game_dir.mkdir(parents=True)

        _write(
            steam / "steamapps" / "libraryfolders.vdf",
            f'''
            "libraryfolders"
            {{
                "0"
                {{
                    "path" "{str(steam).replace("\\", "\\\\")}"
                }}
                "1"
                {{
                    "path" "{str(library).replace("\\", "\\\\")}"
                }}
            }}
            ''',
        )
        _write(
            library / "steamapps" / "appmanifest_4242.acf",
            '''
            "AppState"
            {
                "appid" "4242"
                "name" "Demo Game"
                "installdir" "Demo Game"
            }
            ''',
        )
        shortcut = root / "Demo.url"
        _write(
            shortcut,
            "[InternetShortcut]\nURL=steam://rungameid/4242\n",
        )

        os.environ["GT_STEAM_ROOTS"] = str(steam)
        try:
            assert resolve_steam_entry("steam://run/4242") == game_dir
            assert Path(resolve_game_path(shortcut)) == game_dir
            assert Path(resolve_game_path(library / "steamapps" / "appmanifest_4242.acf")) == game_dir
        finally:
            if old_roots is None:
                os.environ.pop("GT_STEAM_ROOTS", None)
            else:
                os.environ["GT_STEAM_ROOTS"] = old_roots


def test_exe_resolves_to_selected_file():
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / "Game.exe"
        exe.write_bytes(b"MZ")
        assert Path(resolve_game_path(exe)) == exe


def test_existing_directory_is_not_reinterpreted_as_steam_entry():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "appid=4242"
        game_dir.mkdir()
        old_roots = os.environ.get("GT_STEAM_ROOTS")
        os.environ["GT_STEAM_ROOTS"] = str(Path(tmp) / "missing-steam")
        try:
            assert Path(resolve_game_path(game_dir)) == game_dir
        finally:
            if old_roots is None:
                os.environ.pop("GT_STEAM_ROOTS", None)
            else:
                os.environ["GT_STEAM_ROOTS"] = old_roots


if __name__ == "__main__":
    for fn in [
        test_extract_app_id,
        test_parse_vdf,
        test_resolve_steam_url_to_library_game_dir,
        test_exe_resolves_to_selected_file,
        test_existing_directory_is_not_reinterpreted_as_steam_entry,
    ]:
        fn()
    print("Steam resolver tests passed")
