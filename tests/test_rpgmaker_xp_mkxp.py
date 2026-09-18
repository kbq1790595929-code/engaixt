"""RPG Maker XP / mkxp detection regression tests."""

from __future__ import annotations

import sys
import tempfile
import importlib.util
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engines.rpgmaker import RPGMakerEngine


def test_detects_mkxp_rxdata_with_po_localization():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Data").mkdir()
        (root / "Graphics").mkdir()
        (root / "Audio").mkdir()
        (root / "Languages").mkdir()
        (root / "Data" / "Actors.rxdata").write_bytes(b"\x04\x08[]")
        (root / "SDL2.dll").write_bytes(b"")
        (root / "x64-vcruntime140-ruby250.dll").write_bytes(b"")
        (root / "Game.exe").write_bytes(b"MZ")
        (root / "Languages" / "zh_CN.po").write_text(
            'msgid ""\nmsgstr ""\n\n'
            'msgid "A large lightbulb. It\'s the sun."\n'
            'msgstr "一只大灯泡。那是太阳。"\n',
            encoding="utf-8",
        )

        engine = RPGMakerEngine()
        score, evidence = engine.detect_confidence(root)
        detected = engine.detect(root)

    assert score >= 90
    assert detected
    assert any("rxdata" in item for item in evidence)


def test_mkxp_runtime_map_prefers_aligned_po_catalogs():
    hook_path = Path(__file__).parent.parent / "frida" / "run_rpgmaker_mkxp_hook.py"
    spec = importlib.util.spec_from_file_location("run_rpgmaker_mkxp_hook_local", hook_path)
    hook = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(hook)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        languages = root / "Languages"
        languages.mkdir()
        (languages / "ja.po").write_text(
            'msgid ""\nmsgstr ""\n\n'
            'msgid "television remote"\n'
            'msgstr "テレビのリモコン"\n\n'
            'msgid "@ed [It\'s too dark to read in here.]"\n'
            'msgstr "@ed [暗すぎて読めない。]"\n',
            encoding="utf-8",
        )
        (languages / "zh_CN.po").write_text(
            'msgid ""\nmsgstr ""\n\n'
            'msgid "television remote"\n'
            'msgstr "电视遥控器"\n\n'
            'msgid "@ed [It\'s too dark to read in here.]"\n'
            'msgstr "@ed [这里太黑了，看不清。]"\n',
            encoding="utf-8",
        )
        checkpoint = root / "translation_checkpoint.json"
        checkpoint.write_text(
            '{"items":[{"original":"テレビのリモコン","translated":"暗处看不清字。"}]}',
            encoding="utf-8",
        )

        mapping = hook._load_translation_map(
            checkpoint,
            root,
            target_locale="zh_CN",
            source_locale="ja",
        )

    assert mapping["テレビのリモコン"] == "电视遥控器"
    assert mapping["@ed [暗すぎて読めない。]"] == "@ed [这里太黑了，看不清。]"
    assert mapping["[暗すぎて読めない。]"] == "[这里太黑了，看不清。]"


if __name__ == "__main__":
    test_detects_mkxp_rxdata_with_po_localization()
    test_mkxp_runtime_map_prefers_aligned_po_catalogs()
    print("RPG Maker XP/mkxp tests passed")
