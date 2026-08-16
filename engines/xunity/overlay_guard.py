from __future__ import annotations

import shutil
from pathlib import Path

from core.resources import resource_path
from utils.logger import info, warning

class UGUIOverlayGuardDeployer:
    """Deploy a helper that displays translated UGUI text on a click-through overlay.

    Some Unity visual novels use the original UGUI Text content for typewriter
    or click-to-advance logic. Replacing that content with Chinese can freeze
    progression. The helper keeps the original Text value for game logic and
    shows the translated value on a non-raycast overlay.
    """

    DLL_NAME = "GameTranslator.UGUITextOverlayGuard.dll"

    def __init__(self, game_dir: Path):
        self.game_dir = game_dir

    def deploy(self) -> bool:
        source = self._find_built_dll()
        if not source:
            warning("UGUI Overlay Guard 未找到；如遇到中文显示后无法点击下一句，可编译并部署该辅助插件。")
            return False
        dest_dir = self.game_dir / "BepInEx" / "plugins" / "GameTranslator"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / self.DLL_NAME
        shutil.copy2(source, dest)
        info(f"UGUI Overlay Guard 已部署: {dest}")
        return True

    def _find_built_dll(self) -> Path | None:
        root = Path(__file__).resolve().parent.parent
        candidates = [
            root / "build" / "UGUITextOverlayGuard" / "bin" / "Release" / "net472" / self.DLL_NAME,
            root / "tools" / self.DLL_NAME,
            root / "assets" / "bepinex_plugins" / self.DLL_NAME,
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
        return None

