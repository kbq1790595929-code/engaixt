"""XUnity 环境诊断：路径编码、杀毒软件、语言锁与自定义 Assembly Resolver 检测。"""

from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# 路径编码兼容检查
# ---------------------------------------------------------------------------

def check_path_encoding(game_dir: Path) -> list[str]:
    """检查路径是否包含可能导致问题的字符。

    返回警告信息列表。Windows 上非 ASCII 路径在 subprocess
    调用和某些库的 C 扩展中可能触发编码错误。
    """
    warnings_list: list[str] = []
    path_str = str(game_dir.resolve())

    # 检查中文/特殊字符
    if any(ord(c) > 127 for c in path_str):
        # 验证 pathlib 能否正常处理
        try:
            test_file = game_dir / ".gt_test_write"
            test_file.write_text("test", encoding="utf-8")
            test_file.unlink()
        except (OSError, PermissionError) as e:
            warnings_list.append(f"路径写入测试失败: {e}")
            warnings_list.append(f"请将游戏移到纯英文路径下运行")

        # 检查 Python 文件系统编码
        import sys
        fs_enc = sys.getfilesystemencoding()
        if fs_enc and "utf" not in fs_enc.lower():
            warnings_list.append(f"系统文件编码为 {fs_enc}，中文路径可能出错")
            warnings_list.append("建议添加环境变量: PYTHONUTF8=1")

    return warnings_list


# ---------------------------------------------------------------------------
# 杀软检测与用户指引
# ---------------------------------------------------------------------------

# 已知会误报的杀软进程名
_AV_PROCESSES = {
    "360sd.exe": "360杀毒",
    "360tray.exe": "360安全卫士",
    "ZhuDongFangYu.exe": "360主动防御",
    "QQPCRTP.exe": "腾讯电脑管家",
    "QQPCTray.exe": "腾讯电脑管家",
    "MsMpEng.exe": "Windows Defender",
    "NisSrv.exe": "Windows Defender",
    "kavtray.exe": "卡巴斯基",
    "avp.exe": "卡巴斯基",
}

# Doorstop DLL 可能被杀软误删的文件
_DOORSTOP_AT_RISK_FILES = ["winhttp.dll", "version.dll", "xinput9_1_0.dll", "doorstop_config.ini"]


def check_antivirus() -> list[str]:
    """检测运行中的杀软，返回用户指引列表。"""
    try:
        import subprocess
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10
        )
    except Exception:
        return []

    found_av: set[str] = set()
    for line in result.stdout.splitlines():
        for proc_name, av_name in _AV_PROCESSES.items():
            if proc_name.lower() in line.lower():
                found_av.add(av_name)

    if not found_av:
        return []

    guidance = [
        f"检测到以下安全软件: {', '.join(sorted(found_av))}",
        "",
        "Doorstop (winhttp.dll) 会被部分杀软误报为 DLL 劫持木马。",
        "请在使用前将以下文件添加信任/白名单：",
    ]
    for f in _DOORSTOP_AT_RISK_FILES:
        guidance.append(f"  - 游戏目录\\{f}")
    guidance.append("")
    guidance.append("操作指引：")
    if "360" in " ".join(found_av):
        guidance.append("  360: 设置 → 安全防护中心 → 信任与阻止 → 添加目录信任")
    if "腾讯" in " ".join(found_av):
        guidance.append("  电脑管家: 病毒查杀 → 信任区 → 添加文件夹")
    if "Windows Defender" in " ".join(found_av):
        guidance.append("  Defender: 病毒和威胁防护 → 管理设置 → 排除项 → 添加文件夹")
    guidance.append("")
    guidance.append(f"如杀软已删除 DLL，重新运行本工具即可自动重新部署。")

    return guidance


# ---------------------------------------------------------------------------
# 多语言锁检测
# ---------------------------------------------------------------------------

# 游戏可能检测这些 API 来强制切换 UI 语言
_LANG_LOCK_PATTERNS = [
    b"Application.systemLanguage",
    b"get_systemLanguage",
    b"SystemLanguage",
    b"CultureInfo.CurrentCulture",
    b"Locale.GetDefault",
    b"get_appLanguage",
    b"m_SystemLanguage",
]


def detect_language_lock(game_dir: Path) -> tuple[bool, list[str]]:
    """检测游戏是否有语言锁机制。

    扫描 Assembly-CSharp.dll 中是否使用了系统语言 API 来强制切换 UI 语言。
    如果存在，游戏可能检测到非日文系统就显示英文，导致 XUAT 翻译的是英文而非日文。

    返回 (has_lock, evidence)。
    """
    evidence: list[str] = []

    # Mono 架构：检查 Managed DLL
    for data_dir in game_dir.glob("*_Data"):
        managed = data_dir / "Managed"
        if not managed.is_dir():
            continue
        for dll_path in sorted(managed.glob("*.dll")):
            if dll_path.stat().st_size > 50_000_000:
                continue  # 跳过超大 DLL
            try:
                data = dll_path.read_bytes()
                for pattern in _LANG_LOCK_PATTERNS:
                    if pattern in data:
                        evidence.append(f"{dll_path.name}: 含 {pattern.decode()}")
                        break
            except Exception:
                continue

    # IL2CPP 架构：检查 global-metadata.dat
    for data_dir in game_dir.glob("*_Data"):
        meta = data_dir / "il2cpp_data" / "Metadata" / "global-metadata.dat"
        if meta.exists():
            try:
                data = meta.read_bytes()
                for pattern in _LANG_LOCK_PATTERNS:
                    if pattern in data:
                        evidence.append(f"global-metadata.dat: 含 {pattern.decode()}")
                        break
            except Exception:
                continue

    return len(evidence) > 0, evidence


# ---------------------------------------------------------------------------
# 自定义 Assembly Resolver 检测
# ---------------------------------------------------------------------------

def detect_custom_assembly_resolver(game_dir: Path) -> tuple[bool, str | None]:
    """检测游戏是否重写了 AssemblyResolve 事件。

    部分游戏自定义了 AppDomain.AssemblyResolve，将所有不认识
    的程序集重定向到 null 或抛出异常，导致 XUAT 的依赖 DLL 加载失败。
    此时需要将 XUAT 所有依赖 DLL 复制到 Managed/ 目录。

    返回 (has_custom_resolver, guidance_message)。
    """
    for data_dir in game_dir.glob("*_Data"):
        managed = data_dir / "Managed"
        if not managed.is_dir():
            continue
        for dll_path in managed.glob("*.dll"):
            if dll_path.stat().st_size > 50_000_000:
                continue
            try:
                data = dll_path.read_bytes()
                if b"AssemblyResolve" in data and b"add_" in data:
                    # 有订阅 AssemblyResolve，需要进一步确认
                    # 简单启发式：同一个 DLL 中也有 ResolveEventHandler
                    if b"ResolveEventHandler" in data or b"AssemblyName" in data:
                        guidance = (
                            f"检测到 {dll_path.name} 包含自定义 Assembly Resolver。\n"
                            "这可能导致 XUAT 依赖程序集加载失败。\n"
                            "如翻译不生效，请将 BepInEx/plugins/ 下所有 DLL\n"
                            f"复制一份到 {managed} 目录下。"
                        )
                        return True, guidance
            except Exception:
                continue

    return False, None
