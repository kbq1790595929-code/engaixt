import json
import base64
import struct
import sys
import threading
import zipfile
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from engines.base import TextItem
from core.pipeline import (
    _clean_kirikiri_checkpoint_items,
    _collect_kirikiri_dump_targets,
    _extend_kirikiri_targets_from_dump_refs,
    _infer_kirikiri_sequential_targets,
    _kirikiri_count_threshold_sufficient,
    _kirikiri_is_extensionless_dump_target,
    _kirikiri_is_dump_target,
    _expand_kirikiri_target_name,
    _load_kirikiri_scn_ref_index,
    _missing_kirikiri_active_dump_targets,
    _prepare_kirikiri_auto_dump_seed_targets,
    _write_krkrdump_config,
)
from engines.kirikiri import (
    _GarbroEntry,
    KiriKiriEngine,
    XP3_SIGNATURE,
    _GarbroListResult,
    _clean_runtime_capture_text,
    _diagnose_xp3_archive,
    _diagnosis_to_dict,
    _append_xp3_replacement_index,
    _patch_xp3_replacements_in_original_slots,
    _decode_kirikiri_sjis_tunnel_text,
    _apply_koihazi_xp3dec_filter,
    _garbro_script_entries,
    _descramble_kirikiri_text,
    _extract_kirikiri_text_spans,
    _is_kirikiri_runtime_overlay_patch_file,
    _read_xp3_entry_bytes,
    _read_xp3_index,
    _scramble_kirikiri_text,
    _select_script_xp3_files,
    _write_xp3_patch,
    _rewrite_xp3_with_replacements,
    _expand_kirikiri_storage_refs,
    export_kirikiri_dump_targets_from_xp3,
    import_kirikiri_external_dump,
    prepare_kirikiri_dump_targets_from_game,
)
from engines.kirikiri.external_tools import ExternalToolResult
from engines.kirikiri.patch_policy import changed_items_require_stream_bridge
from utils.kirikiri_psb import extract_kirikiri_scn_storage_refs, extract_kirikiri_scn_texts, patch_kirikiri_scn_texts


def _hook_source() -> str:
    return (Path(__file__).parent.parent / "frida" / "kirikiri_realtime_hook.js").read_text(encoding="utf-8")


def _make_game(tmp_path: Path) -> Path:
    game = tmp_path / "game"
    game.mkdir()
    (game / "krkr.exe").write_bytes(b"MZ")
    return game


def test_kirikiri_kag_control_labels_are_not_translatable_text():
    spans = _extract_kirikiri_text_spans("[button text='新規' target='*click_start']")

    texts = [span.text for span in spans]
    assert texts == ["新規"]
    assert "*click_start" not in texts


def test_kirikiri_speaker_command_text_attr_is_not_translatable_text():
    spans = _extract_kirikiri_text_spans('[章吾 vo=vo9_0001 text="同級生"]')

    assert spans == []


def test_kirikiri_inline_macro_visible_tail_is_translatable_text():
    spans = _extract_kirikiri_text_spans("[>>]なんだよ、またお前と同じクラスか[<<][c]")

    assert [span.text for span in spans] == ["なんだよ、またお前と同じクラスか"]


def test_kirikiri_command_expression_brackets_are_not_visible_text():
    spans = _extract_kirikiri_text_spans(
        "[jump storage=\"&tf.EV_EventListInfo[f.EV_NowEventNum][0] + '.ks'\"]"
    )

    assert spans == []


def test_kirikiri_plain_function_call_is_not_visible_text():
    spans = _extract_kirikiri_text_spans("NextSceneGetInfo();")

    assert spans == []


def test_kirikiri_extracts_inline_ruby_sentence_as_visible_text():
    line = (
        '\u79c1\u300a[ruby text="\u3052\u3068\u3046"][ch text="\u590f\u6cb9"]'
        '\u3000[ruby text="\u3042\u304b\u306d"][ch text="\u831c"]\u300b'
        '\u306f\u6614\u304b\u3089\u5f15\u3063\u8fbc\u307f\u601d\u6848\u3060\u3063\u305f\u3002'
    )

    spans = _extract_kirikiri_text_spans(line)

    assert [span.text for span in spans] == [line]


def test_kirikiri_repack_filters_legacy_control_label_translations():
    engine = KiriKiriEngine()
    items = [
        TextItem(file="system/SysTitle.ks", original="*click_start", translated="*开始游戏"),
        TextItem(file="scenario/intro.ks", original="こんにちは", translated="你好"),
    ]

    filtered = engine.filter_repack_items(items)

    assert [item.original for item in filtered] == ["こんにちは"]


def test_kirikiri_find_exe_ignores_mtool_helper_when_game_exe_exists(tmp_path):
    game = tmp_path / "恋愛、はじめまして"
    game.mkdir()
    (game / "MTool_Game.exe").write_bytes(b"MZ" + b"\0" * (5 * 1024 * 1024))
    (game / "ファイル破損チェックツール.exe").write_bytes(b"MZ" + b"\0" * (600 * 1024))
    (game / "恋愛、はじめまして.exe").write_bytes(b"MZ" + b"\0" * (4 * 1024 * 1024))

    assert KiriKiriEngine().find_exe(game) == game / "恋愛、はじめまして.exe"


def test_kirikiri_repack_keeps_flat_xp3_scenario_scripts():
    engine = KiriKiriEngine()
    items = [
        TextItem(
            file="00_01.ks",
            original="こんにちは",
            translated="你好",
            meta={"from_xp3": True, "xp3_filter": {"archive": "data_scenario_n.xp3", "key": 149}},
        ),
        TextItem(
            file="mode_re.ks",
            original="リターン",
            translated="回想",
            meta={"from_xp3": True},
        ),
        TextItem(
            file="startup.tjs",
            original="タイトル",
            translated="标题",
            meta={"from_xp3": True},
        ),
        TextItem(
            file="MainWindow.tjs",
            original="タイトル",
            translated="标题",
            meta={"from_xp3": True},
        ),
        TextItem(
            file="system/SysTitle.ks",
            original="タイトル",
            translated="标题",
            meta={"from_xp3": True},
        ),
    ]

    filtered = engine.filter_repack_items(items)

    assert [item.file for item in filtered] == ["00_01.ks"]


def test_kirikiri_repack_filters_system_logic_scripts():
    engine = KiriKiriEngine()
    items = [
        TextItem(file="00_02.ks", original="hello", translated="你好", meta={"from_xp3": True}),
        TextItem(file="exsystembutton.ks", original="button", translated="按钮", meta={"from_xp3": True}),
        TextItem(file="macro.ks", original="macro", translated="宏", meta={"from_xp3": True}),
        TextItem(file="config.ks", original="config", translated="设置", meta={"from_xp3": True}),
        TextItem(file="ButtonLinkPlugin.ks", original="plugin", translated="插件", meta={"from_xp3": True}),
    ]

    filtered = engine.filter_repack_items(items)

    assert [item.file for item in filtered] == ["00_02.ks"]


def test_kirikiri_repack_filters_scenario_db_and_startup_logic_scripts():
    engine = KiriKiriEngine()
    items = [
        TextItem(file="scenario/main/0_1.ks", original="old", translated="new", meta={"from_xp3": True}),
        TextItem(file="scenario/DB/Output/stand_info.ks", original="old", translated="new", meta={"from_xp3": True}),
        TextItem(file="scenario/CommonClass/window.ks", original="old", translated="new", meta={"from_xp3": True}),
        TextItem(file="scenario/avan/ADV_Start.ks", original="old", translated="new", meta={"from_xp3": True}),
    ]

    filtered = engine.filter_repack_items(items)

    assert [item.file for item in filtered] == ["scenario/main/0_1.ks"]


def test_kirikiri_extract_skips_iscript_blocks(tmp_path):
    game = _make_game(tmp_path)
    script = game / "scenario.ks"
    script.write_text(
        "[iscript]\n"
        "if(enabled)\n"
        "    表示ボタン;\n"
        "[endscript]\n"
        "こんにちは。\n",
        encoding="utf-8",
    )

    items = KiriKiriEngine().unpack(game, tmp_path / "ws")

    assert [item.original for item in items] == ["こんにちは。"]


def test_kirikiri_patch_only_cleans_legacy_bad_checkpoint_items():
    cleaned = _clean_kirikiri_checkpoint_items([
        {"original": "*click_start", "translated": "*开始游戏"},
        {"original": "こんにちは", "translated": "????"},
        {"original": "こんばんは", "translated": "晚上好"},
    ])

    assert cleaned[0]["translated"] == ""
    assert cleaned[1]["translated"] == ""
    assert cleaned[2]["translated"] == "晚上好"


def test_kirikiri_detects_plain_scripts(tmp_path):
    game = _make_game(tmp_path)
    (game / "startup.tjs").write_text('var title = "起動";', encoding="utf-8")
    (game / "scenario.ks").write_text("こんにちは\n", encoding="utf-8")

    engine = KiriKiriEngine()

    assert engine.detect(game)
    score, evidence = engine.detect_confidence(game)
    assert score > 0
    assert evidence


def test_kirikiri_extracts_plain_kag_and_tjs_text(tmp_path):
    game = _make_game(tmp_path)
    (game / "scenario.ks").write_text(
        ";comment\n"
        "@bg storage=room\n"
        "こんにちは、世界。\n"
        "[link text='はじめる' target=*start]\n"
        "[名前 id=【良】]\n"
        "[「]愛奈すげーな！　マジで『ステラレンジャー』じゃん！」\n"
        "*label\n",
        encoding="utf-8",
    )
    (game / "system.tjs").write_text(
        'var title = "タイトル";\n'
        'menu.caption = "設定";\n'
        'var id = "CONFIG_ID";\n',
        encoding="utf-8",
    )

    engine = KiriKiriEngine()
    items = engine.unpack(game, tmp_path / "ws")

    texts = {item.original for item in items}
    assert "こんにちは、世界。" in texts
    assert "はじめる" in texts
    assert "愛奈すげーな！　マジで『ステラレンジャー』じゃん！」" in texts
    assert "【良】" not in texts
    assert "タイトル" in texts
    assert "設定" in texts
    assert "CONFIG_ID" not in texts


def test_kirikiri_tjs_extraction_skips_code_noise(tmp_path):
    game = _make_game(tmp_path)
    (game / "startup.tjs").write_text(
        'with(kag.fore.layers[global.FREE_LAYER]) {\n'
        '.setImageSize(scWidth, scHeight);\n'
        '"ゞ々’”）〕］｝〉》」』】°′″℃¢％‰"; // 行頭禁則文字\n'
        'face: "ＭＳ ゴシック",\n'
        'System.inform("画像を保存しました。", "画像を保存しました");\n'
        'menu.caption = "設定";\n',
        encoding="utf-8",
    )

    items = KiriKiriEngine().unpack(game, tmp_path / "ws")
    texts = {item.original for item in items}

    assert "画像を保存しました。" in texts
    assert "設定" in texts
    assert "with(kag.fore.layers[global.FREE_LAYER]) {" not in texts
    assert ".setImageSize(scWidth, scHeight);" not in texts
    assert "ＭＳ ゴシック" not in texts


def test_kirikiri_repack_keeps_source_encoding_and_newline(tmp_path):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    source_script = game / "scenario.ks"
    source_script.write_bytes("こんにちは\r\n次の行\r\n".encode("cp932"))

    engine = KiriKiriEngine()
    items = engine.unpack(game, ws)
    for item in items:
        if item.original == "こんにちは":
            item.translated = "你好"

    engine.repack(items, ws)

    script = ws / "original" / "scenario.ks"
    data = script.read_bytes()
    assert data.startswith(b"\xff\xfe")
    decoded = data.decode("utf-16")
    assert "\r\n" in decoded
    assert "你好" in decoded


def test_kirikiri_repack_preserves_cp932_when_translation_fits(tmp_path):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    source_script = game / "scenario.ks"
    source_script.write_bytes("こんにちは\r\n".encode("cp932"))

    engine = KiriKiriEngine()
    items = engine.unpack(game, ws)
    for item in items:
        if item.original == "こんにちは":
            item.translated = "さようなら"

    engine.repack(items, ws)

    data = (ws / "original" / "scenario.ks").read_bytes()
    assert not data.startswith(b"\xff\xfe")
    assert "さようなら" in data.decode("cp932")


