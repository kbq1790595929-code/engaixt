"""Static CJK font fallback for KiriKiri script patches."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from core.open_source_fonts import find_source_han_sans
from engines.kirikiri.codec import _norm_rel, _read_text_guess, _write_text
from utils.logger import info, warning


KIRIKIRI_CJK_FONT_FILE = "SourceHanSansCN-Regular.otf"
KIRIKIRI_CJK_FONT_FACE = "Source Han Sans CN"


@dataclass(frozen=True)
class KirikiriFontPatch:
    """Files and XP3 filters needed for a static font fallback."""

    files: tuple[Path, ...] = ()
    filters_by_rel: dict[str, dict[str, object]] | None = None
    replacement_count: int = 0


_FONT_FILE_RE = re.compile(
    r"(System\s*\.\s*addFont\s*\(\s*[\"'])(?:Kosugi(?:Maru)?-Regular\.ttf)([\"'])",
    re.IGNORECASE,
)

_FONT_FACE_ALIASES = (
    "Kosugi Maru",
    "Kosugi",
    "MS PGothic",
    "MS Gothic",
    "MS Mincho",
    "Meiryo",
    "\uff2d\uff33 \uff30\u660e\u671d",
    "\uff2d\uff33 \u30b4\u30b7\u30c3\u30af",
    "\uff2d\uff33 \u660e",
    "\u30e1\u30a4\u30ea\u30aa",
)


def _replace_quoted_font_faces(text: str) -> tuple[str, int]:
    changed = 0
    for alias in _FONT_FACE_ALIASES:
        for quote in ('"', "'"):
            old = f"{quote}{alias}{quote}"
            new = f"{quote}{KIRIKIRI_CJK_FONT_FACE}{quote}"
            count = text.count(old)
            if count:
                text = text.replace(old, new)
                changed += count
    return text, changed


def _rewrite_font_references(text: str) -> tuple[str, int]:
    rewritten, count = _FONT_FILE_RE.subn(
        rf"\g<1>{KIRIKIRI_CJK_FONT_FILE}\g<2>",
        text,
    )
    rewritten, face_count = _replace_quoted_font_faces(rewritten)
    return rewritten, count + face_count


def _script_filter(
    script_meta: Mapping[str, Mapping[str, object]],
    rel: str,
) -> dict[str, object] | None:
    meta = script_meta.get(rel)
    if meta is None:
        folded = rel.casefold()
        meta = next(
            (
                value
                for key, value in script_meta.items()
                if str(key).replace("\\", "/").casefold() == folded
            ),
            None,
        )
    if not isinstance(meta, Mapping):
        return None
    value = meta.get("xp3_filter")
    return dict(value) if isinstance(value, Mapping) else None


def prepare_kirikiri_cjk_font_patch(
    original_dir: Path,
    script_meta: Mapping[str, Mapping[str, object]] | None = None,
) -> KirikiriFontPatch:
    """Add a bundled CJK font and patch scripts that select unsupported fonts.

    KiriKiri resolves files from XP3 storage, so the font belongs in the same
    static patch as the changed scripts. Filter metadata is retained for every
    script touched here; otherwise a protected source archive would reject the
    patched script before the game can load it.
    """
    original_dir = Path(original_dir)
    script_meta = script_meta or {}
    candidates = sorted(
        path
        for path in original_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".ks", ".tjs"}
    )
    rewrites: list[tuple[Path, str, str, int]] = []
    for path in candidates:
        decoded = _read_text_guess(path)
        if decoded is None:
            continue
        rewritten, count = _rewrite_font_references(decoded.text)
        if count and rewritten != decoded.text:
            rewrites.append((path, decoded.encoding, rewritten, count))

    if not rewrites:
        return KirikiriFontPatch()

    font_source = find_source_han_sans()
    if font_source is None:
        warning("KiriKiri detected unsupported font references, but no bundled CJK font is available")
        return KirikiriFontPatch()

    font_target = original_dir / KIRIKIRI_CJK_FONT_FILE
    try:
        shutil.copy2(font_source, font_target)
        files: list[Path] = [font_target]
        filters: dict[str, dict[str, object]] = {}
        replacement_count = 0
        for path, encoding, rewritten, count in rewrites:
            _write_text(path, rewritten, encoding)
            files.append(path)
            rel = _norm_rel(path.relative_to(original_dir))
            xp3_filter = _script_filter(script_meta, rel)
            if xp3_filter:
                filters[rel] = xp3_filter
            replacement_count += count
    except OSError as exc:
        warning(f"KiriKiri CJK font patch failed: {exc}")
        return KirikiriFontPatch()

    info(
        "KiriKiri static CJK font fallback prepared: "
        f"{KIRIKIRI_CJK_FONT_FILE}, scripts={len(rewrites)}, replacements={replacement_count}"
    )
    return KirikiriFontPatch(
        files=tuple(dict.fromkeys(files)),
        filters_by_rel=filters,
        replacement_count=replacement_count,
    )
