from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tools.lora_dataset.clean import CleaningConfig, classify_genres, clean_row
from tools.lora_dataset.cli import is_local_model_row, local_only_pair_keys
from tools.lora_dataset.models import CacheRow, GameLabel
from tools.lora_dataset.reader import (
    iter_cache_rows,
    iter_legacy_cache_rows,
    load_labels,
    summarize_databases,
)
from tools.lora_dataset.strict_filter import (
    StrictFilterConfig,
    filter_existing_dataset,
    normalize_target_punctuation,
    rejection_reason,
)
from tools.lora_dataset.writer import deduplicate_records, split_records


def _row(source: str, target: str, *, source_file: str = "scene.ks") -> CacheRow:
    return CacheRow("translations_demo.db", "hash", source, target, source_file, 1, False)


def test_clean_keeps_game_controls_and_translated_text():
    result = clean_row(
        _row("%s[wait]「こんにちは」", "%s[wait]你好"),
        GameLabel(name="demo"),
        ("romance",),
        CleaningConfig(),
    )
    assert result.source_text == "%s[wait]「こんにちは」"
    assert result.target_text == "%s[wait]你好"
    assert result.primary_genre == "romance"


def test_pure_kanji_japanese_is_kept_by_default_but_can_be_excluded():
    row = _row("勝利", "胜利")
    kept = clean_row(row, GameLabel(), ("unknown",), CleaningConfig())
    dropped = clean_row(
        row,
        GameLabel(),
        ("unknown",),
        CleaningConfig(keep_ambiguous_kanji=False),
    )
    assert getattr(kept, "source_language", None) == "ja_kanji_ambiguous"
    assert getattr(dropped, "reason", None) == "source_not_japanese"


def test_clean_rejects_ai_explanation_and_placeholder_mismatch():
    ai = clean_row(_row("こんにちは", "作为AI，以下是翻译：你好"), GameLabel(), ("unknown",), CleaningConfig())
    mismatch = clean_row(_row("[name]こんにちは", "你好"), GameLabel(), ("unknown",), CleaningConfig())
    assert getattr(ai, "reason", None) == "ai_artifact_or_mojibake"
    assert getattr(mismatch, "reason", None) == "placeholder_mismatch"


def test_strict_filter_requires_explicit_japanese_and_perfect_score():
    valid = {
        "source": "こんにちは、{name}。",
        "target": "你好，{name}。",
        "metadata": {"source_language": "ja", "quality_score": 1.0},
    }
    ambiguous = {
        "source": "勝利",
        "target": "胜利",
        "metadata": {"source_language": "ja_kanji_ambiguous", "quality_score": 1.0},
    }
    low_score = {
        **valid,
        "metadata": {"source_language": "ja", "quality_score": 0.96},
    }

    assert rejection_reason(valid, StrictFilterConfig()) == ""
    assert rejection_reason(ambiguous, StrictFilterConfig()) == "source_language_not_explicit_ja"
    assert rejection_reason(low_score, StrictFilterConfig()) == "quality_score_below_threshold"


def test_strict_filter_rejects_source_mojibake_and_control_drift():
    mojibake = {
        "source": "我想,互相了",
        "target": "我想，互相自慰过了",
        "metadata": {"source_language": "ja", "quality_score": 1.0},
    }
    drift = {
        "source": "[name]こんにちは",
        "target": "你好",
        "metadata": {"source_language": "ja", "quality_score": 1.0},
    }

    assert rejection_reason(mojibake, StrictFilterConfig()) == "mojibake"
    assert rejection_reason(drift, StrictFilterConfig()) == "placeholder_mismatch"


def test_strict_filter_writes_disjoint_chatml_split(tmp_path: Path):
    source = tmp_path / "all_pairs.jsonl"
    source.write_text(
        "\n".join([
            json.dumps({
                "source": "こんにちは",
                "target": "你好",
                "metadata": {"source_language": "ja", "quality_score": 1.0},
            }, ensure_ascii=False),
            json.dumps({
                "source": "勝利",
                "target": "胜利",
                "metadata": {"source_language": "ja_kanji_ambiguous", "quality_score": 1.0},
            }, ensure_ascii=False),
        ]) + "\n",
        encoding="utf-8",
    )

    manifest = filter_existing_dataset(source, tmp_path / "strict", StrictFilterConfig(validation_ratio=0.5))

    assert manifest["counts"]["accepted"] == 1
    assert manifest["counts"]["rejected"] == 1
    assert manifest["counts"]["train"] + manifest["counts"]["validation"] == 1
    rows = []
    for name in ("train.jsonl", "validation.jsonl"):
        content = (tmp_path / "strict" / name).read_text(encoding="utf-8").strip()
        if content:
            rows.append(json.loads(content))
    assert [row["messages"][1]["content"] for row in rows] == ["こんにちは"]


