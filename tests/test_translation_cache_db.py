from __future__ import annotations

from engines.base import TextItem


def test_game_cache_accepts_bgi_ruby_visible_translation(tmp_path, monkeypatch):
    import core.translation_cache_db as cache_db

    monkeypatch.setattr(cache_db, "DB_DIR", tmp_path / "cache")
    cache_db.DB_DIR.mkdir()

    game_dir = tmp_path / "game"
    game_dir.mkdir()
    item = TextItem(
        file="data01110.arc::pg00_com",
        original="送信者の名前は、『<rギフトアップル>ＧｉｆｔＡｐｆｅｌ</r>』。",
        translated="发信人的名字是『ＧｉｆｔＡｐｆｅｌ』。",
        context="message",
        meta={"arc": "data01110.arc", "archive_kind": "arc20", "entry": "pg00_com"},
    )

    cache_db.save_translations_to_cache(game_dir, [item])

    restored = TextItem(
        file=item.file,
        original=item.original,
        context=item.context,
        meta=item.meta,
    )
    assert cache_db.load_translations_from_cache(game_dir, [restored]) == 1
    assert restored.translated == item.translated


def test_game_cache_does_not_reuse_an_older_translation_contract(tmp_path, monkeypatch):
    import core.translation_cache_db as cache_db

    monkeypatch.setattr(cache_db, "DB_DIR", tmp_path / "cache")
    cache_db.DB_DIR.mkdir()
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    original = "\\n<\u304a\u3058\u3058>\u300c\u5916\u306e\u4e16\u754c\u306e\u8a71\u300d"

    legacy = TextItem(file="hook", original=original, translated="\u9519\u4f4d\u65e7\u8bd1\u6587")
    cache_db.save_translations_to_cache(game_dir, [legacy])

    current = TextItem(
        file="hook",
        original=original,
        meta={"translation_cache_scope": "rpgmaker_message_v2"},
    )
    assert cache_db.load_translations_from_cache(game_dir, [current]) == 0
    assert current.translated == ""
