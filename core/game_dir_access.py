"""游戏目录可写性探测。

EngAixt 的回填、检查点镜像和运行时启动器都要写进游戏目录。用户常常直接选中
光盘、虚拟光驱或挂载镜像里的游戏（只读介质），或选中当前账户没有写权限的目录；
这两种情况在任何引擎路线上都无法完成翻译，必须在动引擎和花 API 费用之前给出
可执行的结论，而不是等到写检查点时报一个裸的 WinError 5。

本模块只回答事实（能不能写、为什么不能写），不修改任何文件，也不替调用方决定
流程走向：pipeline 用它提前停止，preflight 用它生成用户可读的建议。
"""
from __future__ import annotations

import ctypes
import os
import sys
import uuid
from pathlib import Path

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5

FILE_READ_ONLY_VOLUME = 0x00080000

_DRIVE_TYPE_LABELS = {
    DRIVE_REMOVABLE: "removable",
    DRIVE_FIXED: "fixed",
    DRIVE_REMOTE: "network",
    DRIVE_CDROM: "cdrom",
}

_READ_ONLY_MEDIA_ACTION = (
    "游戏位于只读介质（光盘／虚拟光驱／挂载镜像），翻译结果无法写回。"
    "请先运行游戏安装程序，或把整个游戏目录复制到本机硬盘的可写位置"
    "（例如 E:\\Games\\游戏名），再选择该目录重新翻译。"
)
_WRITE_DENIED_ACTION = (
    "当前账户对游戏目录没有写入权限。请把游戏复制到当前账户可写的目录"
    "（不要放在系统盘根目录或受保护目录），或用管理员身份重新运行 EngAixt。"
)
_NO_SPACE_ACTION = "游戏所在磁盘没有剩余空间，请先清理磁盘后再重新翻译。"
_MISSING_DIR_ACTION = "游戏目录不存在，请确认路径后重新选择游戏。"


def game_dir_of(game_path: str | Path) -> Path:
    """把游戏路径归一化成游戏目录（接受主 exe、目录或任意内部文件）。

    路径不存在时不猜：带扩展名按文件取其所在目录，其余按目录本身处理，
    以便如实报告“游戏目录不存在”。
    """
    path = Path(game_path)
    try:
        if path.is_dir():
            return path
        if path.is_file():
            return path.parent
    except OSError:
        pass
    return path.parent if path.suffix else path


def describe_game_dir_access(game_path: str | Path) -> dict:
    """返回游戏目录可写性事实。

    字段：
      dir / exists / writable / reason / detail / action
      drive_type / volume_root / read_only_volume / free_bytes / probe_error

    reason 取值：""（可写）、read_only_media、read_only_volume、write_denied、
    no_free_space、missing_dir。
    """
    game_dir = game_dir_of(game_path)
    facts: dict = {
        "dir": str(game_dir),
        "exists": False,
        "writable": False,
        "reason": "",
        "detail": "",
        "action": "",
        "drive_type": "",
        "volume_root": "",
        "read_only_volume": False,
        "free_bytes": None,
        "probe_error": "",
    }

    try:
        exists = game_dir.is_dir()
    except OSError:
        exists = False
    if not exists:
        facts.update({
            "reason": "missing_dir",
            "detail": f"游戏目录不存在: {game_dir}",
            "action": _MISSING_DIR_ACTION,
        })
        return facts
    facts["exists"] = True

    root = _volume_root(game_dir)
    if root:
        facts["volume_root"] = root
        drive_type = _drive_type(root)
        facts["drive_type"] = _DRIVE_TYPE_LABELS.get(drive_type, f"type_{drive_type}")
        read_only_volume = _volume_read_only(root)
        facts["read_only_volume"] = read_only_volume
        free_bytes = _free_bytes(root)
        facts["free_bytes"] = free_bytes
    else:
        drive_type = 0
        read_only_volume = False
        free_bytes = None

    probe_error = _write_probe_error(game_dir)
    if probe_error is None:
        facts["reason"] = ""
        facts["detail"] = ""
        facts["action"] = ""
        facts["writable"] = True
        return facts

    facts["probe_error"] = f"{type(probe_error).__name__}: {probe_error}"
    if drive_type == DRIVE_CDROM or read_only_volume:
        reason = "read_only_media" if drive_type == DRIVE_CDROM else "read_only_volume"
        medium = "光盘或挂载镜像" if drive_type == DRIVE_CDROM else "只读卷"
        detail = f"游戏目录位于只读介质（{medium}），无法写入译本和检查点: {game_dir}"
        action = _READ_ONLY_MEDIA_ACTION
    elif free_bytes == 0:
        reason = "no_free_space"
        detail = f"游戏所在磁盘没有剩余空间，无法写入: {game_dir}"
        action = _NO_SPACE_ACTION
    else:
        reason = "write_denied"
        detail = f"游戏目录不可写: {game_dir}（{facts['probe_error']}）"
        action = _WRITE_DENIED_ACTION

    facts.update({"reason": reason, "detail": detail, "action": action})
    return facts


def game_dir_writable(game_path: str | Path) -> tuple[bool, dict]:
    """返回 (是否可写, 可写性事实)，供只想拿结论的调用方使用。"""
    facts = describe_game_dir_access(game_path)
    return bool(facts.get("writable")), facts


def _volume_root(directory: Path) -> str:
    if sys.platform != "win32":
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumePathNameW(
            ctypes.c_wchar_p(str(directory)), buffer, 261
        )
    except Exception:
        return ""
    return buffer.value if ok else ""


def _drive_type(root: str) -> int:
    if sys.platform != "win32" or not root:
        return 0
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))
    except Exception:
        return 0


def _volume_read_only(root: str) -> bool:
    if sys.platform != "win32" or not root:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        volume_name = ctypes.create_unicode_buffer(261)
        file_system = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_uint32(0)
        max_component = ctypes.c_uint32(0)
        flags = ctypes.c_uint32(0)
        ok = kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root),
            volume_name,
            261,
            ctypes.byref(serial),
            ctypes.byref(max_component),
            ctypes.byref(flags),
            file_system,
            261,
        )
    except Exception:
        return False
    return bool(ok) and bool(flags.value & FILE_READ_ONLY_VOLUME)


def _free_bytes(root: str) -> int | None:
    if sys.platform != "win32" or not root:
        return None
    try:
        free = ctypes.c_ulonglong(0)
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(root), None, None, ctypes.byref(free)
        )
    except Exception:
        return None
    return int(free.value) if ok else None


def _write_probe_error(directory: Path) -> OSError | None:
    """实际写一个探针文件再删掉：这是唯一可靠的“能不能写”判据。

    只读属性和 ACL 都可能被继承规则掩盖，因此这里不依赖 os.access。
    同时测试临时子目录，因为部分受保护目录允许在根目录创建文件，
    却拒绝创建 EngAixt 需要的 `_translation_meta` 目录。
    """
    probe = directory / f".engaixt_write_probe_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    probe_dir = directory / f".engaixt_meta_probe_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    nested_probe = probe_dir / "probe"
    try:
        with open(probe, "wb") as handle:
            handle.write(b"engaixt")
        probe_dir.mkdir()
        with open(nested_probe, "wb") as handle:
            handle.write(b"engaixt")
    except OSError as exc:
        return exc
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
        try:
            nested_probe.unlink()
        except OSError:
            pass
        try:
            probe_dir.rmdir()
        except OSError:
            pass
    return None
