from __future__ import annotations

import shutil
from pathlib import Path

from core.resources import resource_path
from utils.logger import warning


SOURCE_HAN_SANS_CN_FONT = "SourceHanSansCN-Regular.otf"
SOURCE_HAN_SANS_LICENSE = "SOURCE_HAN_SANS_LICENSE.txt"
SOURCE_HAN_SANS_CN_URL = (
    "https://github.com/adobe-fonts/source-han-sans/raw/release/SubsetOTF/CN/"
    "SourceHanSansCN-Regular.otf"
)
SOURCE_HAN_SANS_LICENSE_URL = (
    "https://raw.githubusercontent.com/adobe-fonts/source-han-sans/release/LICENSE.txt"
)


def cjk_font_cache_dir() -> Path:
    return Path.home() / "Downloads" / ".game_translator" / "cjk_fonts" / "source_han_sans_cn"


def bundled_source_han_sans_path() -> Path:
    return resource_path("assets", SOURCE_HAN_SANS_CN_FONT)


def cached_source_han_sans_path() -> Path:
    return cjk_font_cache_dir() / SOURCE_HAN_SANS_CN_FONT


def find_source_han_sans() -> Path | None:
    for path in (bundled_source_han_sans_path(), cached_source_han_sans_path()):
        if path.exists() and path.stat().st_size > 500_000:
            return path
    return None


def ensure_source_han_sans() -> Path | None:
    existing = find_source_han_sans()
    if existing:
        return existing
    try:
        import urllib.request

        target_dir = cjk_font_cache_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        font_path = target_dir / SOURCE_HAN_SANS_CN_FONT
        license_path = target_dir / "LICENSE.txt"
        urllib.request.urlretrieve(SOURCE_HAN_SANS_CN_URL, str(font_path))
        urllib.request.urlretrieve(SOURCE_HAN_SANS_LICENSE_URL, str(license_path))
        return font_path if font_path.exists() and font_path.stat().st_size > 500_000 else None
    except Exception as exc:
        warning(f"开源中文字体下载失败: {exc}")
        return None


def deploy_source_han_sans(destination: Path) -> bool:
    source = ensure_source_han_sans()
    if not source:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True