@pytest.mark.parametrize("mode", [0, 1, 2])
def test_kirikiri_scrambled_script_roundtrip(mode):
    text = "@bg storage=room\nこんにちは、世界。\n"

    encoded = _scramble_kirikiri_text(text, mode)
    decoded = _descramble_kirikiri_text(encoded)

    assert encoded.startswith(b"\xfe\xfe" + bytes([mode]) + b"\xff\xfe")
    assert decoded == (text, mode)


def test_kirikiri_extracts_and_repatches_scrambled_xp3_scripts(tmp_path):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    root = tmp_path / "root"
    (root / "scenario").mkdir(parents=True)
    script = root / "scenario" / "intro.ks"
    script.write_bytes(_scramble_kirikiri_text("こんにちは、世界。\n", 1))
    _write_xp3_patch(game / "data.xp3", root, [script])

    engine = KiriKiriEngine()
    items = engine.unpack(game, ws)
    item = next(i for i in items if i.original == "こんにちは、世界。")
    item.translated = "你好，世界。"

    engine.repack(items, ws)

    patched = ws / "original" / "scenario" / "intro.ks"
    data = patched.read_bytes()
    decoded = _descramble_kirikiri_text(data)
    assert data.startswith(b"\xfe\xfe\x01\xff\xfe")
    assert decoded is not None
    assert "你好，世界。" in decoded[0]
    assert item.meta["scramble_mode"] == 1


def test_kirikiri_unpack_reports_current_archive_and_script_to_gui(tmp_path):
    game = _make_game(tmp_path)
    root = tmp_path / "root"
    (root / "scenario").mkdir(parents=True)
    script = root / "scenario" / "intro.ks"
    script.write_text("こんにちは、世界。\n", encoding="utf-8")
    _write_xp3_patch(game / "scenario.xp3", root, [script])

    events = []
    engine = KiriKiriEngine()
    engine._progress = lambda message, percent: events.append((message, percent))

    items = engine.unpack(game, tmp_path / "ws")

    assert items
    assert ("KRKR：准备解析 1 个脚本封包", 20.0) in events
    assert any(message.startswith("KRKR：读取 scenario.xp3 索引") for message, _ in events)
    assert any(message.startswith("KRKR：提取 scenario.xp3 脚本") for message, _ in events)
    assert any("KRKR：解析脚本文本（1/1）：scenario/intro.ks" == message for message, _ in events)
    assert {percent for _, percent in events} == {20.0}


def test_kirikiri_repack_builds_patch_xp3_for_xp3_items(tmp_path):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    original = ws / "original"
    (original / "data").mkdir(parents=True)
    script = original / "data" / "scenario.ks"
    script.write_text("こんにちは\n", encoding="utf-8")

    engine = KiriKiriEngine()
    engine._game_dir = game
    item = TextItem(
        file="data/scenario.ks",
        original="こんにちは",
        translated="你好",
        line=1,
        meta={"from_xp3": True, "encoding": "utf-8", "newline": "\n"},
    )

    engine.repack([item], ws)

    patch = original / "patch.xp3"
    deployed = game / "patch.xp3"
    assert patch.exists()
    assert deployed.exists()
    assert patch.read_bytes().startswith(XP3_SIGNATURE)
    assert _xp3_index_contains(patch, "data/scenario.ks")
    assert (game / "_translation_meta" / "kirikiri_patch" / "data" / "scenario.ks").exists()
    assert (game / "_translation_meta" / "kirikiri_patch_manifest.txt").read_text(encoding="utf-8") == "data/scenario.ks\n"


def test_kirikiri_runtime_dump_repack_uses_patch_bridge_without_root_patch(tmp_path):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    original = ws / "original"
    (original / "scenario").mkdir(parents=True)
    script = original / "scenario" / "intro.txt.scn"
    script.write_bytes(_reference_magalumina_scn())
    (game / "patch.xp3").write_bytes(b"old root patch")

    refs = extract_kirikiri_scn_texts(script.read_bytes())
    engine = KiriKiriEngine()
    engine._game_dir = game
    item = TextItem(
        file="scenario/intro.txt.scn",
        original=refs[0].text,
        translated="\u6d4b\u8bd5\u4e2d\u6587\u56de\u586b\u3002",
        line=1,
        meta={
            "from_xp3": True,
            "runtime_dump": True,
            "format": "kirikiri_psb_scn",
            "psb_index": refs[0].index,
            "psb_path": refs[0].path,
        },
    )

    engine.repack([item], ws)

    assert not (game / "patch.xp3").exists()
    assert (game / "_translation_meta" / "kirikiri_patch.xp3").exists()
    assert (game / "_translation_meta" / "kirikiri_patch" / "scenario" / "intro.txt.scn").exists()
    diag = json.loads((game / "_translation_meta" / "kirikiri_patch_diagnostics.json").read_text(encoding="utf-8"))
    assert diag["root_patch"] is False


def test_kirikiri_runtime_dump_repack_uses_patch_bridge_from_game_meta(tmp_path):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    original = ws / "original"
    (original / "scenario").mkdir(parents=True)
    script = original / "scenario" / "intro.txt.scn"
    script.write_bytes(_reference_magalumina_scn())
    meta = game / "_translation_meta"
    meta.mkdir()
    (meta / "kirikiri_dump.zip").write_bytes(b"placeholder")
    (game / "patch.xp3").write_bytes(b"old root patch")

    refs = extract_kirikiri_scn_texts(script.read_bytes())
    engine = KiriKiriEngine()
    engine._game_dir = game
    item = TextItem(
        file="scenario/intro.txt.scn",
        original=refs[0].text,
        translated="\u6d4b\u8bd5\u4e2d\u6587\u56de\u586b\u3002",
        line=1,
        meta={
            "from_xp3": True,
            "runtime_dump": True,
            "format": "kirikiri_psb_scn",
            "psb_index": refs[0].index,
            "psb_path": refs[0].path,
        },
    )

    engine.repack([item], ws)

    assert not (game / "patch.xp3").exists()
    assert (game / "_translation_meta" / "kirikiri_patch.xp3").exists()
    diag = json.loads((game / "_translation_meta" / "kirikiri_patch_diagnostics.json").read_text(encoding="utf-8"))
    assert diag["root_patch"] is False


def test_kirikiri_static_items_ignore_unrelated_protected_archive_for_patch_route(tmp_path):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    original = ws / "original"
    original.mkdir(parents=True)
    script = original / "scenario.ks"
    script.write_text("こんにちは\n", encoding="utf-8")

    engine = KiriKiriEngine()
    engine._game_dir = game
    # This models a data.xp3 candidate that could not be parsed while the
    # selected scenario script was safely extracted from another archive.
    engine._protected_archives = ["data.xp3"]
    item = TextItem(
        file="scenario.ks",
        original="こんにちは",
        translated="你好",
        line=1,
        meta={"from_xp3": True, "encoding": "utf-8", "newline": "\n"},
    )

    assert changed_items_require_stream_bridge({"scenario.ks": [item]}) is False
    assert engine._should_use_patch_bridge(game, {"scenario.ks": [item]}) is False

    engine.repack([item], ws)

    assert (game / "patch.xp3").exists()
    diag = json.loads((game / "_translation_meta" / "kirikiri_patch_diagnostics.json").read_text(encoding="utf-8"))
    assert diag["root_patch"] is True


def test_kirikiri_external_or_runtime_items_require_stream_bridge():
    base = TextItem(file="scenario.ks", original="こんにちは", translated="你好")
    runtime = TextItem(
        file="runtime.ks",
        original="こんにちは",
        translated="你好",
        meta={"runtime_capture": True},
    )
    external = TextItem(
        file="external.ks",
        original="こんにちは",
        translated="你好",
        meta={"from_external_tool": True},
    )

    assert changed_items_require_stream_bridge({"scenario.ks": [base]}) is False
    assert changed_items_require_stream_bridge({"runtime.ks": [runtime]}) is True
    assert changed_items_require_stream_bridge({"external.ks": [external]}) is True


def test_kirikiri_repack_ignores_unpatchable_runtime_capture_when_deploying_static_patch(tmp_path):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    original = ws / "original"
    original.mkdir(parents=True)
    script = original / "scenario.ks"
    script.write_text("こんにちは\n", encoding="utf-8")

    engine = KiriKiriEngine()
    engine._game_dir = game
    static_item = TextItem(
        file="scenario.ks",
        original="こんにちは",
        translated="你好",
        line=1,
        meta={"from_xp3": True, "encoding": "utf-8", "newline": "\n"},
    )
    stale_runtime_item = TextItem(
        file="__kirikiri_runtime_capture__.jsonl",
        original="これは以前の捕捉です",
        translated="这是之前捕获的文本",
        meta={"runtime_capture": True},
    )

    engine.repack([static_item, stale_runtime_item], ws)

    assert (game / "patch.xp3").exists()
    diag = json.loads((game / "_translation_meta" / "kirikiri_patch_diagnostics.json").read_text(encoding="utf-8"))
    assert diag["root_patch"] is True


def test_minimal_xp3_writer_emits_parseable_index(tmp_path):
    root = tmp_path / "root"
    (root / "scenario").mkdir(parents=True)
    file_path = root / "scenario" / "intro.ks"
    file_path.write_text("你好", encoding="utf-8")
    out = tmp_path / "patch.xp3"

    _write_xp3_patch(out, root, [file_path])

    assert out.read_bytes().startswith(XP3_SIGNATURE)
    assert _xp3_index_contains(out, "scenario/intro.ks")


def test_kirikiri_export_dump_targets_from_xp3_writes_only_script_names(tmp_path):
    game = _make_game(tmp_path)
    root = tmp_path / "patch_root"
    (root / "scenario").mkdir(parents=True)
    (root / "scenario" / "intro.txt.scn").write_bytes(_reference_magalumina_scn())
    (root / "startup.tjs").write_text('var boot = "skip";', encoding="utf-8")
    patch = tmp_path / "hint.xp3"
    _write_xp3_patch(patch, root, [root / "scenario" / "intro.txt.scn", root / "startup.tjs"])

    out = export_kirikiri_dump_targets_from_xp3(patch, game)

    assert out == game / "_translation_meta" / "kirikiri_dump_targets.txt"
    assert out.read_text(encoding="utf-8").splitlines() == ["scenario/intro.txt.scn"]


def test_kirikiri_export_dump_targets_includes_obfuscated_scn_entries(tmp_path):
    game = _make_game(tmp_path)
    root = tmp_path / "patch_root"
    root.mkdir()
    obfuscated = root / "倁"
    obfuscated.write_bytes(b"\x01\x02\x03\x04\xff\x00\x80\x81" * 8)
    warning_file = root / "倀"
    warning_file.write_text("Warning: Extracting this archive may infringe on author's rights.", encoding="cp932")
    patch = tmp_path / "scn.xp3"
    _write_xp3_patch(patch, root, [warning_file, obfuscated])

    out = export_kirikiri_dump_targets_from_xp3(patch, game)

    assert out.read_text(encoding="utf-8").splitlines() == ["倁"]


def test_kirikiri_storage_refs_expand_ks_scn_variants():
    refs = _expand_kirikiri_storage_refs(["scenario/next.ks", "system/menu.scn", "https://example.test/a.ks"])

    assert "scenario/next.ks" in refs
    assert "scenario/next.ks.scn" in refs
    assert "system/menu.scn" in refs
    assert "system/menu.ks.scn" in refs
    assert "https://example.test/a.ks" not in refs


def test_kirikiri_auto_dump_infers_next_targets_per_prefix_without_common_bias():
    inferred = _infer_kirikiri_sequential_targets(
        [
            "e05.ks.scn",
            "e06.ks.scn",
            "【共通】01.ks.scn",
            "【共通】02.ks.scn",
        ]
    )

    assert inferred[:2] == ["e07.ks.scn", "e08.ks.scn"]
    assert "【共通】03.ks.scn" in inferred


def test_kirikiri_auto_dump_keeps_storage_refs_before_sequence_guesses(tmp_path):
    dump_dir = tmp_path / "_translation_meta" / "kirikiri_dump"
    dump_dir.mkdir(parents=True)
    (dump_dir / "【共通】13.ks.scn").write_bytes(b"already dumped")
    (dump_dir / "e05.ks.scn").write_bytes(b"already dumped")
    targets = [
        "【風実花】00_分岐.ks",
        "【風実花】00_分岐.ks.scn",
        "e06_2.ks",
        "e06_2.ks.scn",
        "【共通】14.ks.scn",
        "e06.ks.scn",
    ]

    missing = _missing_kirikiri_active_dump_targets(tmp_path, targets)

    assert missing == [
        "【風実花】00_分岐.ks.scn",
        "e06_2.ks.scn",
        "【共通】14.ks.scn",
        "e06.ks.scn",
    ]


