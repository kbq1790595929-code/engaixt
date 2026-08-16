import shutil
import zipfile
from pathlib import Path
from utils.logger import warning


def is_archive(path: Path) -> bool:
    """判断文件是否为支持的压缩包格式。"""
    if not path.is_file():
        return False
    ext = path.suffix.lower()
    return ext in (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2")


def extract_archive(archive_path: Path, dest: Path) -> Path | None:
    """解压压缩包到目标目录，成功返回目标路径，失败返回 None。"""
    dest.mkdir(parents=True, exist_ok=True)
    ext = archive_path.suffix.lower()

    try:
        if ext == ".zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(dest)
            return dest
        elif ext in (".rar", ".7z", ".tar", ".gz", ".bz2"):
            return _extract_with_patool(archive_path, dest)
        else:
            return None
    except Exception as e:
        warning(f"解压失败: {archive_path.name} - {e}")
        return None


def _extract_with_patool(archive_path: Path, dest: Path) -> Path | None:
    try:
        import patoolib
        patoolib.extract_archive(str(archive_path), outdir=str(dest))
        return dest
    except ImportError:
        warning("patool 未安装，尝试系统命令解压")
    except Exception as e:
        warning(f"patool 解压失败: {e}")

    # fallback: 尝试用系统命令
    ext = archive_path.suffix.lower()
    import subprocess
    try:
        if ext == ".7z":
            subprocess.run(["7z", "x", str(archive_path), f"-o{str(dest)}", "-y"],
                           capture_output=True, check=True)
            return dest
        elif ext == ".rar":
            subprocess.run(["unrar", "x", "-y", str(archive_path), str(dest)],
                           capture_output=True, check=True)
            return dest
        elif ext in (".tar", ".gz", ".bz2"):
            subprocess.run(["tar", "-xf", str(archive_path), "-C", str(dest)],
                           capture_output=True, check=True)
            return dest
    except FileNotFoundError as e:
        warning(f"系统解压命令未找到: {e}")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else e.stderr
        warning(f"系统解压命令失败: {stderr or e}")

    return None


def find_game_root(extracted_dir: Path) -> Path | None:
    """在解压目录中寻找游戏根目录（如果有嵌套目录，找到最可能是游戏根目录的那一层）。"""
    if not extracted_dir.is_dir():
        return None

    # 宽松匹配：检查是否本身就是一个游戏目录
    if _looks_like_game_dir(extracted_dir):
        return extracted_dir

    # 如果只有一个子目录，深入进去
    subdirs = [d for d in extracted_dir.iterdir() if d.is_dir()]
    if len(subdirs) == 1:
        deeper = find_game_root(subdirs[0])
        if deeper:
            return deeper

    return extracted_dir


def _looks_like_game_dir(path: Path) -> bool:
    signs = [
        "*.exe", "*.bat", "game", "data", "www", "renpy",
        "*.rpy", "*.rpyc", "Game.rpgproject", "*.wolf",
    ]
    count = 0
    for pattern in signs:
        if list(path.glob(pattern)):
            count += 1
    return count >= 1
