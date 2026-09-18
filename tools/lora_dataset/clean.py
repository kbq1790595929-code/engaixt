from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from utils.text_extract import (
    extract_placeholders,
    is_mojibake,
    is_translatable,
    verify_translation,
)

from .models import CacheRow, CleanRecord, GameLabel, RejectedRecord


GENRE_NAMES = {
    "romance": "romance",
    "school": "school",
    "fantasy": "fantasy",
    "sci_fi": "sci_fi",
    "mystery": "mystery",
    "horror": "horror",
    "historical": "historical",
    "action_adventure": "action_adventure",
    "adult": "adult",
    "comedy": "comedy",
    "unknown": "unknown",
}

GENRE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "romance": ("恋愛", "恋人", "好き", "愛して", "告白", "彼女", "彼氏", "幼馴染", "キス", "デート"),
    "school": ("学園", "学校", "教室", "先生", "生徒", "先輩", "後輩", "部活", "学生", "制服"),
    "fantasy": ("魔法", "魔物", "王国", "勇者", "異世界", "妖精", "竜", "城", "錬金", "精霊"),
    "sci_fi": ("宇宙", "未来", "ロボット", "科学", "研究所", "人工知能", "AI", "機械", "銀河"),
    "mystery": ("探偵", "事件", "犯人", "謎", "真相", "推理", "証拠", "密室", "失踪"),
    "horror": ("恐怖", "呪い", "幽霊", "怪異", "血", "死体", "悪夢", "怨霊", "廃墟"),
    "historical": ("戦国", "江戸", "明治", "昭和", "武士", "侍", "幕末", "歴史", "王朝"),
    "action_adventure": ("戦闘", "冒険", "旅", "剣", "銃", "敵", "ダンジョン", "クエスト", "戦士"),
    "adult": ("18禁", "成人", "セックス", "淫", "調教", "寝取", "性行為", "裸", "精液", "エッチ"),
    "comedy": ("ギャグ", "冗談", "笑", "面白", "コメディ", "ふざけ"),
}