def test_kirikiri_auto_dump_uses_xp3_index_targets_as_first_run_seeds(tmp_path):
    meta = tmp_path / "_translation_meta"
    meta.mkdir()
    (meta / "kirikiri_dump_targets.txt").write_text("scenario/start.ks\n", encoding="utf-8")

    targets = _collect_kirikiri_dump_targets(tmp_path, include_inferred=False)

    assert targets == ["scenario/start.ks", "scenario/start.ks.scn"]


def test_kirikiri_auto_dump_identifies_extensionless_protected_targets():
    assert _kirikiri_is_extensionless_dump_target("A001")
    assert _kirikiri_is_extensionless_dump_target("scn/0001")
    assert _kirikiri_is_extensionless_dump_target("scn.xp3>A001")
    assert not _kirikiri_is_extensionless_dump_target("scenario/start.ks.scn")
    assert not _kirikiri_is_extensionless_dump_target("https://example.test/0001")


def test_kirikiri_auto_dump_accepts_archive_entry_targets():
    assert _kirikiri_is_dump_target("scn.xp3>A001")
    assert _expand_kirikiri_target_name("scn.xp3>scenario/start.ks") == [
        "scn.xp3>scenario/start.ks",
        "scn.xp3>scenario/start.ks.scn",
    ]


def test_kirikiri_pre_extract_skips_plain_xp3_seed_targets(tmp_path):
    game = _make_game(tmp_path)
    root = tmp_path / "root"
    (root / "scenario").mkdir(parents=True)
    script = root / "scenario" / "intro.ks"
    script.write_text("hello", encoding="utf-8")
    _write_xp3_patch(game / "scn.xp3", root, [script])

    result = prepare_kirikiri_dump_targets_from_game(game)

    assert result["target_count"] == 1
    assert result["protected_hint_count"] == 0
    assert result["pre_dump_recommended"] is False
    assert not (game / "_translation_meta" / "kirikiri_dump_targets.txt").exists()


def test_kirikiri_pre_extract_indexes_protected_seed_targets_without_dump_side_effects(tmp_path):
    game = _make_game(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    protected = root / "001"
    protected.write_bytes(b"\x01\x02\x03\x04\xff\x00\x80\x81" * 8)
    _write_xp3_patch(game / "scn.xp3", root, [protected])

    result = prepare_kirikiri_dump_targets_from_game(game)
    wrapped = _prepare_kirikiri_auto_dump_seed_targets(game)

    assert result["target_count"] == 1
    assert result["dump_target_variant_count"] == 2
    assert result["protected_hint_count"] == 1
    assert result["runtime_dump_targets_enabled"] is False
    assert wrapped and wrapped["pre_dump_recommended"] is True
    assert wrapped["runtime_dump_targets_enabled"] is False
    assert not (game / "_translation_meta" / "kirikiri_dump_targets.txt").exists()
    assert not (game / "_translation_meta" / "kirikiri_explicit_dump_targets.txt").exists()


def test_kirikiri_pre_extract_writes_protected_seed_targets_when_dump_opted_in(tmp_path, monkeypatch):
    game = _make_game(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    protected = root / "001"
    protected.write_bytes(b"\x01\x02\x03\x04\xff\x00\x80\x81" * 8)
    _write_xp3_patch(game / "scn.xp3", root, [protected])

    monkeypatch.setattr("engines.kirikiri.dump_targets._kirikiri_runtime_dump_targets_enabled", lambda: True)

    result = prepare_kirikiri_dump_targets_from_game(game)

    targets = (game / "_translation_meta" / "kirikiri_dump_targets.txt").read_text(encoding="utf-8").splitlines()
    explicit = (game / "_translation_meta" / "kirikiri_explicit_dump_targets.txt").read_text(encoding="utf-8").splitlines()
    assert result["target_count"] == 1
    assert result["dump_target_variant_count"] == 2
    assert result["protected_hint_count"] == 1
    assert result["runtime_dump_targets_enabled"] is True
    assert targets == ["001", "scn.xp3>001"]
    assert explicit == ["001", "scn.xp3>001"]


def test_kirikiri_pre_extract_writes_protected_named_scripts_when_dump_opted_in(tmp_path, monkeypatch):
    game = _make_game(tmp_path)
    root = tmp_path / "root"
    (root / "scenario").mkdir(parents=True)
    plain = root / "scenario" / "plain.ks"
    protected = root / "scenario" / "protected.ks"
    plain.write_text("hello", encoding="utf-8")
    protected.write_bytes(b"Q\x00QQ#QQQQ QcQQ-Q\x00Q<QQ<QQQQ.QQQ`QQQQQ$QQQQQ")
    _write_xp3_patch(game / "data.xp3", root, [plain, protected])

    monkeypatch.setattr("engines.kirikiri.dump_targets._kirikiri_runtime_dump_targets_enabled", lambda: True)

    result = prepare_kirikiri_dump_targets_from_game(game)

    targets = (game / "_translation_meta" / "kirikiri_dump_targets.txt").read_text(encoding="utf-8").splitlines()
    explicit = (game / "_translation_meta" / "kirikiri_explicit_dump_targets.txt").read_text(encoding="utf-8").splitlines()
    assert result["target_count"] == 2
    assert result["protected_hint_count"] == 1
    assert targets == ["data.xp3>scenario/protected.ks", "scenario/protected.ks"]
    assert explicit == ["data.xp3>scenario/protected.ks", "scenario/protected.ks"]


def test_kirikiri_internal_xp3_static_xor_filter_extracts_and_writes_plain_patch(tmp_path):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    root = tmp_path / "root"
    (root / "scenario").mkdir(parents=True)
    script = root / "scenario" / "intro.ks"
    plain = "\ufeff;comment\n\u3053\u3093\u306b\u3061\u306f\u3001\u4e16\u754c\u3002\n"
    key = 0x95
    script.write_bytes(bytes(byte ^ key for byte in plain.encode("utf-16")))
    _write_xp3_patch(game / "data.xp3", root, [script])
    _make_xp3_chained_shell(game / "data.xp3")
    (game / "data.xp3.sig").write_bytes(b"sig")
    original_data_xp3 = (game / "data.xp3").read_bytes()

    engine = KiriKiriEngine()
    items = engine.unpack(game, ws)

    item = next(i for i in items if i.original == "\u3053\u3093\u306b\u3061\u306f\u3001\u4e16\u754c\u3002")
    assert item.meta["encoding"] == f"kirikiri-xor-{key}"
    assert item.meta["xp3_filter"]["kind"] == "single_byte_xor"
    assert engine._static_external_decrypt_succeeded is True
    assert engine._protected_script_count == 0
    item.translated = "\u4f60\u597d\uff0c\u4e16\u754c\u3002"

    engine.repack(items, ws)

    patched = ws / "original" / "scenario" / "intro.ks"
    raw = patched.read_bytes()
    assert raw.startswith((b"\xff\xfe", b"\xfe\xff"))
    decoded = raw.decode("utf-16")
    assert "\u4f60\u597d\uff0c\u4e16\u754c\u3002" in decoded
    assert "GT" not in decoded
    assert not (game / "_translation_meta" / "kirikiri_placeholder_map.tsv").exists()

    root_patch = game / "patch.xp3"
    disabled_root_patch = game / "patch.xp3.disabled_static_rewrite"
    assert not root_patch.exists()
    assert disabled_root_patch.exists()
    assert (game / "data.xp3").read_bytes() != original_data_xp3
    assert not (game / "data.xp3.sig").exists()
    assert (game / "_translation_meta" / "original_signatures" / "data.xp3.sig.disabled").read_bytes() == b"sig"
    static_diag = json.loads((game / "_translation_meta" / "kirikiri_static_xp3_rebuild.json").read_text(encoding="utf-8"))
    assert static_diag["rebuilt_archives"] == ["data.xp3"]
    assert static_diag["modes"] == {"data.xp3": "in_place_original_segments_preserve_index"}
    assert _read_xp3_index(game / "data.xp3").chained is True
    assert "patch.xp3.disabled_static_rewrite" in static_diag["disabled_shadow_patches"]
    assert not (game / "_translation_meta" / "kirikiri_patch.xp3").exists()
    assert not (game / "_translation_meta" / "kirikiri_patch").exists()
    assert not (game / "_translation_meta" / "kirikiri_patch_manifest.txt").exists()
    assert (game / "_translation_meta" / "kirikiri_patch.disabled_static_rewrite").exists()
    index = _read_xp3_index(game / "data.xp3")
    entry = next(entry for entry in index.entries if entry.name == "scenario/intro.ks")
    patched_payload = _read_xp3_entry_bytes(game / "data.xp3", entry)
    decoded_payload = bytes(byte ^ key for byte in patched_payload)
    assert decoded_payload.startswith((b"\xff\xfe", b"\xfe\xff"))
    assert "\u4f60\u597d\uff0c\u4e16\u754c\u3002" in decoded_payload.decode("utf-16")


def test_kirikiri_internal_xp3_koihazi_filter_extracts_and_deploys_native_root_patch(tmp_path):
    game = _make_game(tmp_path)
    plugin_dir = game / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "koihazi.tpm").write_bytes(
        b"\\xp3dec\\Release\\xp3dec.pdb\x00"
        b"TVPSetXP3ArchiveExtractionFilter\x00"
        b"Incompatible tTVPXP3ExtractionFilterInfo size\x00"
        b"V2Link\x00"
    )
    ws = tmp_path / "ws"
    root = tmp_path / "root"
    (root / "scenario").mkdir(parents=True)
    script = root / "scenario" / "intro.ks"
    plain = "@start\r\n[name text=\"\u592a\u90ce\"]\r\n\u3053\u3093\u306b\u3061\u306f\u3002\r\n"
    script.write_text(plain, encoding="utf-8")
    xp3 = game / "data.xp3"
    _write_xp3_patch(xp3, root, [script])
    entry = _read_xp3_index(xp3).entries[0]
    assert entry.adler is not None
    _write_xp3_patch(
        xp3,
        root,
        [script],
        filters_by_rel={"scenario/intro.ks": {"kind": "koihazi_xp3dec", "file_hash": entry.adler}},
    )
    _make_xp3_chained_shell(xp3)
    before_xp3 = xp3.read_bytes()
    original_payload = _read_xp3_entry_bytes(xp3, _read_xp3_index(xp3).entries[0])
    assert "\u3053\u3093".encode("utf-8") not in original_payload

    engine = KiriKiriEngine()
    items = engine.unpack(game, ws)

    item = next(i for i in items if i.original == "\u3053\u3093\u306b\u3061\u306f\u3002")
    assert item.meta["xp3_filter"]["kind"] == "koihazi_xp3dec"
    assert item.meta["encoding"] == "utf-8-sig" or item.meta["encoding"] == "utf-8"
    item.translated = "\u4f60\u597d\u3002"

    engine.repack(items, ws)

    assert xp3.read_bytes() == before_xp3
    patch_index = _read_xp3_index(game / "patch.xp3")
    names = {entry.name for entry in patch_index.entries}
    assert "scenario/intro.ks" in names
    assert "intro.ks" in names
    alias_entry = next(entry for entry in patch_index.entries if entry.name == "intro.ks")
    assert alias_entry.adler == 0
    alias_payload = _read_xp3_entry_bytes(game / "patch.xp3", alias_entry)
    assert "\u4f60\u597d\u3002" in alias_payload.decode("utf-8")
    static_diag = json.loads((game / "_translation_meta" / "kirikiri_static_xp3_rebuild.json").read_text(encoding="utf-8"))
    assert static_diag["reason"] == "native_root_patch_preferred"
    patch_diag = json.loads((game / "_translation_meta" / "kirikiri_patch_diagnostics.json").read_text(encoding="utf-8"))
    assert patch_diag["root_patch"] is True


def test_kirikiri_koihazi_native_root_patch_neutralizes_filtered_patch_entries(tmp_path):
    game = _make_game(tmp_path)
    plugin_dir = game / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "koihazi.tpm").write_bytes(
        b"\\xp3dec\\Release\\xp3dec.pdb\x00"
        b"TVPSetXP3ArchiveExtractionFilter\x00"
        b"Incompatible tTVPXP3ExtractionFilterInfo size\x00"
        b"V2Link\x00"
    )
    ws = tmp_path / "ws"
    root = tmp_path / "root"
    (root / "scenario").mkdir(parents=True)
    slot_script = root / "scenario" / "slot.ks"
    bridge_script = root / "scenario" / "bridge.ks"
    slot_script.write_text("@start\r\n\u3053\u3093\u306b\u3061\u306f\u3002\r\n", encoding="utf-8")
    bridge_script.write_text("@start\r\n\u304a\u306f\u3088\u3046\u3002\r\n", encoding="utf-8")
    xp3 = game / "data.xp3"
    _write_xp3_patch(xp3, root, [slot_script, bridge_script])
    filters_by_rel = {}
    for entry in _read_xp3_index(xp3).entries:
        filters_by_rel[entry.name] = {"kind": "koihazi_xp3dec", "file_hash": entry.adler}
    _write_xp3_patch(xp3, root, [slot_script, bridge_script], filters_by_rel=filters_by_rel)
    _make_xp3_chained_shell(xp3)

    engine = KiriKiriEngine()
    items = engine.unpack(game, ws)
    for item in items:
        if item.file == "scenario/slot.ks":
            item.translated = "\u4f60\u597d\u3002"
        elif item.file == "scenario/bridge.ks":
            item.translated = "\u8fd9\u662f\u4e00\u6bb5\u8d85\u8fc7\u539f\u59cb\u69fd\u4f4d\u5f88\u591a\u7684\u4e2d\u6587\u8bd1\u6587\uff0c\u7528\u6765\u786e\u4fdd\u5269\u4f59\u811a\u672c\u8fdb\u5165\u8865\u4e01\u5305\u800c\u4e0d\u662f\u539f\u5305\u539f\u69fd\u4f4d\u3002"

    engine.repack(items, ws)

    static_diag = json.loads((game / "_translation_meta" / "kirikiri_static_xp3_rebuild.json").read_text(encoding="utf-8"))
    assert static_diag["reason"] == "native_root_patch_preferred"
    patch_index = _read_xp3_index(game / "patch.xp3")
    bridge_entry = next(entry for entry in patch_index.entries if entry.name == "scenario/bridge.ks")
    bridge_payload = _read_xp3_entry_bytes(game / "patch.xp3", bridge_entry)
    assert bridge_entry.adler == 0
    assert "\u8fd9\u662f\u4e00\u6bb5" in bridge_payload.decode("utf-8")
    bridge_alias = next(entry for entry in patch_index.entries if entry.name == "bridge.ks")
    assert bridge_alias.adler == 0


