from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from engines.tyrano import TyranoEngine
from engines.tyrano.archive import find_packed_tyrano_exe
from engines.tyrano.script import extract_spans


def _make_tyrano_game(root: Path) -> Path:
    game = root / "game"
    (game / "data" / "scenario").mkdir(parents=True)
    kag = game / "tyrano" / "plugins" / "kag"
    kag.mkdir(parents=True)
    (kag / "kag.tag_system.js").write_text("// tyrano", encoding="utf-8")
    (game / "index.html").write_text("<html></html>", encoding="utf-8")
    (game / "package.json").write_text('{"name":"tyranoscript"}', encoding="utf-8")
    (game / "Game.exe").write_bytes(b"MZ")
    return game


def _make_packed_tyrano_game(root: Path) -> tuple[Path, Path, bytes]:
    game = root / "packed_game"
    game.mkdir()
    executable = game / "Game.exe"
    prefix = b"MZ" + b"native-nw-runtime" * 65536
    executable.write_bytes(prefix)
    with ZipFile(executable, "a", compression=ZIP_DEFLATED) as archive:
        archive.writestr("index.html", "<html></html>")
        archive.writestr("package.json", '{"name":"tyranoscript"}')
        archive.writestr("tyrano/plugins/kag/kag.tag_system.js", "// kag")
        archive.writestr("data/scenario/scene001.ks", '#クロエ\n「元の台詞です」[p]\n')
        archive.writestr("data/bgimage/keep.txt", "unchanged asset")
    return game, executable, prefix


def test_tyrano_detection_uses_runtime_and_scenario_markers(tmp_path):
    game = _make_tyrano_game(tmp_path)

    score, evidence = TyranoEngine().detect_confidence(game)

    assert score == 99
    assert any("kag" in item for item in evidence)
    assert TyranoEngine().detect(game / "Game.exe") is True


def test_tyrano_detects_embedded_nw_zip_without_loose_project(tmp_path):
    game, executable, _prefix = _make_packed_tyrano_game(tmp_path)

    engine = TyranoEngine()
    score, evidence = engine.detect_confidence(game)

    assert find_packed_tyrano_exe(game) == executable
    assert score == 99
    assert any("内嵌" in item for item in evidence)


def test_tyrano_extracts_visible_text_and_skips_commands():
    content = """[tb_start_text mode=4 ]
#クロエ
「今日は[r]帰りたい」[p]
[_tb_end_text]
[ptext text="選択してください" storage="画像/ボタン.png"]
[ptext text="&tf.maney_buy+'円'"]
[chara_show name="クロエ" storage="chara/クロエ.png"]
[iscript]
f.message = "翻訳してはいけない";
[endscript]
"""

    spans = extract_spans(content, require_kana=True)

    assert [span.text for span in spans] == [
        "クロエ",
        "「今日は",
        "帰りたい」",
        "選択してください",
    ]
    assert "画像/ボタン.png" not in {span.text for span in spans}
    assert "&tf.maney_buy+'円'" not in {span.text for span in spans}
    assert "翻訳してはいけない" not in {span.text for span in spans}


def test_tyrano_unpack_prefers_japanese_bak_over_existing_chinese(tmp_path):
    game = _make_tyrano_game(tmp_path)
    scenario = game / "data" / "scenario" / "scene001.ks"
    scenario.write_text("#克洛伊\n「现有劣质中文」[p]\n", encoding="utf-8")
    backup = Path(str(scenario) + ".bak")
    backup.write_text("#クロエ\n「元の日本語です」[p]\n", encoding="utf-8-sig")
    workspace = tmp_path / "workspace"

    engine = TyranoEngine()
    items = engine.unpack(game, workspace)

    assert [item.original for item in items] == ["クロエ", "「元の日本語です」"]
    assert all(item.meta["source_kind"] == "bak" for item in items)
    restored = (workspace / "original" / "data" / "scenario" / "scene001.ks").read_text(
        encoding="utf-8-sig"
    )
    assert "元の日本語" in restored
    assert "劣质中文" not in restored


