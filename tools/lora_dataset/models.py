from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GameLabel:
    """Optional human labels for one per-game cache database."""

    name: str = ""
    genres: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class CacheRow:
    db_name: str
    source_hash: str
    source_text: str
    target_text: str
    source_file: str
    extraction_time: int | None
    verified: bool
    source_lang: str = ""
    target_lang: str = ""
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    text_type: str = ""
    hit_count: int = 0

    @property
    def game_id(self) -> str:
        return self.db_name.removesuffix(".db")


@dataclass(frozen=True)
class CleanRecord:
    source_hash: str
    source_text: str
    target_text: str
    source_file: str
    db_name: str
    game_id: str
    game_name: str
    genres: tuple[str, ...]
    primary_genre: str
    text_type: str
    source_language: str
    quality_score: float
    validation_warnings: tuple[str, ...] = field(default_factory=tuple)
    verified: bool = False
    source_lang: str = "ja"
    target_lang: str = "zh-CN"
    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    hit_count: int = 0


@dataclass(frozen=True)
class RejectedRecord:
    db_name: str
    source_hash: str
    source_text: str
    target_text: str
    source_file: str
    reason: str
    detail: str = ""