def test_kirikiri_static_rebuild_prunes_only_rebuilt_files_from_bridge_patch(tmp_path):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    root = tmp_path / "root"
    root.mkdir()
    key = 0x31
    slot_script = root / "slot.ks"
    bridge_script = root / "bridge.ks"
    slot_text = "\ufeff" + "こんにちは世界。\n" * 80
    bridge_text = "\ufeffこんにちは。\n"
    slot_script.write_bytes(bytes(byte ^ key for byte in slot_text.encode("utf-16")))
    bridge_script.write_bytes(bytes(byte ^ key for byte in bridge_text.encode("utf-16")))
    _write_xp3_patch(game / "data.xp3", root, [slot_script, bridge_script])
    _make_xp3_chained_shell(game / "data.xp3")

    engine = KiriKiriEngine()
    items = engine.unpack(game, ws)
    for item in items:
        if item.file == "slot.ks":
            item.translated = "你好。"
        elif item.file == "bridge.ks":
            item.translated = "这是一段明显超过原始槽位长度的中文译文。"

    engine.repack(items, ws)

    static_diag = json.loads((game / "_translation_meta" / "kirikiri_static_xp3_rebuild.json").read_text(encoding="utf-8"))
    patch_diag = json.loads((game / "_translation_meta" / "kirikiri_patch_diagnostics.json").read_text(encoding="utf-8"))
    assert static_diag["rebuilt_files"] == ["slot.ks"]
    assert static_diag["shadow_patch_mode"] == "pruned_static_rebuilt_files"
    assert static_diag["shadow_patch_remaining_files"] == ["bridge.ks"]
    assert patch_diag["script_files"] == ["bridge.ks"]
    assert (game / "_translation_meta" / "kirikiri_patch.xp3").exists()
    bridge_patch = game / "_translation_meta" / "kirikiri_patch" / "bridge.ks"
    assert bridge_patch.exists()
    assert "这是一段明显超过原始槽位长度的中文译文。" in bridge_patch.read_text(encoding="utf-16")
    assert not (game / "_translation_meta" / "kirikiri_patch" / "slot.ks").exists()

    entry = next(entry for entry in _read_xp3_index(game / "data.xp3").entries if entry.name == "slot.ks")
    payload = _read_xp3_entry_bytes(game / "data.xp3", entry)
    decoded = bytes(byte ^ key for byte in payload).decode("utf-16")
    assert "你好。" in decoded


def test_kirikiri_runtime_resource_overlay_does_not_rewrite_source_xp3(tmp_path):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    root = tmp_path / "root"
    (root / "scenario").mkdir(parents=True)
    script = root / "scenario" / "intro.ks"
    script.write_text("@start\r\nこんにちは。\r\n", encoding="utf-8")
    xp3 = game / "data.xp3"
    _write_xp3_patch(xp3, root, [script])
    before = xp3.read_bytes()

    engine = KiriKiriEngine()
    items = engine.unpack(game, ws)
    for item in items:
        if item.original == "こんにちは。":
            item.translated = "你好。"
            item.meta["xp3_filter"] = {"archive": "data.xp3", "kind": "test_filter"}
    engine._runtime_resource_overlay = True

    engine.repack(items, ws)

    assert xp3.read_bytes() == before
    assert not (game / "patch.xp3").exists()
    assert (game / "_translation_meta" / "kirikiri_patch.xp3").exists()
    patch_file = game / "_translation_meta" / "kirikiri_patch" / "scenario" / "intro.ks"
    assert patch_file.exists()
    assert "你好。" in patch_file.read_text(encoding="utf-8")
    diag = json.loads((game / "_translation_meta" / "kirikiri_static_xp3_rebuild.json").read_text(encoding="utf-8"))
    assert diag["reason"] == "runtime_resource_overlay"
    assert diag["rebuilt_archives"] == []


def test_kirikiri_repack_preserves_utf8_without_bom(tmp_path):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    original = ws / "original"
    script = original / "scenario" / "main" / "0_1.ks"
    script.parent.mkdir(parents=True)
    script.write_bytes("@SC_StartProcess bg=bg10_0\r\nこんにちは。\r\n".encode("utf-8"))

    engine = KiriKiriEngine()
    engine._game_dir = game
    engine._script_meta = {}
    engine._meta_lock = threading.Lock()
    items = engine._extract_script_file(script, original, from_xp3=True)
    assert items
    assert items[0].meta["encoding"] == "utf-8"
    items[0].translated = "你好。"

    engine.repack(items, ws)

    patch_file = game / "_translation_meta" / "kirikiri_patch" / "scenario" / "main" / "0_1.ks"
    assert patch_file.exists()
    assert not patch_file.read_bytes().startswith(b"\xef\xbb\xbf")
    assert patch_file.read_text(encoding="utf-8").startswith("@SC_StartProcess")


def test_kirikiri_runtime_resource_overlay_prunes_system_scripts(tmp_path):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    original = ws / "original"
    story = original / "scenario" / "main" / "0_1.ks"
    system = original / "scenario" / "CommonClass" / "yesnodialoglayer.ks"
    story.parent.mkdir(parents=True)
    system.parent.mkdir(parents=True)
    story.write_text("@start\nこんにちは\n", encoding="utf-8")
    system.write_text("@start\nはい\n", encoding="utf-8")

    engine = KiriKiriEngine()
    engine._game_dir = game
    engine._runtime_resource_overlay = True
    items = [
        TextItem(
            file="scenario/main/0_1.ks",
            original="こんにちは",
            translated="你好",
            line=1,
            meta={"from_xp3": True, "encoding": "utf-8", "newline": "\n"},
        ),
        TextItem(
            file="scenario/CommonClass/yesnodialoglayer.ks",
            original="はい",
            translated="是",
            line=1,
            meta={"from_xp3": True, "encoding": "utf-8", "newline": "\n"},
        ),
    ]

    engine.repack(items, ws)

    assert _is_kirikiri_runtime_overlay_patch_file("scenario/main/0_1.ks") is True
    assert _is_kirikiri_runtime_overlay_patch_file("scenario/CommonClass/yesnodialoglayer.ks") is False
    assert (game / "_translation_meta" / "kirikiri_patch" / "scenario" / "main" / "0_1.ks").exists()
    assert not (game / "_translation_meta" / "kirikiri_patch" / "scenario" / "CommonClass" / "yesnodialoglayer.ks").exists()
    assert not (game / "_translation_meta" / "kirikiri_patch" / "0_1.ks").exists()
    assert (game / "_translation_meta" / "kirikiri_patch_manifest.txt").read_text(encoding="utf-8") == "scenario/main/0_1.ks\n"


