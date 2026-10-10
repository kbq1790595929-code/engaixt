from pathlib import Path

from engines.kirikiri import fonts


def test_kirikiri_font_patch_rewrites_loader_and_face(tmp_path, monkeypatch):
    original = tmp_path / "original"
    original.mkdir()
    source_font = tmp_path / "bundled.otf"
    source_font.write_bytes(b"font")
    script = original / "system" / "afterinit.tjs"
    script.parent.mkdir()
    script.write_text(
        'System.addFont("Kosugi-Regular.ttf", true);\n'
        'System.addFont("KosugiMaru-Regular.ttf", true);\n'
        'var face = "Kosugi Maru";\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(fonts, "find_source_han_sans", lambda: source_font)
    result = fonts.prepare_kirikiri_cjk_font_patch(
        original,
        {
            "system/afterinit.tjs": {
                "xp3_filter": {
                    "kind": "koihazi_xp3dec",
                    "file_hash": 123,
                },
            }
        },
    )

    assert result.replacement_count == 3
    assert result.files[0] == original / fonts.KIRIKIRI_CJK_FONT_FILE
    assert (original / fonts.KIRIKIRI_CJK_FONT_FILE).read_bytes() == b"font"
    rewritten = script.read_text(encoding="utf-8")
    assert rewritten.count(fonts.KIRIKIRI_CJK_FONT_FILE) == 2
    assert f'"{fonts.KIRIKIRI_CJK_FONT_FACE}"' in rewritten
    assert result.filters_by_rel == {
        "system/afterinit.tjs": {
            "kind": "koihazi_xp3dec",
            "file_hash": 123,
        }
    }


def test_kirikiri_font_patch_does_not_add_font_without_known_reference(tmp_path, monkeypatch):
    original = tmp_path / "original"
    original.mkdir()
    (original / "scenario.ks").write_text("こんにちは\n", encoding="utf-8")
    source_font = tmp_path / "bundled.otf"
    source_font.write_bytes(b"font")
    monkeypatch.setattr(fonts, "find_source_han_sans", lambda: source_font)

    result = fonts.prepare_kirikiri_cjk_font_patch(original)

    assert result.files == ()
    assert not (original / fonts.KIRIKIRI_CJK_FONT_FILE).exists()
