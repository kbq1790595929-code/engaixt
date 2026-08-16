import os
import shutil
import uuid
from pathlib import Path
from utils.logger import info, debug, warning


class Workspace:
    def __init__(self, base_dir: Path | None = None):
        if base_dir is None:
            base_dir = Path.home() / "Downloads" / ".game_translator" / "workspaces"
        self.base = base_dir
        self.id = str(uuid.uuid4())[:8]
        self.root = self.base / f"job_{self.id}"
        self.original = self.root / "original"
        self.translated = self.root / "translated"
        self.backup = self.root / "backup"
        self._setup()

    def _setup(self):
        for d in [self.root, self.original, self.translated, self.backup]:
            d.mkdir(parents=True, exist_ok=True)
        info(f"工作区已创建: {self.root}")

    def copy_game(self, game_path: Path) -> Path:
        """复制游戏到工作区 original 目录。"""
        dest = self.original / game_path.name
        if game_path.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(game_path, dest)
        else:
            shutil.copy2(game_path, dest)
        debug(f"游戏已复制到: {dest}")
        return dest

    def safe_backup(self, target: Path) -> Path:
        """
        防线 4：安全备份 —— 回填前强制建立 .bak 备份。
        如果 .bak 已存在则跳过（不覆盖已有备份）。
        返回备份文件路径。
        """
        backup_path = Path(str(target) + ".bak")
        if not backup_path.exists():
            if target.is_file():
                shutil.copy2(target, backup_path)
            elif target.is_dir():
                shutil.copytree(target, backup_path)
            info(f"已建立安全备份: {backup_path.name}")
        else:
            debug(f"备份已存在，跳过: {backup_path.name}")
        return backup_path

    def restore_from_backup(self, target: Path) -> bool:
        """从 .bak 恢复原始文件。"""
        backup_path = Path(str(target) + ".bak")
        if not backup_path.exists():
            warning(f"未找到备份文件: {backup_path}")
            return False
        if target.is_file() or not target.exists():
            shutil.copy2(backup_path, target)
        elif target.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(backup_path, target)
        info(f"已从备份恢复: {target}")
        return True

    def cleanup(self):
        if self.root.exists():
            shutil.rmtree(self.root)
            info(f"工作区已清理: {self.root}")


def safe_replace(target_file: Path) -> Path:
    """
    独立的安全备份函数 —— 可被任何模块调用。
    只有在 .bak 不存在时才创建备份，防止重复覆盖。
    """
    backup_file = Path(str(target_file) + ".bak")
    if not backup_file.exists():
        shutil.copy2(target_file, backup_file)
        info(f"已为原文件建立安全备份: {backup_file}")
    return backup_file
