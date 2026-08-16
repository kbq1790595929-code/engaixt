from __future__ import annotations

import sys
from pathlib import Path


def app_root() -> Path:
    """Return the runtime resource root for source and PyInstaller builds."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str | Path) -> Path:
    path = app_root()
    for part in parts:
        path /= Path(part)
    return path