def test_high_confidence_filter_rejects_semantically_suspicious_pairs():
    config = StrictFilterConfig.high_confidence()
    metadata = {"source_language": "ja", "quality_score": 1.0}

    assert rejection_reason(
        {
            "source": "これは大丈夫ですか? 何か問題がありますか?",
            "target": "这没问题吗？有什么问题吗？",
            "metadata": metadata,
        },
        config,
    ) == ""
    assert rejection_reason(
        {
            "source": "ア芸 リ預 ゼ放",
            "target": "艺 预 放",
            "metadata": metadata,
        },
        config,
    ) == "source_mangled_identifier"
    assert rejection_reason(
        {
            "source": "その後、私たちは折り紙を折りながら、取り留めのないことを話した。",
            "target": "……好的。",
            "metadata": metadata,
        },
        config,
    ) == "translation_too_short"
    assert rejection_reason(
        {
            "source": "「うん……」",
            "target": "「嗯ッ……」",
            "metadata": metadata,
        },
        config,
    ) == "target_contains_visible_japanese"
    assert rejection_reason(
        {
            "source": "あんっ! あぁ……ふぁぁんっ!!",
            "target": "「啊! 嗯……哈啊!」(注:这是典型的日语拟声词)",
            "metadata": metadata,
        },
        config,
    ) == "model_annotation"
    assert rejection_reason(
        {
            "source": "周囲に人影はなく、静かな夜だった。誰も話さなかった。",
            "target": "这是一个安静的夜晚。后来我们又走到了庭院里。什么也没有发生。我们随后回到了房间。",
            "metadata": metadata,
        },
        config,
    ) == "translation_contains_extra_sentences"


def test_high_confidence_filter_rejects_control_character_and_preserves_placeholders():
    config = StrictFilterConfig.high_confidence()
    metadata = {"source_language": "ja", "quality_score": 1.0}
    assert rejection_reason(
        {"source": "ケ\x7f・ア", "target": "凯·阿", "metadata": metadata},
        config,
    ) == "mojibake"
    assert normalize_target_punctuation("你好,%p-1;%fMS ゴシック;世界!") == (
        "你好，%p-1;%fMS ゴシック;世界！"
    )
    assert normalize_target_punctuation("这样!?真的?") == "这样！？真的？"


def test_genre_labels_override_heuristic():
    assert classify_genres("demo", ["魔法と王国", "勇者"], explicit=["romance"]) == ("romance",)
    assert "fantasy" in classify_genres("demo", ["魔法と王国", "勇者"])


def test_duplicate_conflicts_are_excluded():
    first = clean_row(_row("こんにちは", "你好", source_file="a.ks"), GameLabel(), ("romance",), CleaningConfig())
    second = clean_row(_row("こんにちは", "您好", source_file="b.ks"), GameLabel(), ("romance",), CleaningConfig())
    clean, conflicts = deduplicate_records([first, second])
    assert clean == []
    assert len(conflicts) == 1
    assert conflicts[0]["reason"] == "conflicting_translations"


def test_identical_pair_prefers_per_game_provenance_over_legacy_cache():
    per_game = clean_row(
        _row("こんにちは", "你好", source_file="scene.ks"),
        GameLabel(name="game"),
        ("romance",),
        CleaningConfig(),
    )
    legacy_row = _row("こんにちは", "你好", source_file="legacy:message")
    legacy_row = type(legacy_row)(
        **{
            **legacy_row.__dict__,
            "db_name": "translation_cache.db",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "hit_count": 100,
        }
    )
    legacy = clean_row(legacy_row, GameLabel(), ("unknown",), CleaningConfig())
    clean, conflicts = deduplicate_records([legacy, per_game])
    assert conflicts == []
    assert len(clean) == 1
    assert clean[0].source_file == "scene.ks"
    assert clean[0].primary_genre == "romance"