def test_kirikiri_loads_runtime_capture_when_static_extract_finds_nothing(tmp_path):
    game = _make_game(tmp_path)
    (game / "data.xp3").write_bytes(XP3_SIGNATURE + b"\0" * 128)
    meta = game / "_translation_meta"
    meta.mkdir()
    (meta / "kirikiri_runtime_capture.jsonl").write_text(
        json.dumps({"text": "はじめから", "source": "gdi32!TextOutA"}, ensure_ascii=False) + "\n"
        + json.dumps({"text": "はじめから", "source": "duplicate"}, ensure_ascii=False) + "\n"
        + json.dumps({"text": "12345", "source": "noise"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    items = KiriKiriEngine().unpack(game, tmp_path / "ws")

    assert [item.original for item in items] == ["はじめから"]
    assert items[0].file == "__kirikiri_runtime_capture__.jsonl"
    assert items[0].meta["runtime_capture"] is True


def test_kirikiri_runtime_capture_schema_skips_speaker_records(tmp_path):
    game = _make_game(tmp_path)
    (game / "data.xp3").write_bytes(XP3_SIGNATURE + b"\0" * 128)
    meta = game / "_translation_meta"
    meta.mkdir()
    visible = "\u4eca\u65e5\u306f\u3044\u3044\u5929\u6c17\u3060"
    raw = "@voice name='\u7ffc\uff08\u5e7c\u5c11\u671f\uff09' word='\u4eca\u65e5\u306f\u3044\u3044\u5929\u6c17\u3060'"
    (meta / "kirikiri_runtime_capture.jsonl").write_text(
        json.dumps({
            "source": "kag_speaker",
            "text": "\u7ffc",
            "role": "speaker",
            "hook_name": "kag_speaker",
            "visible_text": "\u7ffc",
            "raw_text": "[name name=\"\u7ffc\"]",
        }, ensure_ascii=False) + "\n"
        + json.dumps({
            "source": "embed_krkrz_utf8",
            "text": visible,
            "role": "text",
            "hook_name": "embed_krkrz_utf8",
            "visible_text": visible,
            "raw_text": raw,
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    items = KiriKiriEngine().unpack(game, tmp_path / "ws")

    assert [item.original for item in items] == [visible]
    assert items[0].meta["source"] == "embed_krkrz_utf8"
    assert items[0].meta["runtime_capture_role"] == "text"
    assert items[0].meta["runtime_capture_raw_text"] == raw


def test_kirikiri_runtime_capture_filter_fixture(tmp_path):
    game = _make_game(tmp_path)
    meta = game / "_translation_meta"
    meta.mkdir()
    records = [
        {
            "role": "speaker",
            "hook_name": "kagparser_speaker",
            "visible_text": "翼",
            "raw_text": "@voice name='翼' word='今日はいい天気だ'",
        },
        {
            "role": "text",
            "hook_name": "kagparser_command",
            "visible_text": "今日はいい天気だ",
            "raw_text": "@voice name='翼' word='今日はいい天気だ'",
        },
        {
            "role": "text",
            "hook_name": "embed_krkrz_utf8",
            "visible_text": "今日はいい天気だ",
            "raw_text": "今日はいい天気だ",
        },
        {
            "role": "text",
            "hook_name": "kagparser",
            "visible_text": "また明日、ここで会おう。",
            "raw_text": "[text]また明日、ここで会おう。[r]",
        },
        {
            "role": "text",
            "hook_name": "gdi_fallback",
            "visible_text": r"scenario\common.ks",
            "raw_text": r"scenario\common.ks",
        },
        {
            "role": "text",
            "hook_name": "kagparser",
            "visible_text": "[jump storage='scene.ks']",
            "raw_text": "[jump storage='scene.ks']",
        },
        {
            "role": "text",
            "hook_name": "gdi_fallback",
            "visible_text": "!!!",
            "raw_text": "!!!",
        },
    ]
    (meta / "kirikiri_runtime_capture.jsonl").write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )

    items = KiriKiriEngine()._load_runtime_capture_items(game)

    assert [item.original for item in items] == ["今日はいい天気だ", "また明日、ここで会おう。"]
    assert items[0].meta["source"] == "kagparser_command"
    assert all(item.meta["runtime_capture_role"] == "text" for item in items)


def test_kirikiri_merges_runtime_capture_as_missing_static_coverage(tmp_path):
    game = _make_game(tmp_path)
    (game / "scenario.ks").write_text("既存テキスト\n", encoding="utf-16")
    meta = game / "_translation_meta"
    meta.mkdir()
    (meta / "kirikiri_runtime_capture.jsonl").write_text(
        json.dumps({"text": "「なんだよ、またお前と同じクラスか」", "source": "kagparser"}, ensure_ascii=False) + "\n"
        + json.dumps({"text": "既存テキスト", "source": "duplicate_static"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    items = KiriKiriEngine().unpack(game, tmp_path / "ws")

    originals = [item.original for item in items]
    assert "既存テキスト" in originals
    assert "「なんだよ、またお前と同じクラスか」" in originals
    captured = [item for item in items if item.original == "「なんだよ、またお前と同じクラスか」"][0]
    assert captured.file == "__kirikiri_runtime_capture__.jsonl"
    assert captured.meta["runtime_capture"] is True
    assert originals.count("既存テキスト") == 1


def test_kirikiri_loads_runtime_dump_before_runtime_capture(tmp_path):
    game = _make_game(tmp_path)
    (game / "data.xp3").write_bytes(XP3_SIGNATURE + b"\0" * 128)
    meta = game / "_translation_meta"
    dump = meta / "kirikiri_dump"
    dump.mkdir(parents=True)
    (dump / "scenario" / "intro.txt.scn").parent.mkdir(parents=True)
    (dump / "scenario" / "intro.txt.scn").write_bytes(_reference_magalumina_scn())
    (meta / "kirikiri_runtime_capture.jsonl").write_text(
        json.dumps({"text": "12345", "source": "gdi32!TextOutA"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    engine = KiriKiriEngine()
    items = engine.unpack(game, tmp_path / "ws")

    assert any(item.file == "scenario/intro.txt.scn" for item in items)
    assert all(item.file != "__kirikiri_runtime_capture__.jsonl" for item in items)
    assert any(item.meta.get("runtime_dump") is True for item in items)
    assert engine._runtime_dump_count == 1


def test_kirikiri_loads_runtime_dump_archive(tmp_path):
    game = _make_game(tmp_path)
    (game / "data.xp3").write_bytes(XP3_SIGNATURE + b"\0" * 128)
    meta = game / "_translation_meta"
    meta.mkdir()
    with zipfile.ZipFile(meta / "kirikiri_dump.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("scenario/intro.txt.scn", _reference_magalumina_scn())
        zf.writestr("../unsafe.txt.scn", _reference_magalumina_scn())
        zf.writestr("notes/readme.txt", "not a script")
    (meta / "kirikiri_runtime_capture.jsonl").write_text(
        json.dumps({"text": "12345", "source": "gdi32!TextOutA"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    engine = KiriKiriEngine()
    items = engine.unpack(game, tmp_path / "ws")

    assert any(item.file == "scenario/intro.txt.scn" for item in items)
    assert all(item.file != "__kirikiri_runtime_capture__.jsonl" for item in items)
    assert any(item.meta.get("runtime_dump") is True for item in items)
    assert engine._runtime_dump_count == 1
    assert not (tmp_path / "ws" / "unsafe.txt.scn").exists()
    diag = json.loads((game / "_translation_meta" / "extract_diagnostics.json").read_text(encoding="utf-8"))
    assert diag["needs_patch_bridge"] is True


def test_kirikiri_imports_external_dump_scripts(tmp_path):
    game = _make_game(tmp_path)
    (game / "scn.xp3").write_bytes(XP3_SIGNATURE + b"\0" * 128)
    external = tmp_path / "external"
    (external / "scn" / "scenario").mkdir(parents=True)
    (external / "scn" / "scenario" / "intro.txt.scn").write_bytes(_reference_magalumina_scn())
    (external / "image").mkdir()
    (external / "image" / "cg.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    result = import_kirikiri_external_dump(game, external)

    assert result["imported_files"] == 1
    dump = game / "_translation_meta" / "kirikiri_dump"
    assert (dump / "scenario" / "intro.txt.scn").exists()
    assert not (dump / "scn" / "scenario" / "intro.txt.scn").exists()
    assert not (dump / "image" / "cg.png").exists()


def test_krkrdump_config_keeps_extensionless_resources(tmp_path):
    out_dir = tmp_path / "dump"
    cfg = tmp_path / "KrkrDump.json"

    _write_krkrdump_config(cfg, out_dir)

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["enableExtract"] is True
    assert data["includeExtensions"] == []
    assert ".ogg" in data["excludeExtensions"]
    assert data["decryptSimpleCrypt"] is True
    assert any("xp3>" in rule for rule in data["rules"])


def test_kirikiri_dump_count_does_not_hide_missing_explicit_targets(tmp_path):
    game = _make_game(tmp_path)
    meta = game / "_translation_meta"
    dump = meta / "kirikiri_dump"
    dump.mkdir(parents=True)
    (meta / "kirikiri_explicit_dump_targets.txt").write_text(
        "scenario/needed.ks.scn\n",
        encoding="utf-8",
    )
    for idx in range(5):
        (dump / f"system{idx}.tjs").write_text('var title = "設定";', encoding="utf-8")

    assert _kirikiri_count_threshold_sufficient(game) is False

    (dump / "scenario").mkdir()
    (dump / "scenario" / "needed.ks.scn").write_bytes(_reference_magalumina_scn())

    assert _kirikiri_count_threshold_sufficient(game) is True


def test_kirikiri_runtime_capture_skips_control_code_noise(tmp_path):
    game = _make_game(tmp_path)
    meta = game / "_translation_meta"
    meta.mkdir()
    (meta / "kirikiri_runtime_capture.jsonl").write_text(
        json.dumps({"text": "鐏€虹€界倓\u000e,\u000f", "source": "gdi"}, ensure_ascii=False) + "\n"
        + json.dumps({"text": "事情はメルヴィから聞いた箱のことね", "source": "gdi"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    items = KiriKiriEngine().unpack(game, tmp_path / "ws")

    assert [item.original for item in items] == ["事情はメルヴィから聞いた箱のことね"]


def test_kirikiri_runtime_capture_cleans_measure_repeat_noise(tmp_path):
    assert _clean_runtime_capture_text("今、もう、夜夜夜夜の１１１１時時時時よ？") == "今、もう、夜の１１時よ？"
    assert _clean_runtime_capture_text("大和和「え？") == "大和「え？"
    assert _clean_runtime_capture_text("は驚き、時計を見る。") == "は驚き、時計を見る。"


def test_kirikiri_alignment_matrix_documents_all_backend_branches():
    doc = (Path(__file__).parent.parent / "docs" / "kirikiri_runtime_alignment.md").read_text(encoding="utf-8")

    assert "Engine layout reference: KRKRZ" in doc
    assert "KRKRZ `fd5c4baa6a2ef5978db1bd043634351f48667daf`" in doc
    assert "the game binary is the only oracle" in doc
    for branch in [
        "KAGParser",
        "KAGParserEx",
        "ExtKAGParser",
        "TextRender",
        "KiriKiriZ1/Z2",
        "KiriKiriZX",
        "EmbedKrkrZ",
        "EmbedKrkr2",
        "Krkr2wcs",
        "GDI caller fallback",
    ]:
        assert branch in doc
    assert "role=speaker" in doc
    assert "role=speaker` records are diagnostic only" in doc
    assert "If an external hook captures a KRKR text but our runtime does not" in doc
    assert "implemented embed beta" in doc
    assert "independent implementation" in doc


def test_kirikiri_hook_merges_single_glyph_measure_fragments():
    hook = _hook_source()

    assert "shouldAppendMeasureFragment" in hook
    assert "buf.text = current + text" in hook
    assert "Some KiriKiriZ/textrender paths measure one glyph at a time." in hook
    assert "isRedundantGdiMeasureSource" in hook
    assert "cleanMeasureRunText" in hook
    assert "isMeasureNumericFragment" in hook


def test_kirikiri_native_hook_contains_protected_stream_dump_contract():
    source = (Path(__file__).parent.parent / "native" / "kirikiri_runtime" / "kirikiri_native_hook.cpp").read_text(encoding="utf-8")

    assert "TryHookDumpStream" in source
    assert "DumpRead" in source
    assert "DumpSeek" in source
    assert "DumpFlushThread" in source
    assert "kirikiri_dump" in source
    assert "StreamSeek_t" in source
    assert "StreamRead_t" in source
    assert "patch hit" in source
    assert "dumped script stream" in source
    assert "kirikiri_dump_targets.txt" in source
    assert "DumpTargetListContains" in source
    assert "DumpTargetListContains(inner)" in source
    assert "ShouldAttemptPatch" in source
    assert "Extensionless protected resources are dumped when the game opens" in source
    assert "archiveSep = name.find(L'>')" in source
    assert "gPassiveDumpOnly" in source
    assert "DumpTargetsContainOnlyExtensionlessEntries" in source
    assert "target dump finished" in source
    assert "gPatchBasenameEntries" in source
    assert "PatchBasenameKey" in source
    assert "ResolvePatchEntryCandidates" in source
    assert "Ambiguous bare script names are intentionally not remapped." in source
    assert "kirikiri_active_extensionless_dump.txt" in source
    assert "gActiveExtensionlessDump" in source
    assert "InstallCreateStreamHookFromExports" in source
    assert "TVPCreateStream export not found" in source
    assert "TVPCreateStream hook installed: export" in source


def test_kirikiri_native_hook_defaults_to_mtool_style_safe_profile():
    source = (Path(__file__).parent.parent / "native" / "kirikiri_runtime" / "kirikiri_native_hook.cpp").read_text(encoding="utf-8")
    embed_source = (Path(__file__).parent.parent / "native" / "kirikiri_runtime" / "kirikiri_embed_runtime.cpp").read_text(encoding="utf-8")
    trace_source = (Path(__file__).parent.parent / "native" / "kirikiri_runtime" / "kirikiri_embed_trace.cpp").read_text(encoding="utf-8")

    assert "2026-07-24-krkr-wide-slot-v29" in source
    assert "engine behavior reference: KiriKiri engine32" in source
    assert "engine layout reference=KRKRZ fd5c4ba tjs2/tjsVariantString.h" in source
    assert 'std::wstring(value, n) : L"mtool"' in source
    assert 'profile == L"display"' in source
    assert "gInternalTextHooks" in source
    assert "internal_text_hooks=1" in source
    assert "KiriKiriResolveInternalText" in source
    assert "kagDisplayReplacement" in source
    assert "hitCounter == &gInternalKagHits" in source
    assert "KiriKiriResolveKagParserArgument" in source
    assert "KiriKiriCaptureInternalTjsString" in source
    assert 'CaptureHookSelected(captureMode, L"psb")' in source
    assert 'CaptureHookSelected(captureMode, L"textrender")' in source
    assert 'CaptureHookSelected(captureMode, L"kag")' in source
    assert "BuildStackTjsStringCaptureStub" in source
    assert "CaptureRuntimeSpeakerCommand" in source
    assert "IsSpeakerOnlyKagCommand" in source
    assert "KagCommandVerb" in source
    assert "IsSpeakerKagCommandVerb" in source
    assert 'verb == L"voice"' in source
    assert 'verb == L"macro"' not in source
    assert "InstallEmbedKrkr2WideHook" in source
    assert "BuildEmbedKrkr2WideSlotStub" in source
    assert "KiriKiriResolveEmbedKrkr2Text" in source
    assert "TryWriteEmbedKrkr2TextSlot" in source
    assert "PassesEmbedKrkr2WideFilter" in embed_source
    assert 'PublishRuntimeOverlayText(src, L"embed_krkr2_wide", visible)' in source
    assert "kirikiri_embed::ResolveWide(text, src, visible)" in source
    assert "ApplyWideInPlace" not in source
    assert "EmbedKrkr2 wide slot applied:" in source
    assert "EmbedKrkr2 wide slot write failed" in source
    assert "trimmed[0] == L'['" in source
    assert "if (sourceIsCommand)" in source
    assert "visible = KagParserVisibleText(sourceText);" in source
    assert "if (!IsInternalCandidateText(visible)) return;" in source
    assert "embed_wide_replaced=" in source
    assert "embed_wide_missed=" in source
    assert "embed_wide_capacity_miss=" not in source
    assert "embed_wide_slot_applied=" in source
    assert "embed_wide_slot_write_fail=" in source
    assert "PassesEmbedKrkrZFilter" in embed_source
    assert "ContainsEmbedKrkrZChatFlag" in source
    assert 'u8"「"' in embed_source
    assert 'u8"。"' in embed_source
    assert "if (!kirikiri_embed::PassesEmbedKrkrZFilter(raw)) return text;" in source
    assert "if (!ContainsEmbedKrkrZChatFlag(wide)) return text;" in source
    assert "TryReadWideCString" in source
    assert "TryReadNarrowCString" in source
    assert "LooksLikeRuntimeMojibake" in source
    assert "BuildKagParserStub" in source
    assert "BuildKagParserEntryStub" in source
    assert "mov [esp+44],eax" not in source
    assert "tTJSString arg2" in source
    assert "kag_entry_hits=" in source
    assert "kag replace:" in source
    assert "kag display:" in source
    assert "std::wstring displayOut = *translated;" in source
    assert "WriteToOverlay(visible, displayOut);" in source
    assert "InitOverlaySharedMemory" in source
    assert "WriteToOverlay" in source
    assert "overlay shared memory ready" in source
    assert 'line += "\\",\\"role\\":\\"";' in source
    assert 'line += "\\",\\"hook_name\\":\\"";' in source
    assert 'line += "\\",\\"visible_text\\":\\"";' in source
    assert 'line += "\\",\\"raw_text\\":\\"";' in source
    assert "gRuntimeTextCaptureCount" in source
    assert "gRuntimeSpeakerCaptureCount" in source
    assert "gRuntimeCaptureDuplicateSkips" in source
    assert "gOverlayWriteCount" in source
    assert 'std::wstring dedupeKey = roleValue + L"\\x1f" + visibleText;' in source
    assert "text_captures=" in source
    assert "speaker_captures=" in source
    assert "capture_duplicates=" in source
    assert "overlay_writes=" in source
    assert 'AppendRuntimeCapture(speaker.empty() ? L"<narration>" : speaker, L"kag_speaker", L"speaker", src)' in source
    assert 'L"kagparser_speaker"' in source
    assert "if (!cmdWord.empty()) return cmdWord;" in source
    assert 'cmdName + L"\\u300c" + cmdWord' not in source
    assert "BuildKiriKiriZ3Stub" in source
    assert "KAGParser text buffer" in source
    assert "KiriKiriZ3 text pointer" in source
    assert "capture candidate matrix begin" in source
    assert 'ReportKagParserModuleCandidates(L"KAGParser.dll", false)' in source
    assert 'ReportKagParserModuleCandidates(L"ExtKAGParser.dll", true)' in source
    assert "tTJSString arg2" in source
    assert "text buffer" in source
    assert "EmbedKrkrZ" in source
    assert "Krkr2wcs" in source
    assert "KiriKiri4" in source
    assert "KIRIKIRI_EXPERIMENTAL_TEXT_HOOKS" in source
    assert "KIRIKIRI_CAPTURE_HOOKS" in source
    assert "capture-only hooks enabled" in source
    assert "capture-only hooks disabled by KIRIKIRI_CAPTURE_HOOKS" in source
    assert "CaptureHookSelected" in source
    assert 'mode == L"all"' in source
    assert "InstallEmbedKrkrZUtf8Hooks" in source
    assert "InstallKiriKiriZXInternalHook" in source
    assert "InstallKiriKiriZ2IndirectHook" in source
    assert 'CaptureHookSelected(captureMode, L"z2") ||' in source
    assert 'CaptureHookSelected(captureMode, L"kr2")) {' in source
    assert "KIRIKIRI_ENABLE_VM_TEXT_REPLACE" in source
    assert "vm_text_replace=0" in source
    assert "KIRIKIRI_ENABLE_EMBED_TEXT_REPLACE" in source
    assert "embed_text_replace=0" in source
    assert "kirikiri_embed::ResolveUtf8(text, raw, WideToUtf8(visible))" in source
    assert "if (!visibleOverride.empty())" in source
    assert "translated = FindTranslationVariant(visible);" in source
    assert "mov [esp+24],eax" in source
    assert "pushRegisterOpcode != 0x51" in source
    assert "embed_replaced=" in source
    assert "KiriKiriEmbedAfterNewUtf8Text" in source
    assert "KiriKiriConvertEmbeddedUtf8" in source
    assert "BuildUtf8ConverterEntryStub" in source
    assert "EmbedKrkrZ UTF-8 converter entry" in source
    assert "KiriKiriReplaceEmbeddedUtf8Cursor" in source
    assert "BuildUtf8CursorEntryStub" in source
    assert "EmbedKrkrZ UTF-8 cursor entry" in source
    assert "gEmbedKrkrZCursorTarget" in source
    assert "BYTE *entry = p - 3" in source
    assert "embed_cursor_replaced=" in source
    assert "GuessEmbedKrkrZReturnAddressOffset" in source
    assert "returnAddressStackOffset" in source
    assert "gEmbedKrkrZConverterTarget" in source
    assert "embed_converter_bypass=" in source
    assert "EMBED_AFTER_NEW consumed target=" in source
    assert "kirikiri_embed_trace::ShouldLog" in source
    assert "embed caller trace source_hash=" in trace_source
    assert "caller_rva=" in trace_source
    assert "parent_caller_rva=" in trace_source
    assert "grandparent_caller_rva=" in trace_source
    assert "destination_mode=" in trace_source
    assert "destination_address=" in trace_source
    assert "embed_after_new=" in source
    assert "BuildStackWideEmbedAfterNewStub" in source
    assert "BuildEaxWideEmbedAfterNewStub" in source
    assert "InstallTextRenderInternalHook" in source
    assert "FindTextRenderGetStringEaxCandidate" in source
    assert "FindTextRenderFallbackCandidate" in source
    assert "KiriKiriResolveTextRenderText" in source
    assert "NormalizeTextRenderVisible" in source
    assert "TextRender EAX GetString" in source
    assert "TextRender UTF-16 arg1" in source
    assert "textrender_hits=" in source
    assert "InstallPsbPostConversionHook" in source
    assert "KiriKiriPatchPsbWideResult" in source
    assert "PSB UTF-8 to UTF-16 post conversion" in source
    assert "psb_post_readonly=1" in source
    assert "psb post conversion readonly=1" in source
    assert "source_replaced=" not in source
    assert "wmemcpy(destination" not in source
    assert "const_cast<char *>(source)" not in source
    psb_source = (Path(__file__).parent.parent / "native" / "kirikiri_runtime" / "kirikiri_psb_runtime.cpp").read_text(encoding="utf-8")
    assert "TVPUtf8ToWideCharString" in psb_source
    assert "FindUtf8ToWidePostCall" in psb_source
    assert "IsWritableRange" not in psb_source
    assert "embed_map_reloads=" in source
    assert "EngAixt_KiriKiriHookReady_" in source
    assert "launcher ready event signaled" in source
    assert "SafeInstallInternalTextHooks();" in source
    assert "SignalLauncherReady();" in source
    init_thread = source[source.index("DWORD WINAPI InitThread") : source.index("DWORD WINAPI DumpFlushThread")]
    assert init_thread.index("InstallCreateStreamHook()") < init_thread.index("SignalLauncherReady();")
    assert init_thread.index("HookGetProcAddress") < init_thread.index("SignalLauncherReady();")
    assert 'StartsWithNoCase(name, L"file://")' in source
    assert 'find_last_of(L"/\\\\>")' in source
    assert "stream normalize #" in source
    assert "AnyPatchManifestMatch" in source
    assert 'lowered == L"config.tjs"' not in source
    assert 'lowered == L"embfontlist.tjs"' not in source
    patch_stream_source = (Path(__file__).parent.parent / "native" / "kirikiri_runtime" / "kirikiri_patch_stream.cpp").read_text(encoding="utf-8")
    assert '#include "kirikiri_patch_stream.h"' in source
    assert "struct MemoryPatchStream" in patch_stream_source
    assert "kVtable" in patch_stream_source
    assert "DeletingDestructor" in patch_stream_source
    assert "OpenLoosePatchMemoryStream" in source
    assert "memory patch hit:" in source
    assert "tjs_uint64 __cdecl Seek" in patch_stream_source
    assert "tjs_uint __cdecl Read" in patch_stream_source
    assert "void *__fastcall DeletingDestructor" in patch_stream_source
    assert "CreateReadOnlyMemoryStream" in source
    assert 'kSkipSignature[] = "skip!"' in source
    assert "signature bypass:" in source
    assert "sig_bypass=" in source
    assert "internal_replaced=" in source
    assert "legacy GDI/encoding import hooks disabled" in source
    assert "if (gLegacySystemHooks) {" in source
    assert "if (!gLegacySystemHooks) return result;" in source
    assert "bool gLowLevelStreamHooks = false;" in source
    assert 'profile == L"stream"' in source
    assert 'profile == L"dump"' in source
    assert 'profile == L"patchstream"' in source
    assert "bool gDirectCreateStreamHook = false;" in source
    assert "bool gGetProcAddressHooks = false;" in source
    assert 'profile == L"display"' in source and "getproc_hooks=1" in source
    assert "direct_create_stream=1" in source
    assert "if (gDirectCreateStreamHook)" in source
    assert "GetProcAddress hook skipped in mtool profile" in source
    assert "if (gLowLevelStreamHooks) {" in source
    assert "exporter && gLowLevelStreamHooks" in source
    assert "linkExporter = new ProxyFunctionExporter(exporter)" in source
    assert "TVPCreateIStream query skipped in mtool profile" in source
    assert "storage/stream inline hooks skipped in mtool profile" in source
    assert "bool gPatchArchiveHooks = false;" in source
    assert "patch_archives=0" in source
    assert "patch archives skipped in display-only profile" in source
    assert "bool WriteWideReplacement" in source
    assert "FindTranslation(src)" in source
    assert "HookMultiByteToWideChar" in source
    assert "HookGdipDrawString" in source
    assert "GdipAddPathString" in source
    assert "InstallPatchAutoPaths();" in source


def test_kirikiri_embed_runtime_has_stable_incremental_map_contract():
    root = Path(__file__).parent.parent
    source = (root / "native" / "kirikiri_runtime" / "kirikiri_embed_runtime.cpp").read_text(encoding="utf-8")
    header = (root / "native" / "kirikiri_runtime" / "kirikiri_embed_runtime.h").read_text(encoding="utf-8")
    build = (root / "native" / "kirikiri_runtime" / "build.ps1").read_text(encoding="utf-8")

    assert "const char *ResolveUtf8(" in header
    assert "const wchar_t *ResolveWide(" in header
    assert "ApplyWideInPlace" not in header
    assert "return original;" in source
    assert "gStableStrings" in source
    assert "gStableWideStrings" in source
    assert "gWideTranslations" in source
    assert "Returned c_str() pointers remain valid after later map reloads." in source
    assert "FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE" in source
    assert "size > gFile.size" in source
    assert "ULONGLONG offset = appendOnly ? gFile.size : 0;" in source
    assert "kReloadIntervalMs = 400" in source
    assert "mode=append" in source
    assert "gWaitStarted" in source
    assert "gWaitTimeoutMs" in source
    assert "Sleep(50)" in source
    assert "PassesEmbedKrkr2WideFilter" in source
    assert "gWideWaitStarted" in source
    assert "TjsVariantStringLayout" not in source
    assert "FindHeapBufferCapacity" not in source
    assert "CompactWideForCapacity" not in source
    assert "NormalizeEmbedKrkrZText" in source
    assert "token[1] == 'p' || token[1] == 'f'" in source
    assert "IsNumericPercentToken" in source
    assert "IsHexColorToken" in source
    assert "kirikiri_embed_runtime.cpp" in build


def test_kirikiri_native_hook_tries_patch_archives_in_config_order():
    source = (Path(__file__).parent.parent / "native" / "kirikiri_runtime" / "kirikiri_native_hook.cpp").read_text(encoding="utf-8")

    assert 'gPatchArchives.push_back(L"_translation_meta/kirikiri_patch.xp3")' in source
    assert 'gPatchArchives.push_back(L"patch.xp3")' in source
    assert 'gPatchArchives.push_back(L"_translation_meta/kirikiri_patch")' in source
    assert "for (const auto &archive : gPatchArchives)" in source
    assert "gPatchArchives.rbegin()" not in source


def test_kirikiri_native_create_stream_uses_stable_trampoline():
    source = (Path(__file__).parent.parent / "native" / "kirikiri_runtime" / "kirikiri_native_hook.cpp").read_text(encoding="utf-8")
    body = source.split("tTJSBinaryStream *CallOriginalTVPCreateStream", 1)[1].split("bool DumpWholeStream", 1)[0]

    assert "gCreateStreamTrampoline" in body
    assert "RestoreCreateStreamHookLocked" not in body
    assert "ApplyCreateStreamHookLocked" not in body


def test_kirikiri_native_hook_prefers_loose_memory_streams_over_archive_patch():
    source = (Path(__file__).parent.parent / "native" / "kirikiri_runtime" / "kirikiri_native_hook.cpp").read_text(encoding="utf-8")

    assert 'bool hasLoosePatch = DirectoryExists(JoinPath(meta, L"kirikiri_patch")) && manifestLoaded;' in source
    assert 'if (hasLoosePatch) {' in source
    assert 'gPatchArchives.push_back(L"_translation_meta/kirikiri_patch")' in source
    assert "loose patch directory selected for filter-safe memory streams" in source
    assert "patch.xp3 kept as fallback; loose memory streams take priority" in source


def test_xp3_reader_skips_unknown_top_chunks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    script = root / "intro.ks"
    script.write_text("銇撱倱銇仭銇痋n", encoding="utf-8")
    out = tmp_path / "patch.xp3"
    _write_xp3_patch(out, root, [script])

    data = bytearray(out.read_bytes())
    index_offset = struct.unpack_from("<Q", data, len(XP3_SIGNATURE))[0]
    flag = data[index_offset]
    assert flag == 1
    cursor = index_offset + 1
    compressed_size, uncompressed_size = struct.unpack_from("<QQ", data, cursor)
    cursor += 16
    index = zlib.decompress(bytes(data[cursor:cursor + compressed_size]))
    unknown = b"Hxv4" + struct.pack("<Q", 4) + b"test"
    new_index = unknown + index
    new_compressed = zlib.compress(new_index, level=9)
    patched = data[:cursor] + new_compressed
    struct.pack_into("<QQ", patched, index_offset + 1, len(new_compressed), len(new_index))
    out.write_bytes(patched)

    parsed = _read_xp3_index(out)

    assert parsed.unknown_chunks == ("Hxv4",)
    assert parsed.entries[0].name == "intro.ks"


def test_kirikiri_xp3_diagnosis_detects_content_filter(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    script = root / "scenario" / "intro.ks"
    script.parent.mkdir()
    script.write_bytes(bytes(((idx * 73 + 19) & 0xFF) for idx in range(4096)))
    xp3 = tmp_path / "data.xp3"
    _write_xp3_patch(xp3, root, [script])

    diagnosis = _diagnosis_to_dict(_diagnose_xp3_archive(xp3, _read_xp3_index(xp3)))

    assert diagnosis["index_anomaly"] == "normal"
    assert diagnosis["content_signature"] == "high_entropy_unknown"
    assert diagnosis["protection_layer"] == "content_filtered"
    assert diagnosis["script_entry_count"] == 1
    assert "content filter" in diagnosis["recommended_action"]


def test_kirikiri_xp3_diagnosis_detects_index_name_obfuscation(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    files = []
    for idx, name in enumerate(["倀", "倁", "倂", "倃"]):
        payload = root / name
        payload.write_bytes(bytes(((byte * 41 + idx * 17) & 0xFF) for byte in range(256)) * 8)
        files.append(payload)
    xp3 = tmp_path / "data.xp3"
    _write_xp3_patch(xp3, root, files)

    data = bytearray(xp3.read_bytes())
    index_offset = struct.unpack_from("<Q", data, len(XP3_SIGNATURE))[0]
    flag = data[index_offset]
    assert flag == 1
    cursor = index_offset + 1
    compressed_size, _uncompressed_size = struct.unpack_from("<QQ", data, cursor)
    cursor += 16
    index = zlib.decompress(bytes(data[cursor:cursor + compressed_size]))
    unknown = b"Hxv4" + struct.pack("<Q", 4) + b"test"
    new_index = unknown + index
    new_compressed = zlib.compress(new_index, level=9)
    patched = data[:cursor] + new_compressed
    struct.pack_into("<QQ", patched, index_offset + 1, len(new_compressed), len(new_index))
    xp3.write_bytes(patched)

    diagnosis = _diagnosis_to_dict(_diagnose_xp3_archive(xp3, _read_xp3_index(xp3)))

    assert diagnosis["index_anomaly"] == "unknown_chunk"
    assert diagnosis["index_anomalies"] == ["unknown_chunk", "name_mangled"]
    assert "possible_id_as_name" in diagnosis["index_subtypes"]
    assert diagnosis["protection_layer"] == "index_and_content"
    assert diagnosis["unknown_chunks"] == ["Hxv4"]
    assert diagnosis["mangled_name_ratio"] == 1.0


def test_xp3_rewrite_replaces_entry_and_keeps_other_files(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    script = root / "intro.ks"
    other = root / "other.ks"
    script.write_text("hello\n", encoding="utf-8")
    other.write_text("keep\n", encoding="utf-8")
    source = tmp_path / "data.xp3"
    out = tmp_path / "rebuilt.xp3"
    _write_xp3_patch(source, root, [script, other])

    replaced = _rewrite_xp3_with_replacements(source, out, {"intro.ks": "你好\n".encode("utf-16")})

    assert replaced == 1
    index = _read_xp3_index(out)
    payloads = {entry.name: _read_xp3_entry_bytes(out, entry) for entry in index.entries}
    assert payloads["intro.ks"].decode("utf-16") == "你好\n"
    assert payloads["other.ks"].decode("utf-8").splitlines() == ["keep"]


def test_xp3_append_replacement_preserves_chained_index_shell(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    script = root / "intro.ks"
    other = root / "other.ks"
    script.write_text("hello\n", encoding="utf-8")
    other.write_text("keep\n", encoding="utf-8")
    source = tmp_path / "data.xp3"
    _write_xp3_patch(source, root, [script, other])

    data = bytearray(source.read_bytes())
    original_index_offset = struct.unpack_from("<Q", data, len(XP3_SIGNATURE))[0]
    first_index_offset = len(data)
    source.write_bytes(
        data[:len(XP3_SIGNATURE)]
        + struct.pack("<Q", first_index_offset)
        + data[len(XP3_SIGNATURE) + 8:]
        + b"\x80"
        + struct.pack("<Q", 0)
        + struct.pack("<Q", original_index_offset)
    )

    replaced = _append_xp3_replacement_index(source, {"intro.ks": "你好\n".encode("utf-16")})

    assert replaced == 1
    with source.open("rb") as f:
        f.seek(len(XP3_SIGNATURE))
        assert struct.unpack("<Q", f.read(8))[0] == first_index_offset
    index = _read_xp3_index(source)
    assert index.chained is True
    payloads = {entry.name: _read_xp3_entry_bytes(source, entry) for entry in index.entries}
    assert payloads["intro.ks"].decode("utf-16") == "你好\n"
    assert payloads["other.ks"].decode("utf-8").splitlines() == ["keep"]



def test_xp3_in_place_slot_patch_keeps_index_and_offsets(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    script = root / "intro.ks"
    key = 0x47
    original_text = "\ufeff" + "".join(f"[name text=\"??{i}\"]\n???????????????????????{i:03d}\n[tp]\n" for i in range(80))
    script.write_bytes(bytes(byte ^ key for byte in original_text.encode("utf-16")))
    source = tmp_path / "data.xp3"
    _write_xp3_patch(source, root, [script])
    before_entry = _read_xp3_index(source).entries[0]
    replacement = bytes(byte ^ key for byte in ("\ufeffGTABCDEF123456\n").encode("utf-16"))

    result = _patch_xp3_replacements_in_original_slots(source, {"intro.ks": replacement})

    assert result["replaced"] == 1
    after_entry = _read_xp3_index(source).entries[0]
    assert after_entry.name == before_entry.name
    assert after_entry.original_size == before_entry.original_size
    assert after_entry.stored_size == before_entry.stored_size
    assert after_entry.segments == before_entry.segments
    payload = _read_xp3_entry_bytes(source, after_entry)
    assert len(payload) == before_entry.original_size
    decoded = bytes(byte ^ key for byte in payload).decode("utf-16")
    assert decoded.lstrip("\ufeff").startswith("GTABCDEF123456")

def test_kirikiri_marks_yuzusoft_yuz_xp3_as_static_decrypt_required(tmp_path):
    game = _make_game(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    protected = root / "001"
    protected.write_bytes(b"\x01\x02\x03\x04\xff\x00\x80\x81" * 8)
    xp3 = game / "scn.xp3"
    _write_xp3_patch(xp3, root, [protected])

    data = bytearray(xp3.read_bytes())
    index_offset = struct.unpack_from("<Q", data, len(XP3_SIGNATURE))[0]
    flag = data[index_offset]
    assert flag == 1
    cursor = index_offset + 1
    compressed_size, uncompressed_size = struct.unpack_from("<QQ", data, cursor)
    cursor += 16
    index = zlib.decompress(bytes(data[cursor:cursor + compressed_size]))
    new_index = b"yuz:" + struct.pack("<Q", 16) + (123).to_bytes(16, "little") + index
    new_compressed = zlib.compress(new_index, level=9)
    patched = data[:cursor] + new_compressed
    struct.pack_into("<QQ", patched, index_offset + 1, len(new_compressed), len(new_index))
    xp3.write_bytes(patched)

    engine = KiriKiriEngine()
    items = engine.unpack(game, tmp_path / "ws")

    assert items == []
    diag = json.loads((game / "_translation_meta" / "extract_diagnostics.json").read_text(encoding="utf-8"))
    assert diag["protected_formats"] == ["yuzusoft_yuz_xp3"]
    assert diag["protected_script_count"] == 1
    assert diag["runtime_dump_targets_enabled"] is False


def test_kirikiri_selects_script_xp3_by_index_hints(tmp_path):
    game = _make_game(tmp_path)
    script_root = tmp_path / "script_root"
    script_root.mkdir()
    (script_root / "001").write_bytes(b"\x01\x02\x03\x04\xff\x00\x80\x81" * 8)
    scn = game / "scn.xp3"
    _write_xp3_patch(scn, script_root, [script_root / "001"])

    image_root = tmp_path / "image_root"
    image_root.mkdir()
    (image_root / "cg.bin").write_bytes(b"\x89PNG\r\n\x1a\n")
    bgimage = game / "bgimage.xp3"
    _write_xp3_patch(bgimage, image_root, [image_root / "cg.bin"])

    selected = _select_script_xp3_files([bgimage, scn])

    assert selected == [scn]


def test_kirikiri_select_script_xp3_ignores_generated_patch_archive(tmp_path):
    game = _make_game(tmp_path)
    script_root = tmp_path / "script_root"
    (script_root / "scenario").mkdir(parents=True)
    script = script_root / "scenario" / "intro.ks"
    script.write_text("こんにちは\n", encoding="utf-8")

    data = game / "data.xp3"
    generated_patch = game / "patch.xp3"
    _write_xp3_patch(data, script_root, [script])
    _write_xp3_patch(generated_patch, script_root, [script])

    selected = _select_script_xp3_files([generated_patch, data])

    assert selected == [data]


def test_garbro_script_entries_prefers_named_scripts_over_extensionless_noise(tmp_path):
    entries = [
        _GarbroEntry("001", 1024),
        _GarbroEntry("scenario/intro.ks.scn", 2048),
        _GarbroEntry("startup.tjs", 128),
        _GarbroEntry("image/title.png", 4096),
    ]

    selected = _garbro_script_entries(tmp_path / "scn.xp3", entries)

    assert [entry.name for entry in selected] == ["scenario/intro.ks.scn"]


def test_kirikiri_external_garbro_extracts_only_script_entries_and_marks_static_success(tmp_path, monkeypatch):
    game = _make_game(tmp_path)
    xp3 = game / "scn.xp3"
    xp3.write_bytes(XP3_SIGNATURE + b"\0" * 128)
    garbro = tmp_path / "GARbro.Console.exe"
    garbro.write_bytes(b"MZ")
    engine = KiriKiriEngine()
    engine._protected_archives = ["scn.xp3"]
    engine._protected_formats = {"yuzusoft_yuz_xp3"}
    engine._protected_script_count = 1

    calls = []

    def fake_list(tool, archive):
        assert tool == garbro
        assert archive == xp3
        return _GarbroListResult(
            [
                _GarbroEntry("scenario/intro.ks.scn", 123),
                _GarbroEntry("image/title.png", 456),
            ],
            "entries_found",
            "",
        )

    def fake_run(cmd, cwd=None, capture_output=False, timeout=None):
        calls.append((cmd, cwd, capture_output, timeout))
        assert cmd[:3] == [str(garbro), "-x", str(xp3)]
        assert "scenario/intro.ks.scn" in cmd
        assert "image/title.png" not in cmd
        out = Path(cwd) / "scenario" / "intro.ks.scn"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake scn")
        class Result:
            returncode = 0
            stdout = b""
            stderr = b""
        return Result()

    monkeypatch.setattr(engine, "_find_garbro", lambda: garbro)
    monkeypatch.setattr("engines.kirikiri.garbro._probe_garbro_entries", fake_list)
    monkeypatch.setattr("engines.kirikiri.garbro.subprocess.run", fake_run)

    assert engine._try_extract_xp3(tmp_path / "ws" / "original", [xp3]) is True
    assert calls
    assert engine._static_external_decrypt_succeeded is True
    assert engine._static_external_extractor == "garbro_console"
    assert engine._static_external_archives == ["scn.xp3"]
    assert engine._static_external_tool_probes[0].status == "entries_found"


def test_kirikiri_vntextpatch_import_recovers_native_repack_failure(tmp_path, monkeypatch):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    source = ws / "original" / "scenario.ks"
    source.parent.mkdir(parents=True)
    source.write_text("#桜\nこんにちは\n", encoding="utf-8")
    export = ws / "external_candidates" / "vntextpatch"
    export.mkdir(parents=True)
    (export / "scenario.json").write_text(
        json.dumps([{"name": "桜", "message": "こんにちは"}], ensure_ascii=False),
        encoding="utf-8",
    )

    engine = KiriKiriEngine()
    engine._game_dir = game
    engine._vntextpatch_export_dir = str(export)
    item = TextItem(
        file="scenario.ks",
        original="こんにちは",
        translated="你好",
        context="message",
        line=2,
    )
    monkeypatch.setattr(engine, "_patch_script_file", lambda *_args, **_kwargs: False)

    def fake_import(_input, _translation, output, **_kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("#桜\n你好\n", encoding="utf-8")
        return ExternalToolResult(
            tool="vntextpatch",
            status="success",
            generated_files=(output.name,),
            script_files=(output.name,),
        )

    monkeypatch.setattr("engines.kirikiri.engine.run_vntextpatch_import", fake_import)

    engine.repack([item], ws)

    assert source.read_text(encoding="utf-8") == "#桜\n你好\n"
    assert engine._static_repack_failed_files == []


def test_kirikiri_xp3pack_is_used_only_after_native_writer_failure(tmp_path, monkeypatch):
    game = _make_game(tmp_path)
    ws = tmp_path / "ws"
    source = ws / "original" / "scenario.ks"
    source.parent.mkdir(parents=True)
    source.write_text("こんにちは\n", encoding="utf-8")
    item = TextItem(
        file="scenario.ks",
        original="こんにちは",
        translated="你好",
        line=1,
        meta={"from_xp3": True, "encoding": "utf-8", "newline": "\n"},
    )
    engine = KiriKiriEngine()
    engine._game_dir = game

    def fail_native_writer(*_args, **_kwargs):
        raise ValueError("synthetic native XP3 failure")

    def fake_xp3pack(input_dir, output_archive, **_kwargs):
        files = [path for path in Path(input_dir).rglob("*") if path.is_file()]
        _write_xp3_patch(output_archive, Path(input_dir), files)
        return ExternalToolResult(
            tool="kirikiri_xp3pack",
            status="success",
            generated_files=(output_archive.name,),
        )

    monkeypatch.setattr("engines.kirikiri.engine._write_xp3_patch", fail_native_writer)
    monkeypatch.setattr("engines.kirikiri.engine.run_xp3pack", fake_xp3pack)

    engine.repack([item], ws)

    assert engine._kirikiri_xp3pack_used is True
    assert (game / "patch.xp3").exists()
    assert _read_xp3_index(game / "patch.xp3").entries


def test_kirikiri_psb_scn_extracts_and_patches_reference_sample():
    sample = _reference_magalumina_scn()
    if sample is None:
        return

    refs = extract_kirikiri_scn_texts(sample)

    assert len(refs) > 100
    assert refs[0].kind == "message"
    assert refs[0].text

    replacement = "\u6d4b\u8bd5\u4e2d\u6587\u56de\u586b\u3002"
    patched, stats = patch_kirikiri_scn_texts(sample, {refs[0].index: replacement})
    refs_after = extract_kirikiri_scn_texts(patched)

    assert stats.patched == 1
    assert next(ref for ref in refs_after if ref.index == refs[0].index).text == replacement


def _xp3_index_contains(path: Path, filename: str) -> bool:
    data = path.read_bytes()
    offset = struct.unpack_from("<Q", data, len(XP3_SIGNATURE))[0]
    flag = data[offset]
    cursor = offset + 1
    if flag == 1:
        compressed_size, uncompressed_size = struct.unpack_from("<QQ", data, cursor)
        cursor += 16
        index = zlib.decompress(data[cursor:cursor + compressed_size])
        assert len(index) == uncompressed_size
    elif flag == 0:
        size = struct.unpack_from("<Q", data, cursor)[0]
        cursor += 8
        index = data[cursor:cursor + size]
    else:
        raise AssertionError(f"unexpected XP3 index flag: {flag}")
    return filename.encode("utf-16le") in index


def _make_xp3_chained_shell(path: Path) -> None:
    data = bytearray(path.read_bytes())
    original_index_offset = struct.unpack_from("<Q", data, len(XP3_SIGNATURE))[0]
    first_index_offset = len(data)
    path.write_bytes(
        data[:len(XP3_SIGNATURE)]
        + struct.pack("<Q", first_index_offset)
        + data[len(XP3_SIGNATURE) + 8:]
        + b"\x80"
        + struct.pack("<Q", 0)
        + struct.pack("<Q", original_index_offset)
    )


def _legacy_reference_magalumina_scn() -> bytes | None:
    xp3 = Path(r"C:\baidunetdiskdownload\M2797\マガルミナ\マガルミナ\magalumina_cn.xp3")
    if not xp3.exists():
        return None
    index = _read_xp3_index(xp3)
    entry = next((entry for entry in index.entries if entry.name == "magaru_01a.txt.scn"), None)
    if entry is None:
        return None
    return _read_xp3_entry_bytes(xp3, entry)


def _reference_magalumina_scn() -> bytes:
    fixture = Path(__file__).parent / "fixtures" / "kirikiri_magalumina_intro.txt.scn"
    if fixture.exists():
        return fixture.read_bytes()

    base = Path(r"C:\baidunetdiskdownload")
    if base.exists():
        for game_dir in base.iterdir():
            if not game_dir.is_dir() or not game_dir.name.startswith("マガルミナ"):
                continue
            sample = game_dir / "_translation_meta" / "kirikiri_patch" / "magaru_01a.txt.scn"
            if sample.exists():
                return sample.read_bytes()

    xp3 = base / "M2797" / "マガルミナ" / "マガルミナ" / "magalumina_cn.xp3"
    if xp3.exists():
        index = _read_xp3_index(xp3)
        entry = next((entry for entry in index.entries if entry.name == "magaru_01a.txt.scn"), None)
        if entry is not None:
            return _read_xp3_entry_bytes(xp3, entry)

    pytest.skip("KiriKiri PSB SCN reference sample is not available on this machine")


def test_kirikiri_path_carries_no_third_party_naming():
    """The runtime must describe KiriKiri's own behaviour.

    The hook identifiers, diagnostics and the alignment document used to name
    another project as their reference. That made the docs claim a dependency
    the implementation does not have, and it meant the naming could drift back
    in unnoticed. This guards the whole surface: identifiers, log lines, the
    environment contract and the alignment document.
    """
    root = Path(__file__).parent.parent
    sources = [
        root / "native" / "kirikiri_runtime" / "kirikiri_native_hook.cpp",
        root / "native" / "kirikiri_runtime" / "kirikiri_embed_runtime.cpp",
        root / "native" / "kirikiri_runtime" / "kirikiri_embed_runtime.h",
        root / "native" / "kirikiri_runtime" / "kirikiri_embed_trace.cpp",
        root / "core" / "launcher.py",
        root / "core" / "realtime_translator.py",
        root / "core" / "pipeline.py",
        root / "core" / "detector.py",
        root / "core" / "pipeline_detect_stage.py",
        root / "engines" / "kirikiri" / "engine.py",
        root / "docs" / "kirikiri_runtime_alignment.md",
        root / "docs" / "stage-contracts.md",
        root / "docs" / "workflow-rules.md",
        root / "README.md",
        root / "THIRD_PARTY_NOTICES.md",
        root / "web" / "i18n.js",
        root / "main.py",
        root / "app.py",
        root / "core" / "translation_overlay_window.py",
        root / "frida" / "run_realtime.py",
    ]
    offenders = []
    for path in sources:
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "LunaTranslator" in line or "LunaHook" in line:
                offenders.append(f"{path.name}:{number}: {line.strip()[:90]}")

    assert not offenders, "KiriKiri naming still references another project:\n" + "\n".join(offenders)

    # The alignment document must be the engine-behaviour one, and reference it.
    doc = root / "docs" / "kirikiri_runtime_alignment.md"
    assert doc.is_file()
    assert not (root / "docs" / "kirikiri_luna_alignment.md").exists()


def test_kirikiri_capture_hook_env_keeps_the_legacy_name_alias():
    """Both environment names are written and both are read.

    A native binary deployed before the rename reads the legacy name, so the
    launcher must keep writing it; otherwise that binary silently falls back to
    the default capture profile instead of reporting a mismatch.
    """
    root = Path(__file__).parent.parent
    launcher = (root / "core" / "launcher.py").read_text(encoding="utf-8")
    native = (root / "native" / "kirikiri_runtime" / "kirikiri_native_hook.cpp").read_text(encoding="utf-8")

    assert 'native_env["KIRIKIRI_CAPTURE_HOOKS"]' in launcher
    assert 'native_env["KIRIKIRI_LUNA_CAPTURE_HOOKS"]' in launcher
    assert "$env:KIRIKIRI_CAPTURE_HOOKS" in launcher
    assert "$env:KIRIKIRI_LUNA_CAPTURE_HOOKS" in launcher

    assert 'L"KIRIKIRI_CAPTURE_HOOKS"' in native
    assert 'L"KIRIKIRI_LUNA_CAPTURE_HOOKS"' in native
    # the new name must be consulted before the legacy one
    assert native.index('L"KIRIKIRI_CAPTURE_HOOKS"') < native.index('L"KIRIKIRI_LUNA_CAPTURE_HOOKS"')
