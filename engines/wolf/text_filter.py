from __future__ import annotations

import re
import unicodedata


_INTERNAL_FIELD_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("editor_comment", re.compile(r"(?:コメント|メモ|memo)", re.IGNORECASE)),
    ("search_tag", re.compile(r"(?:検索.*タグ|(?:^|[_\s])tag(?:$|[_\s]))", re.IGNORECASE)),
    ("spawn_metadata", re.compile(r"出現.*ダンジョン", re.IGNORECASE)),
    ("movement_directive", re.compile(r"移動指示", re.IGNORECASE)),
    (
        "audio_resource",
        re.compile(
            r"(?:^(?:se\d*|bgm\d*|bgs\d*|voice\d*|hse\d*)$|"
            r"効果音|(?:音声|音楽|サウンド).*ファイル|音ファイル|ファイル名)",
            re.IGNORECASE,
        ),
    ),
    (
        "image_resource",
        re.compile(r"(?:^絵\d+(?:_move)?$|(?:画像|ピクチャ|グラフィック).*ファイル)", re.IGNORECASE),
    ),
    ("alternate_language", re.compile(r"(?:english|英語|[（(]英[）)])", re.IGNORECASE)),
    (
        "internal_behavior",
        re.compile(r"(?:^行動種類_|敵テーブル|参考\s*:\s*必要フラグ)", re.IGNORECASE),
    ),
)

_RESOURCE_PATH_RE = re.compile(
    r"(?:^|[/\\])[^\r\n/\\]+\."
    r"(?:png|jpe?g|bmp|webp|gif|wav|ogg|mp3|opus|mid|midi|m4a|avi|webm|mp4)$",
    re.IGNORECASE,
)
_WOLF_CONTROL_RE = re.compile(r"\\[A-Za-z][A-Za-z0-9_]*(?:\[[^\[\]]*\])?")
_ANGLE_CONTROL_RE = re.compile(r"<[A-Za-z][^>]*>")
_NUMERIC_ARGUMENT_RE = re.compile(r"^[+\-]?\d+(?:\.\d+)?(?:\s*[,;:]\s*[+\-]?\d+(?:\.\d+)?)*$")
_VISIBLE_LETTER_RE = re.compile(r"[A-Za-z\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")
_INTERNAL_DIAGNOSTIC_RE = re.compile(
    r"^(?:\\>\s*)?「?X\[移\].{0,160}(?:エラー|初期化|コード取得|コード設定)",
    re.DOTALL,
)
_INTERNAL_VALUE_NOTE_RE = re.compile(r"^ここの値は「.+?」処理でセットされます$")


def classify_database_value(field_name: str, value: str) -> str | None:
    """Return why a WOLF database value is internal, or None if it may be visible."""
    normalized_field = unicodedata.normalize("NFKC", str(field_name or "")).strip()
    for reason, pattern in _INTERNAL_FIELD_RULES:
        if pattern.search(normalized_field):
            return reason
    return classify_wolf_text(value)


def classify_wolf_text(value: str) -> str | None:
    """Reject resource references and control-only expressions before AI translation."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not normalized:
        return "empty"
    if _looks_like_resource_reference(normalized):
        return "resource_reference"
    if _looks_like_control_reference(normalized):
        return "control_reference"
    if _INTERNAL_DIAGNOSTIC_RE.search(normalized) or _INTERNAL_VALUE_NOTE_RE.search(normalized):
        return "internal_diagnostic"
    return None


def _looks_like_resource_reference(value: str) -> bool:
    lines = [line.strip() for line in value.replace("\r", "\n").split("\n") if line.strip()]
    if not lines or not _RESOURCE_PATH_RE.search(lines[0]):
        return False
    return all(_RESOURCE_PATH_RE.search(line) or _NUMERIC_ARGUMENT_RE.fullmatch(line) for line in lines)


def _looks_like_control_reference(value: str) -> bool:
    visible = _ANGLE_CONTROL_RE.sub("", value)
    visible = re.sub(r"^@[0-9]+(?:\s|$)", "", visible)
    # Nested database references are removed from the inside out.
    for _ in range(12):
        reduced = _WOLF_CONTROL_RE.sub("", visible)
        if reduced == visible:
            break
        visible = reduced
    visible = re.sub(r"[\s\d０-９\[\]{}()（）<>:：;；,，.。/+\-*＝=_%％#＃&＆|｜]+", "", visible)
    return not _VISIBLE_LETTER_RE.search(visible)