def test_split_is_stable_and_non_overlapping():
    records = []
    for index in range(20):
        item = clean_row(
            _row(f"こんにちは{index}", f"你好{index}"),
            GameLabel(),
            ("romance",),
            CleaningConfig(),
        )
        records.append(item)
    train_a, validation_a = split_records(records, 0.2)
    train_b, validation_b = split_records(records, 0.2)
    assert [item.source_text for item in train_a] == [item.source_text for item in train_b]
    assert [item.source_text for item in validation_a] == [item.source_text for item in validation_b]
    assert not {item.source_text for item in train_a} & {item.source_text for item in validation_a}


def test_reader_uses_cache_databases_read_only(tmp_path: Path):
    path = tmp_path / "translations_demo.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE translations (source_hash TEXT PRIMARY KEY, source_text TEXT, "
            "target_text TEXT, source_file TEXT, extraction_time INTEGER, verified INTEGER)"
        )
        connection.execute(
            "INSERT INTO translations VALUES (?, ?, ?, ?, ?, ?)",
            ("h", "こんにちは", "你好", "scene.ks", 1, 1),
        )
    rows = list(iter_cache_rows(tmp_path))
    assert len(rows) == 1
    assert rows[0].target_text == "你好"


def test_reader_supports_legacy_global_cache_and_summary(tmp_path: Path):
    path = tmp_path / "translation_cache.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE cache (hash TEXT PRIMARY KEY, original TEXT, translated TEXT, "
            "source_lang TEXT, target_lang TEXT, created_at TEXT)"
        )
        connection.execute(
            "INSERT INTO cache VALUES ('h1', 'こんにちは', '你好', 'ja', 'zh-CN', 'now')"
        )
        connection.execute(
            "CREATE TABLE cache_v2 (cache_key TEXT PRIMARY KEY, provider TEXT, model TEXT, "
            "prompt_version TEXT, source_lang TEXT, target_lang TEXT, text_type TEXT, "
            "original TEXT, normalized_text TEXT, translated TEXT, created_at INTEGER, "
            "last_hit_at INTEGER, hit_count INTEGER)"
        )
        connection.execute(
            "INSERT INTO cache_v2 VALUES "
            "('h2', 'hy_mt2', 'Hy-MT2-1.8B-Q4_K_M', 'v1', 'ja', 'zh-CN', "
            "'message', 'おはよう', 'おはよう', '早上好', 1, 1, 3)"
        )
    rows = list(iter_legacy_cache_rows(path))
    summary = summarize_databases(tmp_path / "per_game", path)[0]
    assert len(rows) == 2
    assert rows[1].provider == "hy_mt2"
    assert rows[1].hit_count == 3
    assert summary["rows"] == 2
    assert summary["source_files"] == ["legacy:cache", "legacy:message"]


def test_labels_json_accepts_filename_and_genres(tmp_path: Path):
    path = tmp_path / "labels.json"
    path.write_text(
        json.dumps({"translations_demo.db": {"name": "测试游戏", "genres": ["school"]}}),
        encoding="utf-8",
    )
    labels = load_labels(path)
    assert labels["translations_demo.db"].name == "测试游戏"
    assert labels["translations_demo.db"].genres == ("school",)


def test_local_hy_mt_model_is_excluded_by_default():
    row = _row("こんにちは", "你好")
    row = type(row)(
        **{**row.__dict__, "provider": "hy_mt2", "model": "Hy-MT2-1.8B-Q4_K_M"}
    )
    assert is_local_model_row(row)


def test_cloud_model_is_not_classified_as_local():
    row = _row("こんにちは", "你好")
    row = type(row)(**{**row.__dict__, "provider": "deepseek", "model": "deepseek-v4-flash"})
    assert not is_local_model_row(row)


def test_local_pair_fingerprint_catches_providerless_cache_copy():
    local = type(_row("こんにちは", "你好"))(
        **{
            **_row("こんにちは", "你好").__dict__,
            "db_name": "translation_cache.db",
            "provider": "hy_mt2",
            "model": "Hy-MT2-1.8B-Q4_K_M",
        }
    )
    providerless = _row("こんにちは", "你好")
    keys = local_only_pair_keys({"legacy": [local], "game": [providerless]})
    assert ("こんにちは", "你好") in keys


def test_cloud_duplicate_prevents_false_local_fingerprint_exclusion():
    local = type(_row("こんにちは", "你好"))(
        **{
            **_row("こんにちは", "你好").__dict__,
            "provider": "hy_mt2",
            "model": "Hy-MT2-1.8B-Q4_K_M",
        }
    )
    cloud = type(local)(
        **{
            **local.__dict__,
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
        }
    )
    assert local_only_pair_keys({"all": [local, cloud]}) == set()