_KANA_RE = re.compile(r"[\u3040-\u30ff\uff66-\uff9f]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_JAPANESE_PUNCT_RE = re.compile(r"[。、「」『』〜ー？！]" )
_AI_ARTIFACT_RE = re.compile(
    r"(?:作为(?:一个)?AI|我是(?:一个)?AI|翻译结果\s*[:：]|以下是翻译|无法提供翻译|抱歉，?我)",
    re.IGNORECASE,
)
_CONTROL_ONLY_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)
_CHOICE_RE = re.compile(r"^(?:はい|いいえ|是|否|确定|取消|继续|返回|开始|结束|OK|Yes|No)$", re.I)
_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|(?:\.\.?[\\/])|(?:https?://))")


@dataclass(frozen=True)
class CleaningConfig:
    min_visible_chars: int = 2
    max_chars: int = 2000
    min_quality_score: float = 0.65
    keep_ambiguous_kanji: bool = True
    keep_unchanged: bool = False


def normalize_for_dataset(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


def visible_text(text: str) -> str:
    value = re.sub(r"\s+", "", text or "")
    for token in extract_placeholders(value):
        value = value.replace(token, "")
    return value


def source_language(source: str, target: str) -> str:
    if _KANA_RE.search(source):
        return "ja"
    if _CJK_RE.search(source) and _CJK_RE.search(target) and source.strip() != target.strip():
        return "ja_kanji_ambiguous"
    return "other"


def classify_genres(
    hint: str,
    samples: Iterable[str],
    explicit: Iterable[str] = (),
) -> tuple[str, ...]:
    explicit_clean = tuple(dict.fromkeys(g for g in explicit if g in GENRE_NAMES and g != "unknown"))
    if explicit_clean:
        return explicit_clean

    signal = " ".join([hint, *list(samples)][:220]).lower()
    scores: dict[str, int] = {}
    for genre, keywords in GENRE_KEYWORDS.items():
        score = 0
        for keyword in keywords:
            count = signal.count(keyword.lower())
            if count:
                # Rare, specific terms carry more weight than broad terms.
                score += min(count, 4) * (3 if len(keyword) >= 3 else 1)
        if score:
            scores[genre] = score
    if not scores:
        return ("unknown",)

    ordered = sorted(scores, key=lambda item: (-scores[item], item))
    primary = ordered[0]
    selected = [primary]
    for genre in ordered[1:]:
        if scores[genre] >= max(3, scores[primary] * 0.45):
            selected.append(genre)
    return tuple(selected[:4])


def infer_text_type(source: str, source_file: str, declared: str = "") -> str:
    stripped = source.strip()
    if declared in {"choice", "name", "speaker", "character", "ui", "system", "menu", "button", "label"}:
        return "name" if declared in {"name", "speaker", "character"} else (
            "choice" if declared == "choice" else "ui/system"
        )
    lower_file = source_file.lower()
    if _CHOICE_RE.fullmatch(stripped):
        return "choice"
    if any(token in lower_file for token in ("name", "speaker", "character", "voice")):
        return "name"
    if len(visible_text(stripped)) <= 12 and not re.search(r"[。！？!?]", stripped):
        return "ui/system"
    if stripped.startswith(("「", "『", '"', "'")):
        return "dialogue"
    return "narration"


def _quality_score(
    source: str,
    target: str,
    language: str,
    warnings: tuple[str, ...],
) -> float:
    score = 0.35
    if language == "ja":
        score += 0.20
    elif language == "ja_kanji_ambiguous":
        score += 0.08
    if _CJK_RE.search(target):
        score += 0.20
    if not is_mojibake(target):
        score += 0.10
    if set(extract_placeholders(source)) == set(extract_placeholders(target)):
        score += 0.08
    visible_source = max(1, len(visible_text(source)))
    visible_target = max(1, len(visible_text(target)))
    ratio = visible_target / visible_source
    if 0.20 <= ratio <= 4.50:
        score += 0.07
    score -= min(0.15, len(warnings) * 0.04)
    return round(max(0.0, min(1.0, score)), 4)


def clean_row(
    row: CacheRow,
    label: GameLabel,
    genres: tuple[str, ...],
    config: CleaningConfig,
) -> CleanRecord | RejectedRecord:
    source = normalize_for_dataset(row.source_text)
    target = normalize_for_dataset(row.target_text)
    if not source or not target:
        return RejectedRecord(row.db_name, row.source_hash, source, target, row.source_file, "empty")
    if len(source) > config.max_chars or len(target) > config.max_chars:
        return RejectedRecord(row.db_name, row.source_hash, source, target, row.source_file, "too_long")
    if "\x00" in source or "\x00" in target:
        return RejectedRecord(row.db_name, row.source_hash, source, target, row.source_file, "nul_character")
    if _PATH_RE.match(source) or _CONTROL_ONLY_RE.fullmatch(visible_text(source)):
        return RejectedRecord(row.db_name, row.source_hash, source, target, row.source_file, "code_or_path")
    declared_source_lang = row.source_lang.strip().lower()
    declared_target_lang = row.target_lang.strip().lower()
    if declared_source_lang and declared_source_lang not in {"ja", "ja-jp", "japanese"}:
        return RejectedRecord(row.db_name, row.source_hash, source, target, row.source_file, "source_language_filter")
    if declared_target_lang and not declared_target_lang.startswith(("zh", "cn")):
        return RejectedRecord(row.db_name, row.source_hash, source, target, row.source_file, "target_language_filter")
    language = source_language(source, target)
    if language == "other" or (language == "ja_kanji_ambiguous" and not config.keep_ambiguous_kanji):
        return RejectedRecord(row.db_name, row.source_hash, source, target, row.source_file, "source_not_japanese")
    if len(visible_text(source)) < config.min_visible_chars:
        return RejectedRecord(row.db_name, row.source_hash, source, target, row.source_file, "too_short")
    if not is_translatable(source):
        return RejectedRecord(row.db_name, row.source_hash, source, target, row.source_file, "not_translatable")
    if source == target and not config.keep_unchanged:
        return RejectedRecord(row.db_name, row.source_hash, source, target, row.source_file, "unchanged")
    if not _CJK_RE.search(target):
        return RejectedRecord(row.db_name, row.source_hash, source, target, row.source_file, "target_not_chinese")
    if _AI_ARTIFACT_RE.search(target) or is_mojibake(target):
        return RejectedRecord(row.db_name, row.source_hash, source, target, row.source_file, "ai_artifact_or_mojibake")

    source_placeholders = set(extract_placeholders(source))
    target_placeholders = set(extract_placeholders(target))
    if source_placeholders != target_placeholders:
        return RejectedRecord(
            row.db_name,
            row.source_hash,
            source,
            target,
            row.source_file,
            "placeholder_mismatch",
            f"missing={sorted(source_placeholders - target_placeholders)} "
            f"extra={sorted(target_placeholders - source_placeholders)}",
        )

    safe_target, warning_list = verify_translation(source, target)
    warnings = tuple(str(item) for item in warning_list)
    if not safe_target or safe_target == source:
        return RejectedRecord(
            row.db_name,
            row.source_hash,
            source,
            target,
            row.source_file,
            "translation_validation_failed",
            "; ".join(warnings),
        )
    target = normalize_for_dataset(safe_target)
    score = _quality_score(source, target, language, warnings)
    if score < config.min_quality_score:
        return RejectedRecord(
            row.db_name,
            row.source_hash,
            source,
            target,
            row.source_file,
            "low_quality_score",
            f"score={score:.4f}",
        )
    game_name = label.name or row.game_id
    primary_genre = genres[0] if genres else "unknown"
    return CleanRecord(
        source_hash=row.source_hash,
        source_text=source,
        target_text=target,
        source_file=row.source_file,
        db_name=row.db_name,
        game_id=row.game_id,
        game_name=game_name,
        genres=genres or ("unknown",),
        primary_genre=primary_genre,
        text_type=infer_text_type(source, row.source_file, row.text_type),
        source_language=language,
        quality_score=score,
        validation_warnings=warnings,
        verified=row.verified,
        source_lang=row.source_lang or "ja",
        target_lang=row.target_lang or "zh-CN",
        provider=row.provider,
        model=row.model,
        prompt_version=row.prompt_version,
        hit_count=row.hit_count,
    )
