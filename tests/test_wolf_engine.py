from __future__ import annotations

import json
import hashlib
import os
import struct
import tempfile
from pathlib import Path

import pytest

from core import tool_manager
from core.preflight import run_preflight
from engines.base import TextItem
from engines.wolf.csv_text import apply_script_translations, extract_script_text_items
from engines.wolf.detect import confidence
from engines.wolf.engine import WolfEngine
from engines.wolf.json_text import apply_translations, extract_text_items
from engines.wolf import native_bridge
from engines.wolf.native_bridge import inspect as native_inspect
from engines.wolf.native_bridge import _redact_command
from engines.wolf.profile_store import (
    ProtectedArchiveProfile,
    lookup_verified_profile,
    store_verified_profile,
    temporary_key_file,
)
from engines.wolf.runtime_keys import (
    snapshot_custom_database_keys,
    verify_custom_database_keys,
)
from engines.wolf.toolchain import (
    WolfState,
    _detect_pack_index,
    _native_pack_and_verify,
    _native_pro_archive,
    _prepare_archived_game,
    _standard_pack_and_verify,
    _windows_file_version,
    bundled_uberwolf_path,
    repack as repack_wolf,
    resolve_uberwolf_path,
    resolve_json_target,
    run_hidden,
)


def _make_loose_layout(root: Path) -> Path:
    data = root / "Data"
    (data / "BasicData").mkdir(parents=True)
    (data / "MapData").mkdir()
    (data / "BasicData" / "Game.dat").write_bytes(b"game")
    (data / "BasicData" / "CommonEvent.dat").write_bytes(b"common")
    (data / "MapData" / "Map001.mps").write_bytes(b"map")
    (root / "Game.exe").write_bytes(b"MZ")
    return data


def _require_bundled_wolf_cli() -> None:
    """Skip (never fail) when the Wolf sidecar binaries are absent.

    The UberWolf CLI and the text bridge are third-party binaries that are not
    distributed with the source: the app downloads them on demand. A checkout
    without them cannot exercise the code paths that read those files, so these
    tests behave like the other optional-asset ones in this suite rather than
    reporting a broken engine.
    """
    if not bundled_uberwolf_path().is_file():
        pytest.skip("bundled Wolf CLI is not present in this checkout")
    if not native_bridge.bridge_path().is_file():
        pytest.skip("bundled Wolf native bridge is not present in this checkout")


def test_wolf_detection_uses_root_level_evidence(tmp_path: Path, monkeypatch):
    _make_loose_layout(tmp_path)
    monkeypatch.setattr(Path, "rglob", lambda *_args, **_kwargs: pytest.fail("detect must not deep-scan"))

    score, evidence = confidence(tmp_path / "Game.exe")

    assert score == 98
    assert any("WOLF" in item for item in evidence)


def test_chromium_pak_without_wolf_layout_is_not_detected(tmp_path: Path):
    (tmp_path / "app.exe").write_bytes(b"MZ")
    (tmp_path / "resources.pak").write_bytes(b"chromium")

    assert confidence(tmp_path) == (0, [])


def test_generic_game_exe_without_wolf_layout_is_not_detected(tmp_path: Path):
    (tmp_path / "Game.exe").write_bytes(b"MZ")

    assert confidence(tmp_path / "Game.exe") == (0, [])


def test_uberwolf_tool_spec_uses_cli_release_only():
    spec = tool_manager.get_tool_spec("uberwolf")

    assert spec is not None
    assert spec.github_repo == "Sinflower/UberWolf"
    assert spec.executable_names == ("UberWolfCli.exe",)
    assert "uberwolfcli.exe" in spec.asset_includes
    assert tool_manager.get_tool_spec("wolfdec") is spec
    if not bundled_uberwolf_path().is_file():
        pytest.skip("bundled Wolf CLI is not present in this checkout")
    assert bundled_uberwolf_path().is_file()
    assert hashlib.sha256(bundled_uberwolf_path().read_bytes()).hexdigest() == (
        "fffbe66caf10699865010217aeabe3a3684ec9320ffe461268f1c9509fda8917"
    )


def test_wolf_job_uses_bundled_cli_without_downloading(monkeypatch):
    _require_bundled_wolf_cli()
    monkeypatch.setattr("engines.wolf.toolchain.find_tool", lambda _name: None)

    assert resolve_uberwolf_path() == bundled_uberwolf_path()