def test_tyrano_current_chinese_is_not_sent_for_retranslation(tmp_path):
    game = _make_tyrano_game(tmp_path)
    scenario = game / "data" / "scenario" / "scene001.ks"
    scenario.write_text(
        '#克洛伊\n「现有中文」[p]\n[chara_show name="クロエ" storage="chara/クロエ.png"]\n',
        encoding="utf-8",
    )

    items = TyranoEngine().unpack(game, tmp_path / "workspace")

    assert items == []


def test_tyrano_repack_replaces_only_bound_visible_spans(tmp_path):
    game = _make_tyrano_game(tmp_path)
    scenario = game / "data" / "scenario" / "scene001.ks"
    scenario.write_text(
        '#クロエ\n「今日は[r]帰りたい」[p]\n[chara_show storage="chara/クロエ.png"]\n',
        encoding="utf-8-sig",
    )
    workspace = tmp_path / "workspace"
    engine = TyranoEngine()
    items = engine.unpack(game, workspace)
    translations = {
        "クロエ": "克洛伊",
        "「今日は": "「今天",
        "帰りたい」": "想回去」",
    }
    for item in items:
        item.translated = translations[item.original]

    engine.repack(items, workspace)

    patched = (workspace / "original" / "data" / "scenario" / "scene001.ks").read_text(
        encoding="utf-8-sig"
    )
    assert "#克洛伊" in patched
    assert "「今天[r]想回去」[p]" in patched
    assert 'storage="chara/クロエ.png"' in patched


def test_tyrano_embedded_repack_preserves_stub_and_unmodified_entries(tmp_path):
    game, executable, prefix = _make_packed_tyrano_game(tmp_path)
    workspace = tmp_path / "workspace"
    engine = TyranoEngine()
    items = engine.unpack(game, workspace)
    for item in items:
        item.translated = {
            "クロエ": "克洛伊",
            "「元の台詞です」": "「这是原来的台词」",
        }[item.original]

    engine.repack(items, workspace)

    assert executable.read_bytes().startswith(prefix)
    with ZipFile(executable) as archive:
        names = archive.namelist()
        scenario = archive.read("data/scenario/scene001.ks").decode("utf-8")
        assert names.count("data/scenario/scene001.ks") == 1
        assert "#克洛伊" in scenario
        assert "「这是原来的台词」[p]" in scenario
        assert archive.read("data/bgimage/keep.txt") == b"unchanged asset"
    assert engine._tyrano_repack_stats["archive_verified"] is True


def test_tyrano_embedded_extraction_rejects_parent_traversal(tmp_path):
    game, executable, _prefix = _make_packed_tyrano_game(tmp_path)
    with ZipFile(executable, "a", compression=ZIP_DEFLATED) as archive:
        archive.writestr("data/scenario/../../outside.ks", "危険なテキスト")

    workspace = tmp_path / "workspace"
    items = TyranoEngine().unpack(game, workspace)

    assert all("outside.ks" not in str(item.meta.get("script_rel")) for item in items)
    assert not (tmp_path / "outside.ks").exists()


def test_tyrano_current_japanese_wins_over_stale_japanese_bak(tmp_path):
    game = _make_tyrano_game(tmp_path)
    scenario = game / "data" / "scenario" / "scene001.ks"
    scenario.write_text("#クロエ\n「現在の日本語です」[p]\n", encoding="utf-8")
    Path(str(scenario) + ".bak").write_text(
        "#クロエ\n「古い日本語です」[p]\n",
        encoding="utf-8",
    )

    items = TyranoEngine().unpack(game, tmp_path / "workspace")

    assert "「現在の日本語です」" in {item.original for item in items}
    assert "「古い日本語です」" not in {item.original for item in items}
    assert all(item.meta["source_kind"] == "current" for item in items)


def test_detector_registers_tyrano_before_kirikiri():
    source = Path("core/detector.py").read_text(encoding="utf-8")

    assert source.index("import engines.tyrano") < source.index("import engines.kirikiri")