def test_wolf_json_extract_and_repack_preserves_line_break_style(tmp_path: Path):
    json_root = tmp_path / "json"
    maps = json_root / "maps"
    maps.mkdir(parents=True)
    source = {
        "events": [{
            "id": 1,
            "name": "internal",
            "pages": [{
                "id": 0,
                "list": [{
                    "code": 101,
                    "codeStr": "Message",
                    "stringArgs": ["@1\r\rこんにちは\r\r二行目です\r\r"],
                    "index": 0,
                }],
            }],
        }],
    }
    path = maps / "Map001.json"
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")

    items = extract_text_items(json_root, lambda _rel: ("Data/MapData/Map001.mps", "Map001.mps"))
    assert len(items) == 1
    assert items[0].original == "こんにちは\n二行目です"
    assert items[0].meta["wolf_prefix"] == "@1\r\r"
    items[0].translated = "你好\n这是第二行"

    assert apply_translations(json_root, items) == 1
    patched = json.loads(path.read_text(encoding="utf-8"))
    value = patched["events"][0]["pages"][0]["list"][0]["stringArgs"][0]
    assert value == "@1\r\r你好\r\r这是第二行\r\r"


def test_wolf_extracts_visible_commands_but_not_comments_or_picture_paths(tmp_path: Path):
    root = tmp_path / "json" / "common_events"
    root.mkdir(parents=True)
    payload = {
        "id": 1,
        "name": "呼出名は変更しない",
        "description": "editor only",
        "commands": [
            {"codeStr": "Comment", "stringArgs": ["翻訳しないコメント"], "index": 0},
            {"codeStr": "Choices", "stringArgs": ["はい", "いいえ"], "index": 1},
            {"codeStr": "Picture", "intArgs": [0], "stringArgs": ["Picture/a.png"], "index": 2},
            {"codeStr": "Picture", "intArgs": [2 << 4], "stringArgs": ["画面テキスト"], "index": 3},
        ],
    }
    (root / "1_test.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    items = extract_text_items(tmp_path / "json", lambda _rel: ("Data/BasicData/CommonEvent.dat", "CommonEvent.dat"))

    assert [item.original for item in items] == ["はい", "いいえ", "画面テキスト"]
    assert [item.meta["kind"] for item in items] == ["choice", "choice", "ui/system"]


def test_wolf_filters_internal_database_fields_and_reports_reasons(tmp_path: Path):
    databases = tmp_path / "json" / "databases"
    databases.mkdir(parents=True)
    payload = {
        "types": [{
            "data": [{
                "name": "ポーション",
                "data": [
                    {"name": "説明", "value": "体力を回復する。"},
                    {"name": "ｺﾒﾝﾄ", "value": "内部メモ"},
                    {"name": "検索タグ", "value": "ｼｽﾃﾑ_全SE停止"},
                    {"name": "SE1", "value": "SE/決定音.wav\n100\n100"},
                    {"name": "移動指示", "value": "前進3"},
                    {"name": "テキストEnglish", "value": "English fallback"},
                ],
            }],
        }],
    }
    (databases / "DataBase.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    stats: dict[str, int] = {}

    items = extract_text_items(
        tmp_path / "json",
        lambda _rel: ("Data.wolf", "Data/BasicData/DataBase.dat"),
        stats,
    )

    assert {item.original for item in items} == {"体力を回復する。"}
    assert items[0].meta["kind"] == "ui/system"
    assert stats == {
        "alternate_language": 1,
        "audio_resource": 1,
        "database_record_name": 1,
        "editor_comment": 1,
        "movement_directive": 1,
        "search_tag": 1,
    }


def test_wolf_filters_string_conditions_and_control_only_references(tmp_path: Path):
    root = tmp_path / "json" / "common_events"
    root.mkdir(parents=True)
    payload = {
        "commands": [
            {"codeStr": "StringCondition", "stringArgs": ["内部比較キー"], "index": 0},
            {"codeStr": "SetString", "stringArgs": [r"\cdb[52:\cself[1]:1]"], "index": 1},
            {"codeStr": "SetString", "stringArgs": [r"第\cself[1]世界"], "index": 2},
        ],
    }
    (root / "1_test.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    stats: dict[str, int] = {}

    items = extract_text_items(
        tmp_path / "json",
        lambda _rel: ("Data.wolf", "Data/BasicData/CommonEvent.dat"),
        stats,
    )

    assert [item.original for item in items] == [r"第\cself[1]世界"]
    assert stats["string_condition"] == 1


def test_wolf_filters_internal_runtime_diagnostics(tmp_path: Path):
    root = tmp_path / "json" / "common_events"
    root.mkdir(parents=True)
    payload = {
        "commands": [
            {
                "codeStr": "Message",
                "stringArgs": [r"\>「X[移]装備装着・解除」エラー：主人公IDが-1以下です。"],
            },
            {
                "codeStr": "SetString",
                "stringArgs": ["ここの値は「自動システム初期化」処理でセットされます"],
            },
            {"codeStr": "Message", "stringArgs": ["実際に表示する文章です。"]},
        ]
    }
    (root / "diagnostic.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    stats: dict[str, int] = {}

    items = extract_text_items(
        tmp_path / "json",
        lambda _rel: ("Data.wolf", "Data/BasicData/CommonEvent.dat"),
        stats,
    )

    assert [item.original for item in items] == ["実際に表示する文章です。"]
    assert items[0].meta["kind"] == "message"
    assert stats["internal_diagnostic"] == 2


def test_all_wolf_database_names_are_runtime_keys_not_translation_text(tmp_path: Path):
    databases = tmp_path / "json" / "databases"
    databases.mkdir(parents=True)
    custom = {
        "types": [{
            "data": [{
                "name": "メニュー消去フラグ",
                "data": [
                    {"name": "数値", "value": "0"},
                    {"name": "文字列", "value": "表示する文章"},
                ],
            }],
        }],
    }
    ordinary = {
        "types": [{
            "data": [{"name": "ポーション", "data": []}],
        }],
    }
    (databases / "CDataBase.json").write_text(json.dumps(custom, ensure_ascii=False), encoding="utf-8")
    (databases / "DataBase.json").write_text(json.dumps(ordinary, ensure_ascii=False), encoding="utf-8")

    items = extract_text_items(tmp_path / "json", lambda _rel: ("Data.wolf", "Data/BasicData/DataBase.dat"))

    assert {item.original for item in items} == {"表示する文章"}
    assert all(item.original not in {"メニュー消去フラグ", "ポーション"} for item in items)


def test_wolf_game_title_is_identity_but_startup_message_is_translatable(tmp_path: Path):
    game = tmp_path / "json" / "game"
    game.mkdir(parents=True)
    (game / "Game.json").write_text(
        json.dumps({
            "Title": "ロエロールの幻淫城",
            "TitlePlus": "追加タイトル",
            "StartUpMsg": "ゲームを開始します。",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    stats: dict[str, int] = {}

    items = extract_text_items(tmp_path / "json", lambda _rel: ("Data.wolf", "Game.dat"), stats)

    assert [item.original for item in items] == ["ゲームを開始します。"]
    assert stats["game_identity"] == 2


def test_wolf_custom_database_runtime_key_fingerprint_rejects_changes(tmp_path: Path):
    databases = tmp_path / "json" / "databases"
    databases.mkdir(parents=True)
    path = databases / "CDataBase.json"
    payload = {
        "types": [{
            "data": [
                {"name": "戦闘中フラグ", "data": []},
                {"name": "現在パーティー人数", "data": []},
            ],
        }],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    count, fingerprint = snapshot_custom_database_keys(tmp_path / "json")

    assert verify_custom_database_keys(tmp_path / "json", count, fingerprint) == {
        "checked": True,
        "count": 2,
        "fingerprint_match": True,
    }
    payload["types"][0]["data"][0]["name"] = "战斗中标志"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="runtime keys changed"):
        verify_custom_database_keys(tmp_path / "json", count, fingerprint)


def test_wolf_json_patch_rejects_changed_source(tmp_path: Path):
    root = tmp_path / "json" / "game"
    root.mkdir(parents=True)
    path = root / "Game.json"
    path.write_text(json.dumps({"Title": "新しいゲーム"}, ensure_ascii=False), encoding="utf-8")
    item = TextItem(
        file="Data/BasicData/Game.dat",
        key="/Title",
        original="古いゲーム",
        translated="新游戏",
        meta={"wolf_json": "game/Game.json", "wolf_pointer": "/Title"},
    )

    with pytest.raises(ValueError, match="source changed"):
        apply_translations(tmp_path / "json", [item])


def test_wolf_repack_filter_rejects_database_names_and_game_identity():
    engine = WolfEngine()
    items = [
        TextItem(
            file="Data.wolf",
            key="/types/0/data/0/name",
            original="システム基本設定",
            translated="系统基本设置",
            meta={"wolf_role": "database_name", "wolf_pointer": "/types/0/data/0/name"},
        ),
        TextItem(
            file="Data.wolf",
            key="/Title",
            original="ロエロールの幻淫城",
            translated="罗艾罗尔的幻淫城",
            meta={"wolf_role": "game", "wolf_pointer": "/Title"},
        ),
        TextItem(
            file="Data.wolf",
            key="/commands/1/stringArgs/0",
            original="こんにちは。",
            translated="你好。",
            meta={"wolf_role": "command", "wolf_pointer": "/commands/1/stringArgs/0"},
        ),
    ]

    assert engine.filter_repack_items(items) == [items[2]]
    assert engine.diagnostics_snapshot()["runtime_key_translations_dropped"] == 2


def test_wolf_script_csv_extract_and_patch_preserves_multiline_quoted_cell(tmp_path: Path):
    data_root = tmp_path / "Data"
    script_root = data_root / "Script"
    script_root.mkdir(parents=True)
    source = script_root / "1.csv"
    source.write_text(
        "STP,MAP,2,3,4,5,6,7,Text,10\r\n"
        "1,4,0,0,0,0,0,0,短い台詞,\r\n"
        "1,4,0,0,0,0,0,0,\"長い、\"\"引用\"\"です。\r\n二行目です。\",\r\n",
        encoding="utf-8",
        newline="",
    )
    original_bytes = source.read_bytes()

    items = extract_script_text_items(
        data_root,
        lambda _path: ("Data.wolf", "Data/Script/1.csv"),
    )

    assert [item.original for item in items] == [
        "短い台詞",
        "長い、\"引用\"です。\n二行目です。",
    ]
    assert items[1].line == 3
    assert items[1].meta["wolf_csv_encoding"] == "utf-8"
    items[0].translated = "短句中文"
    items[1].translated = "较长的中文，包含\"引用\"。\n这是第二行。"

    generated = tmp_path / "generated"
    assert apply_script_translations(data_root, generated, items) == 2
    assert source.read_bytes() == original_bytes
    with (generated / "Script" / "1.csv").open("r", encoding="utf-8", newline="") as stream:
        patched = stream.read()
    assert "短句中文" in patched
    assert '"较长的中文，包含""引用""。\r\n这是第二行。"' in patched
    assert patched.count("\r\n") == 4


def test_wolf_script_csv_preserves_cp932_and_rejects_unencodable_chinese(tmp_path: Path):
    data_root = tmp_path / "Data"
    script_root = data_root / "Script"
    script_root.mkdir(parents=True)
    source = script_root / "1.csv"
    source.write_bytes("a,b,c,d,e,f,g,h,Text\r\na,b,c,d,e,f,g,h,テスト\r\n".encode("cp932"))
    items = extract_script_text_items(data_root, lambda _path: ("Data.wolf", "Data/Script/1.csv"))

    assert len(items) == 1
    assert items[0].meta["wolf_csv_encoding"] == "cp932"
    items[0].translated = "English text"
    assert apply_script_translations(data_root, tmp_path / "generated", items) == 1
    assert "English text" in (tmp_path / "generated" / "Script" / "1.csv").read_bytes().decode("cp932")

    items[0].translated = "emoji \U0001f600"
    with pytest.raises(RuntimeError, match="cannot store Chinese"):
        apply_script_translations(data_root, tmp_path / "generated", items)


def test_wolf_script_csv_patch_rejects_changed_source(tmp_path: Path):
    data_root = tmp_path / "Data"
    script_root = data_root / "Script"
    script_root.mkdir(parents=True)
    source = script_root / "1.csv"
    source.write_text("a,b,c,d,e,f,g,h,Text\na,b,c,d,e,f,g,h,原文\n", encoding="utf-8")
    item = extract_script_text_items(data_root, lambda _path: ("Data.wolf", "Data/Script/1.csv"))[0]
    item.translated = "译文"
    source.write_text("a,b,c,d,e,f,g,h,Text\na,b,c,d,e,f,g,h,已被修改\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source changed"):
        apply_script_translations(data_root, tmp_path / "generated", [item])


def test_wolf_native_bridge_inspects_pro_archive(tmp_path: Path):
    archive = tmp_path / "Data.wolf"
    header = bytearray(48)
    header[:2] = b"DX"
    struct.pack_into("<H", header, 46, 1000)
    archive.write_bytes(header)

    info, result = native_inspect(archive)

    assert result["exit_code"] == 0
    assert info.crypt_version == 1000
    assert info.protection == "pro"


def test_wolf_native_bridge_header_fallback_preserves_archive_classification(tmp_path: Path, monkeypatch):
    archive = tmp_path / "Data.wolf"
    header = bytearray(48)
    header[:2] = b"DX"
    struct.pack_into("<H", header, 46, 1000)
    archive.write_bytes(header)

    monkeypatch.setattr(
        native_bridge,
        "_run",
        lambda *_args, **_kwargs: {
            "command": ["engaixt_wolf_native.exe", "inspect", str(archive)],
            "exit_code": -1073741795,
            "duration_ms": 1,
            "stdout": "",
            "stderr": "",
        },
    )

    info, result = native_inspect(archive)

    assert info == native_bridge.NativeArchiveInfo(crypt_version=1000, protection="pro")
    assert result["exit_code"] == 0
    assert result["native_exit_code"] == -1073741795
    assert result["inspection_backend"] == "python_header"


def test_wolf_native_pro_candidate_is_limited_to_single_pro_archive(tmp_path: Path):
    archive = tmp_path / "Data.wolf"
    header = bytearray(48)
    header[:2] = b"DX"
    struct.pack_into("<H", header, 46, 1000)
    archive.write_bytes(header)
    assert _native_pro_archive([archive]) == (archive, 1000)
    assert _native_pro_archive([archive, tmp_path / "MapData.wolf"]) is None


@pytest.mark.skipif(os.name != "nt", reason="WOLF protected profiles use Windows DPAPI")
def test_wolf_verified_profile_is_encrypted_and_job_key_is_ephemeral(tmp_path: Path):
    archive = tmp_path / "Data.wolf"
    archive.write_bytes(b"protected archive fixture")
    store = tmp_path / "profiles.json"
    key_hex = "4142434400010203"

    key_id = store_verified_profile(archive, 1000, key_hex, path=store)
    profile = lookup_verified_profile(archive, 1000, path=store)

    assert profile is not None
    assert profile.key_id == key_id
    assert profile.validation == "clone_runtime_v1"
    assert profile.key_hex == key_hex.lower()
    assert key_hex.lower() not in store.read_text(encoding="utf-8").lower()
    with temporary_key_file(tmp_path, profile.key_hex) as key_file:
        assert key_file.read_text(encoding="ascii") == key_hex.lower()
    assert not key_file.exists()


def test_wolf_native_command_redacts_ephemeral_key_path():
    command = ["bridge.exe", "unpack", "--key-file", "C:/secret/key.hex", "--crypt-version", "1000"]

    assert _redact_command(command)[3] == "<ephemeral>"
    assert "C:/secret/key.hex" not in _redact_command(command)


def test_wolf_pro_without_verified_profile_stops_before_translation(tmp_path: Path, monkeypatch):
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    exe = game_dir / "GamePro.exe"
    exe.write_bytes(b"MZ")
    archive = game_dir / "Data.wolf"
    header = bytearray(48)
    header[:2] = b"DX"
    struct.pack_into("<H", header, 46, 1000)
    archive.write_bytes(header)
    cli = tmp_path / "UberWolfCli.exe"
    cli.write_bytes(b"MZ")
    monkeypatch.setattr("engines.wolf.toolchain.resolve_uberwolf_path", lambda: cli)
    monkeypatch.setattr("engines.wolf.toolchain.find_wolf_exe", lambda _path: exe)
    monkeypatch.setattr("engines.wolf.toolchain.native_bridge_available", lambda: True)
    monkeypatch.setattr("engines.wolf.toolchain.lookup_verified_profile", lambda *_args: None)
    monkeypatch.setattr(
        "engines.wolf.toolchain.run_hidden",
        lambda *_args, **_kwargs: {"exit_code": 5, "stderr": "key missing", "stdout": "", "duration_ms": 1},
    )

    with pytest.raises(RuntimeError, match="没有匹配的已验证 profile"):
        _prepare_archived_game(exe, game_dir, tmp_path / "workspace" / "wolf", [archive])


def test_wolf_pro_verified_profile_uses_native_unpack(tmp_path: Path, monkeypatch):
    _require_bundled_wolf_cli()
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    exe = game_dir / "GamePro.exe"
    exe.write_bytes(b"MZ")
    archive = game_dir / "Data.wolf"
    header = bytearray(48)
    header[:2] = b"DX"
    struct.pack_into("<H", header, 46, 1000)
    archive.write_bytes(header)
    cli = tmp_path / "UberWolfCli.exe"
    cli.write_bytes(b"MZ")
    profile = ProtectedArchiveProfile("archive-id", 1000, "clone_runtime_v1", "41424300")
    native_calls = []

    monkeypatch.setattr("engines.wolf.toolchain.resolve_uberwolf_path", lambda: cli)
    monkeypatch.setattr("engines.wolf.toolchain.find_wolf_exe", lambda _path: exe)
    monkeypatch.setattr("engines.wolf.toolchain.native_bridge_available", lambda: True)
    monkeypatch.setattr("engines.wolf.toolchain.lookup_verified_profile", lambda *_args: profile)
    monkeypatch.setattr("engines.wolf.toolchain.native_bridge_path", lambda: cli)
    monkeypatch.setattr(
        "engines.wolf.toolchain.run_hidden",
        lambda *_args, **_kwargs: {
            "command": ["UberWolfCli.exe"],
            "exit_code": 5,
            "stderr": "key missing",
            "stdout": "",
            "duration_ms": 1,
        },
    )

    def fake_native_unpack(staged_archive, key_file, crypt_version):
        native_calls.append((staged_archive, key_file.exists(), crypt_version))
        _make_loose_layout(staged_archive.parent)
        return {"command": ["native"], "exit_code": 0, "stderr": "", "stdout": "", "duration_ms": 1}

    monkeypatch.setattr("engines.wolf.toolchain.native_unpack", fake_native_unpack)

    state = _prepare_archived_game(exe, game_dir, tmp_path / "workspace" / "wolf", [archive])

    assert state.native_archive == "Data.wolf"
    assert state.native_key_id == "archive-id"
    assert state.native_profile_validation == "clone_runtime_v1"
    assert native_calls[0][1:] == (True, 1000)
    assert not (tmp_path / "workspace" / "wolf" / ".secrets").exists()


def test_wolf_native_repack_requires_roundtrip_unpack(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    staged = workspace / "wolf" / "staged_game"
    data_root = _make_loose_layout(staged)
    archive = staged / "Data.wolf"
    archive.write_bytes(b"original")
    json_root = workspace / "wolf" / "json"
    json_root.mkdir(parents=True)
    state = WolfState(
        game_dir=str(tmp_path / "game"),
        data_root=str(data_root),
        json_root=str(json_root),
        staged_root=str(staged),
        staged_exe=str(staged / "Game.exe"),
        archives=["Data.wolf"],
        native_archive="Data.wolf",
        native_crypt_version=1000,
        native_key_id="archive-id",
        native_profile_validation="clone_runtime_v1",
    )
    profile = ProtectedArchiveProfile("archive-id", 1000, "clone_runtime_v1", "41424300")
    calls = []

    monkeypatch.setattr("engines.wolf.toolchain.lookup_verified_profile_by_id", lambda *_args: profile)

    def fake_pack(source, key_file, crypt_version):
        calls.append(("pack", key_file.exists(), crypt_version))
        (source.parent / "Data.wolf").write_bytes(b"translated archive")
        return {"command": ["pack"], "exit_code": 0, "stderr": "", "stdout": "", "duration_ms": 1}

    def fake_unpack(target, key_file, crypt_version):
        calls.append(("unpack", key_file.exists(), crypt_version))
        _make_loose_layout(target.parent)
        return {"command": ["unpack"], "exit_code": 0, "stderr": "", "stdout": "", "duration_ms": 1}

    monkeypatch.setattr("engines.wolf.toolchain.native_pack", fake_pack)
    monkeypatch.setattr("engines.wolf.toolchain.native_unpack", fake_unpack)
    monkeypatch.setattr(
        "engines.wolf.toolchain.run_hidden",
        lambda command, timeout: {
            "command": command,
            "exit_code": 0,
            "stderr": "",
            "stdout": "",
            "duration_ms": 1,
        },
    )

    result, runtime_keys = _native_pack_and_verify(workspace, state, data_root)

    assert result["exit_code"] == 0
    assert runtime_keys["fingerprint_match"] is True
    assert calls == [("pack", True, 1000), ("unpack", True, 1000)]
    assert not (workspace / "wolf" / ".secrets").exists()


def test_wolf_standard_repack_requires_roundtrip_unpack(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    staged = workspace / "wolf" / "staged_game"
    staged.mkdir(parents=True)
    exe = staged / "Game.exe"
    archive = staged / "Data.wolf"
    exe.write_bytes(b"MZ")
    archive.write_bytes(b"translated archive")
    state = WolfState(
        game_dir=str(tmp_path / "game"),
        data_root=str(staged / "Data"),
        json_root=str(workspace / "wolf" / "json"),
        staged_root=str(staged),
        staged_exe=str(exe),
        uberwolf=str(tmp_path / "UberWolfCli.exe"),
        archives=["Data.wolf"],
    )
    calls: list[list[str]] = []

    def fake_run(command, timeout):
        calls.append(command)
        if command[-1] == "--unprotect":
            _make_loose_layout(Path(command[1]).parent)
        return {
            "command": command,
            "exit_code": 0,
            "stderr": "",
            "stdout": "",
            "duration_ms": 1,
        }

    monkeypatch.setattr("engines.wolf.toolchain.run_hidden", fake_run)

    runtime_keys = _standard_pack_and_verify(workspace, state)

    assert runtime_keys == {"checked": False, "count": 0, "fingerprint_match": True}
    assert calls[0][-1] == "--unprotect"
    assert calls[1][1] == "verify"


def test_resolve_target_maps_archived_data_back_to_archive(tmp_path: Path):
    staged = tmp_path / "staged"
    source = staged / "Data" / "MapData" / "Map001.mps"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"map")
    state = WolfState(
        game_dir=str(tmp_path / "game"),
        data_root=str(staged / "Data"),
        json_root=str(tmp_path / "json"),
        staged_root=str(staged),
        archives=["Data/MapData.wolf"],
    )

    target, source_rel = resolve_json_target(state, "maps/Map001.json")

    assert target == "Data/MapData.wolf"
    assert source_rel == "Data/MapData/Map001.mps"


def test_hidden_tool_runner_records_contract_without_launch(monkeypatch, tmp_path: Path):
    calls = []

    class Completed:
        returncode = 0
        stdout = b"ok"
        stderr = b""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    monkeypatch.setattr("engines.wolf.toolchain.subprocess.run", fake_run)
    result = run_hidden([str(tmp_path / "UberWolfCli.exe"), "Game.exe"], timeout=12)

    assert result["exit_code"] == 0
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["check"] is False
    assert calls[0][1]["timeout"] == 12
    assert "shell" not in calls[0][1]


def test_loose_repack_never_modifies_game_before_copy_back(tmp_path: Path, monkeypatch):
    game_dir = tmp_path / "game"
    data_root = _make_loose_layout(game_dir)
    original_game = (data_root / "BasicData" / "Game.dat").read_bytes()
    original_map = (data_root / "MapData" / "Map001.mps").read_bytes()
    workspace = tmp_path / "workspace"
    json_root = workspace / "wolf" / "json"
    json_root.mkdir(parents=True)
    state = WolfState(
        game_dir=str(game_dir),
        data_root=str(data_root),
        json_root=str(json_root),
    )

    def fake_run(command, timeout):
        if command[1] == "apply":
            generated = Path(command[4])
            (generated / "BasicData").mkdir(parents=True)
            (generated / "MapData").mkdir()
            (generated / "BasicData" / "Game.dat").write_bytes(b"translated-game")
            (generated / "MapData" / "Map001.mps").write_bytes(b"translated-map")
        elif command[1] == "verify":
            assert Path(command[2]) == workspace / "wolf" / "generated_data"
        return {
            "command": command,
            "exit_code": 0,
            "duration_ms": 1,
            "stdout": "",
            "stderr": "",
        }

    monkeypatch.setattr("engines.wolf.toolchain.run_hidden", fake_run)

    result = repack_wolf(workspace, state)

    assert (data_root / "BasicData" / "Game.dat").read_bytes() == original_game
    assert (data_root / "MapData" / "Map001.mps").read_bytes() == original_map
    assert (workspace / "original" / "Data" / "BasicData" / "Game.dat").read_bytes() == b"translated-game"
    assert (workspace / "original" / "Data" / "MapData" / "Map001.mps").read_bytes() == b"translated-map"
    assert sorted(result["outputs"]) == [
        "Data/BasicData/Game.dat",
        "Data/MapData/Map001.mps",
    ]


def test_wolf_repack_runs_script_patch_before_validation(tmp_path: Path, monkeypatch):
    game_dir = tmp_path / "game"
    data_root = _make_loose_layout(game_dir)
    workspace = tmp_path / "workspace"
    json_root = workspace / "wolf" / "json"
    json_root.mkdir(parents=True)
    state = WolfState(
        game_dir=str(game_dir),
        data_root=str(data_root),
        json_root=str(json_root),
    )
    callback_called = False

    def fake_run(command, timeout):
        generated = workspace / "wolf" / "generated_data"
        if command[1] == "apply":
            (generated / "BasicData").mkdir(parents=True)
            (generated / "BasicData" / "Game.dat").write_bytes(b"game")
        elif command[1] == "verify":
            assert callback_called is True
            assert (generated / "Script" / "1.csv").read_text(encoding="utf-8") == "translated"
        return {
            "command": command,
            "exit_code": 0,
            "duration_ms": 1,
            "stdout": "",
            "stderr": "",
        }

    def patch_script(generated: Path) -> None:
        nonlocal callback_called
        callback_called = True
        (generated / "Script").mkdir(parents=True, exist_ok=True)
        (generated / "Script" / "1.csv").write_text("translated", encoding="utf-8")

    monkeypatch.setattr("engines.wolf.toolchain.run_hidden", fake_run)

    result = repack_wolf(workspace, state, post_apply=patch_script)

    assert callback_called is True
    assert "Data/Script/1.csv" in result["outputs"]


def test_wolf_capabilities_are_static_beta():
    engine = WolfEngine()
    caps = engine.get_capabilities()

    assert engine.support_level == "beta"
    assert caps.extract is True
    assert caps.repack is True
    assert caps.static_patch is True
    assert caps.runtime_patch is False
    assert caps.needs_external_tool is True


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ((2, 1, 0, 0), 0),
        ((2, 10, 0, 0), 1),
        ((2, 20, 0, 0), 2),
        ((2, 25, 0, 0), 3),
        ((3, 0, 0, 0), 4),
        ((3, 14, 0, 0), 5),
        ((3, 31, 0, 0), 6),
        ((3, 71, 0, 0), 7),
    ],
)
def test_pack_mode_falls_back_to_game_version(tmp_path: Path, monkeypatch, version, expected):
    archive = tmp_path / "Data.wolf"
    archive.write_bytes(b"not-a-modern-dx-header")
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr("engines.wolf.toolchain._windows_file_version", lambda _path: version)

    assert _detect_pack_index([archive], exe) == expected


def test_windows_version_reader_handles_official_wolf_sample_when_available():
    # Only runs where the official WOLF RPG Editor sample happens to be
    # installed under the temp directory; skipped elsewhere. Point
    # ENGAIXT_WOLF_SAMPLE at any official Game.exe to force it.
    override = os.environ.get("ENGAIXT_WOLF_SAMPLE")
    if override:
        sample = Path(override)
    else:
        sample = (
            Path(tempfile.gettempdir())
            / "WolfRPGEditor_3.712" / "WOLF_RPG_Editor3" / "Game.exe"
        )
    if not sample.is_file():
        pytest.skip("official local WOLF sample is not installed")

    assert _windows_file_version(sample)[:2] == (3, 712)


def test_wolf_preflight_accepts_bundled_uberwolf(tmp_path: Path):
    _require_bundled_wolf_cli()
    game = tmp_path / "Game.exe"
    game.write_bytes(b"MZ")

    result = run_preflight(game, WolfEngine())
    check = next(row for row in result["checks"] if row["name"] == "tool:uberwolf")

    assert check["status"] == "ok"
    assert "内置 UberWolfCli" in check["detail"]
