"""运行时部署器：BepInEx、XUAT 插件、字体、翻译缓存与 UGUI 覆盖防护。"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from engines.base import TextItem
from utils.logger import info, debug, warning, error

from engines.xunity.constants import (
    BEPINEX_PRELOADER_ENTRY,
    BEPINEX_SOURCES,
    FONT_DOWNLOAD_URL,
    SYSTEM_FONT_CANDIDATES,
    TOOLS_CACHE,
    XUNITY_DOWNLOAD_URLS,
    _BEPINEX_FRAMEWORK_PREFIXES,
    _BUNDLED_DATA,
    _PATCH_ASSET_DIR,
    _REF_TOOL_IL2CPP_X64,
    _REF_TOOL_IL2CPP_X86,
    _REF_TOOL_MONO_X64,
    _REF_TOOL_MONO_X86,
    _REF_TOOL_TRANS,
)
from engines.xunity.detect import (
    UnityArchitecture,
    detect_bcl_stripping,
    detect_unity_arch,
    detect_unity_bits,
    detect_unity_version,
    get_bepinex_variant,
    is_legacy_unity_runtime_injection_blocked,
    parse_unity_year,
)


# ---------------------------------------------------------------------------
# BepInEx 部署器
# ---------------------------------------------------------------------------

class BepInExDeployer:
    """BepInEx 运行时自动部署。

    根据 Unity 架构（Mono/IL2CPP）、位数（x86/x64）、版本号，
    自动选取兼容的 BepInEx 版本并部署到游戏目录。
    """

    def __init__(self, game_dir: Path):
        self.game_dir = game_dir
        self.arch = detect_unity_arch(game_dir)
        self.bits = detect_unity_bits(game_dir)
        self.unity_version = detect_unity_version(game_dir)

    def is_deployed(self) -> bool:
        """检查 BepInEx 是否已部署且核心文件完整。"""
        blocked, version = is_legacy_unity_runtime_injection_blocked(self.game_dir)
        if blocked:
            self._disable_doorstop_config()
            warning(
                f"检测到 Unity {version}，已保持 BepInEx/doorstop 注入禁用，避免游戏菜单或对话无法点击。"
            )
            return False

        bep_dir = self.game_dir / "BepInEx"
        core_dir = bep_dir / "core"
        if not (core_dir.is_dir() and any(core_dir.glob("*.dll"))):
            return False

        # 校验已部署的 BepInEx 版本是否与当前 Unity 版本/架构匹配。
        # 防止 Unity 6000 Mono 游戏沿用早先错装的 BepInEx 5.x（缺少 Unity.Mono.Preloader），
        # 那会在预加载阶段崩溃：Method not found: System.Reflection.Module.GetPEKind。
        variant = get_bepinex_variant(self.game_dir)
        entry = BEPINEX_PRELOADER_ENTRY.get(variant) if variant else None
        if entry and not (core_dir / entry).exists():
            warning(
                f"已部署的 BepInEx 与 Unity 不匹配（core 缺少 {entry}，应为 variant={variant}），"
                "将重新部署正确版本。"
            )
            return False

        # 检查 doorstop 代理 DLL 是否存在
        doorstop = self.game_dir / "winhttp.dll"
        if not doorstop.exists():
            # 也检查备用名称
            for alt in ["version.dll", "xinput9_1_0.dll"]:
                if (self.game_dir / alt).exists():
                    return True
            return False
        return True

    def deploy(self) -> bool:
        """部署 BepInEx 到游戏目录。"""
        blocked, version = is_legacy_unity_runtime_injection_blocked(self.game_dir)
        if blocked:
            self._disable_doorstop_config()
            warning(
                f"跳过 BepInEx 部署：Unity {version} 属于旧版运行时注入风险区，"
                "doorstop 注入会导致部分游戏无法点击。"
            )
            return False

        if self.is_deployed():
            info("BepInEx 已就绪")
            return True

        bep_dir = self.game_dir / "BepInEx"
        bep_dir.mkdir(parents=True, exist_ok=True)

        # 使用版本/架构矩阵确定正确的 BepInEx 变体
        variant = get_bepinex_variant(self.game_dir)
        if not variant:
            # get_bepinex_variant 可能因旧版 Unity 或 BCL 裁减返回 None。
            # 这些情况下不应强行部署（会导致崩溃或无法工作），而是告知用户走离线路线。
            blocked, _ = is_legacy_unity_runtime_injection_blocked(self.game_dir)
            is_stripped, _ = detect_bcl_stripping(self.game_dir)
            if blocked or is_stripped:
                warning("运行时注入不可用，BepInEx 部署已跳过。请使用离线资源汉化路线。")
                return False
            # 架构未知：简单回退猜测
            if self.arch == UnityArchitecture.mono:
                variant = f"mono_{self.bits}"
            elif self.arch == UnityArchitecture.il2cpp:
                variant = f"il2cpp_{self.bits}"
            else:
                if any(self.game_dir.glob("*_Data/Managed")):
                    variant = f"mono_{self.bits}"
                else:
                    variant = f"il2cpp_{self.bits}"

        source = BEPINEX_SOURCES.get(variant)
        if not source:
            warning(f"不支持的架构组合: {variant}")
            return False

        unity_ver_str = f"Unity {self.unity_version}" if self.unity_version != "unknown" else "Unity (版本未知)"
        info(f"部署 BepInEx: {variant} ({unity_ver_str})")

        # 检查是否存在可能冲突的 winhttp.dll
        self._check_doorstop_conflict()

        # 若 core 里残留的是不匹配的 BepInEx 版本（如 Unity6 上的 5.x），
        # 先备份并清理框架文件，避免新旧版本混杂。
        self._prepare_core_for_redeploy(variant)

        # 获取或下载 BepInEx
        zip_path = self._get_bepinex_zip(variant, source["url"])
        if not zip_path:
            return False

        # 解压到游戏目录
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.namelist():
                    parts = member.split("/")
                    # 去除 BepInEx 顶层目录前缀
                    known_roots = (
                        "BepInEx_win_x64", "BepInEx_win_x86",
                        "BepInEx_UnityIL2CPP_win_x64", "BepInEx_UnityIL2CPP_win_x86",
                        "BepInEx_IL2CPP_x64", "BepInEx_IL2CPP_x86",
                    )
                    if parts[0] in known_roots:
                        rel_path = "/".join(parts[1:])
                    else:
                        rel_path = member

                    if not rel_path:
                        continue

                    dest = self.game_dir / rel_path
                    if member.endswith("/"):
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)

            info("BepInEx 部署完成")

            # 写入 doorstop_config.ini
            self._write_doorstop_config()

            # 确保 Doorstop 代理 DLL 存在（本地副本可能缺失）
            self._ensure_doorstop_proxy()

            # 去重 Doorstop 代理：保留 version.dll，移除多余的 winhttp.dll，
            # 避免 zip 自带的 winhttp.dll 与既有 version.dll 形成双重注入。
            self._dedup_doorstop_proxy()

            # 校正 doorstop_config.ini 的入口指向，确保与本 variant 的 Preloader 一致。
            self._ensure_doorstop_target(variant)

            # Unity 6000 Mono: patch BepInEx.Unity.Mono.dll for UnityLogWriter compatibility
            source = BEPINEX_SOURCES.get(variant, {})
            if source.get("needs_patch"):
                self._apply_unity6000_mono_patch()

            # Unity6 Mono + BepInEx 5.x：补 GetPEKind（Preloader.dll / MonoMod.Utils.dll）
            if variant in ("mono_x64", "mono_x86") and self._is_unity6():
                self._apply_unity6_5x_getpekind_patch()

            # IL2CPP 模式：额外写入 BepInEx.cfg + 预生成 interop 程序集
            if self.arch == UnityArchitecture.il2cpp:
                self._write_il2cpp_config()
                self._update_il2cpp_cpp2il()

            return True
        except Exception as e:
            error(f"BepInEx 解压失败: {e}")
            return False

    # Unity 6000 精简 Mono 缺失若干标准 BCL 方法，BE 6 预加载链调用它们即崩溃。
    # 用预制的 patched DLL 覆盖（与 BEPINEX_SOURCES['mono6000_x64'] 的 be.755 构建对应）：
    #   BepInEx.Unity.Mono.dll     —— UnityLogWriter 的内部调用 (WriteStringToUnityLogImpl)
    #   BepInEx.Preloader.Core.dll —— PlatformUtils.SetPlatform 的 Module.GetPEKind (Unity6 无此方法)
    #   BepInEx.Core.dll           —— ConfigFile.Save 的 StreamWriter(string,bool,Encoding) 重载缺失
    # 补丁源放在 TOOLS_CACHE，由维护者用 Mono.Cecil 预生成（见 scripts/）。
    _MONO6000_PATCHES = {
        "BepInEx.Unity.Mono.dll": "BepInEx_UnityMono_patched.dll",
        "BepInEx.Preloader.Core.dll": "BepInEx.Preloader.Core.patched.dll",
        "BepInEx.Core.dll": "BepInEx.Core.patched.dll",
    }

    def _apply_unity6000_mono_patch(self):
        """应用 Unity 6000 Mono 兼容补丁（覆盖预制的 patched DLL）。"""
        core = self.game_dir / "BepInEx" / "core"
        applied: list[str] = []
        missing: list[str] = []
        for target, patched_name in self._MONO6000_PATCHES.items():
            dst = core / target
            if not dst.exists():
                continue  # 该 build 没有此 DLL，跳过
            src = next((d / patched_name for d in (TOOLS_CACHE, _PATCH_ASSET_DIR)
                        if (d / patched_name).exists()), None)
            if src:
                shutil.copy2(str(src), str(dst))
                applied.append(target)
            else:
                missing.append(patched_name)
        if applied:
            info(f"Unity 6000 Mono 兼容补丁已应用: {', '.join(applied)}")
        if missing:
            warning(
                f"缺少 Unity6 Mono 补丁文件: {', '.join(missing)}，"
                "BepInEx 可能无法在精简 Mono 运行时上启动"
            )

    def _is_unity6(self) -> bool:
        return parse_unity_year(self.unity_version) >= 6000

    # Unity6 精简 Mono 缺 Module.GetPEKind，BepInEx 5.x 的 Preloader 与 MonoMod.Utils
    # 在 PlatformHelper 里调用它会崩。用预制的、已 NOP 掉 GetPEKind 的 5.x DLL 覆盖。
    _UNITY6_5X_PATCHES = {
        "BepInEx.Preloader.dll": "BepInEx.Preloader.5x.patched.dll",
        "MonoMod.Utils.dll": "MonoMod.Utils.5x.patched.dll",
    }

    def _apply_unity6_5x_getpekind_patch(self):
        """对 Unity6 上部署的 BepInEx 5.x 应用 GetPEKind 补丁。"""
        core = self.game_dir / "BepInEx" / "core"
        applied: list[str] = []
        missing: list[str] = []
        for target, patched_name in self._UNITY6_5X_PATCHES.items():
            dst = core / target
            if not dst.exists():
                continue
            src = next((d / patched_name for d in (TOOLS_CACHE, _PATCH_ASSET_DIR)
                        if (d / patched_name).exists()), None)
            if src:
                shutil.copy2(str(src), str(dst))
                applied.append(target)
            else:
                missing.append(patched_name)
        if applied:
            info(f"Unity6 Mono GetPEKind 补丁已应用 (BepInEx 5.x): {', '.join(applied)}")
        if missing:
            warning(f"缺少 Unity6 5.x GetPEKind 补丁: {', '.join(missing)}，游戏可能崩在预加载阶段")

    def _prepare_core_for_redeploy(self, variant: str | None):
        """重新部署前清理 core 中不匹配的旧 BepInEx 框架文件。

        仅当目标 variant 的入口 Preloader DLL 不在 core 时才执行（说明当前是
        错误/过时版本）。先整目录备份一次，再只删除属于 BepInEx 框架的文件
        （按前缀），保留 XUnity.Common.dll 等放在 core 的第三方插件依赖。
        """
        core_dir = self.game_dir / "BepInEx" / "core"
        if not core_dir.is_dir():
            return
        entry = BEPINEX_PRELOADER_ENTRY.get(variant) if variant else None
        if not entry:
            return  # 未知 variant，保持宽松，不主动清理
        if (core_dir / entry).exists():
            return  # 已是目标版本，无需清理

        backup_dir = self.game_dir / "BepInEx" / "core.stale.bak"
        try:
            if not backup_dir.exists():
                shutil.copytree(core_dir, backup_dir)
                info(f"已备份旧 BepInEx core → {backup_dir}")
        except Exception as e:
            warning(f"备份旧 core 失败（继续）: {e}")

        removed = 0
        preserved: list[str] = []
        for f in list(core_dir.iterdir()):
            if not f.is_file():
                continue
            if f.name.startswith(_BEPINEX_FRAMEWORK_PREFIXES) and f.suffix.lower() in (".dll", ".xml"):
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    pass
            else:
                preserved.append(f.name)
        info(f"清理不匹配的 BepInEx core：删除 {removed} 个框架文件"
             + (f"，保留 {preserved}" if preserved else ""))

    def _dedup_doorstop_proxy(self):
        """存在 version.dll 时移除多余的 winhttp.dll，避免双重 doorstop 注入。

        Windows 将 winhttp.dll 列入 Known DLLs，本地副本通常不被加载；而
        version.dll 不受限。两者并存可能导致 BepInEx 初始化两次或互相冲突。
        """
        version_dll = self.game_dir / "version.dll"
        winhttp_dll = self.game_dir / "winhttp.dll"
        if version_dll.exists() and winhttp_dll.exists():
            try:
                winhttp_dll.unlink()
                info("已移除多余的 winhttp.dll（保留 version.dll 作为 doorstop 代理）")
            except Exception as e:
                warning(f"移除多余 winhttp.dll 失败: {e}")

    def _ensure_doorstop_target(self, variant: str | None):
        """确保 doorstop_config.ini 的 target_assembly 指向本 variant 的入口 Preloader。

        解压时 zip 自带的 doorstop_config.ini 通常已正确，但若游戏目录沿用了旧
        配置（如指向 5.x 的 BepInEx.Preloader.dll），这里依据 variant 校正。
        """
        entry = BEPINEX_PRELOADER_ENTRY.get(variant) if variant else None
        if not entry:
            return
        config_path = self.game_dir / "doorstop_config.ini"
        if not config_path.exists():
            return
        target = f"BepInEx\\core\\{entry}"
        try:
            text = config_path.read_text(encoding="utf-8")
        except Exception:
            return
        if re.search(r"(?im)^\s*target_assembly\s*=\s*" + re.escape(target) + r"\s*$", text):
            return  # 已正确
        new_text, n = re.subn(
            r"(?im)^(\s*target_assembly\s*=).*$",
            lambda m: m.group(1) + target,
            text,
        )
        if n > 0 and new_text != text:
            try:
                config_path.write_text(new_text, encoding="utf-8")
                info(f"doorstop_config.ini: target_assembly 已校正为 {target}")
            except Exception as e:
                warning(f"校正 doorstop target 失败: {e}")

    def _disable_doorstop_config(self):
        config_path = self.game_dir / "doorstop_config.ini"
        if not config_path.exists():
            return
        try:
            text = config_path.read_text(encoding="utf-8")
            updated = re.sub(r"(?im)^enabled\s*=\s*true\s*$", "enabled=false", text)
            if updated != text:
                config_path.write_text(updated, encoding="utf-8")
                info("doorstop_config.ini 已设为 enabled=false")
        except Exception as e:
            warning(f"无法禁用 doorstop_config.ini: {e}")

    def _update_il2cpp_cpp2il(self):
        """更新 IL2CPP BepInEx 的 Cpp2IL 组件至新版。

        BepInEx 6.0.0-pre.2 绑定的 LibCpp2IL/Cpp2IL.Core 过旧，
        只支持 metadata v23–29。Unity 6000+ 使用 v31，启动时 Cpp2IL
        初始化会直接报错退出。

        这里下载 Cpp2IL 2022.1.0-pre-release.21，将其中的 DLL 覆盖到
        BepInEx/core，使 BepInEx 的内置 Cpp2IL 运行步骤能正常完成。
        """
        core_dir = self.game_dir / "BepInEx" / "core"
        if not core_dir.is_dir():
            return

        # 如果已有 interop 目录且文件存在，说明已处理过，跳过
        interop_dir = self.game_dir / "BepInEx" / "interop"
        if interop_dir.is_dir() and any(interop_dir.glob("*.dll")):
            return

        # 仅当 GameAssembly.dll 存在时执行
        game_asm = self.game_dir / "GameAssembly.dll"
        if not game_asm.exists():
            return

        info("更新 Cpp2IL 组件（适配新版 IL2CPP metadata）...")

        cpp2il_zip = self._get_cpp2il_zip()
        if not cpp2il_zip:
            warning("无法获取新版 Cpp2IL，BepInEx 初始化可能失败")
            return

        try:
            with zipfile.ZipFile(cpp2il_zip, "r") as zf:
                for member in zf.namelist():
                    name = Path(member).name
                    if not name:
                        continue
                    if member.endswith("/"):
                        continue
                    # 仅替换/补充 DLL 文件
                    if not name.endswith(".dll"):
                        continue
                    dest = core_dir / name
                    with zf.open(member) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)

            count = len(list(core_dir.glob("*.dll")))
            info(f"Cpp2IL 组件更新完成（{count} 个 DLL 在 core 中）")
        except Exception as e:
            error(f"Cpp2IL 组件更新失败: {e}")

    def _get_cpp2il_zip(self) -> Path | None:
        """获取 Cpp2IL NetFramework 构建包。"""
        TOOLS_CACHE.mkdir(parents=True, exist_ok=True)
        cache_path = TOOLS_CACHE / "Cpp2IL-Windows-Netframework472.zip"

        if cache_path.exists() and cache_path.stat().st_size > 1_000_000:
            return cache_path

        cpp2il_url = (
            "https://github.com/SamboyCoding/Cpp2IL/releases/download/"
            "2022.1.0-pre-release.21/Cpp2IL-2022.1.0-pre-release.21-Windows-Netframework472.zip"
        )
        info("下载 Cpp2IL 组件包...")
        try:
            import urllib.request
            urllib.request.urlretrieve(cpp2il_url, str(cache_path))
            info("Cpp2IL 组件包下载完成")
            return cache_path
        except Exception as e:
            warning(f"Cpp2IL 下载失败: {e}")
            return None

    def _ensure_doorstop_proxy(self):
        """确保 Doorstop 代理 DLL 存在。

        Windows 10+ 将 winhttp.dll 列入 Known DLLs，系统会强制加载
        系统版本而忽略本地副本。因此优先使用 version.dll。
        """
        # version.dll 优先：不受 Known DLLs 限制
        proxy_priority = ["version.dll", "xinput9_1_0.dll", "winhttp.dll"]
        existing = [n for n in proxy_priority if (self.game_dir / n).exists()]
        if existing:
            # 如果已有的是 winhttp.dll 且没有 version.dll，升级为 version.dll
            if "winhttp.dll" in existing and "version.dll" not in existing:
                import shutil
                try:
                    shutil.move(str(self.game_dir / "winhttp.dll"),
                                str(self.game_dir / "version.dll"))
                    info("已将 winhttp.dll 重命名为 version.dll（避免 Windows Known DLLs 拦截）")
                except Exception:
                    pass
            return True

        info("Doorstop 代理 DLL 缺失，从官方源获取...")
        doorstop_url = (
            "https://github.com/BepInEx/BepInEx/releases/download/"
            "v5.4.23.2/BepInEx_win_x64_5.4.23.2.zip"
        )

        import tempfile
        tmp_zip = Path(tempfile.mktemp(suffix=".zip"))
        try:
            import urllib.request
            urllib.request.urlretrieve(doorstop_url, str(tmp_zip))
        except Exception as e:
            warning(f"Doorstop 下载失败: {e}")
            return False

        try:
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                for name in zf.namelist():
                    basename = name.rsplit("/", 1)[-1]
                    if basename in proxy_priority:
                        dest = self.game_dir / basename
                        with zf.open(name) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        info(f"已部署 Doorstop 代理: {basename}")

            # 如果有 winhttp.dll 但没有 version.dll，重命名
            if ((self.game_dir / "winhttp.dll").exists()
                    and not (self.game_dir / "version.dll").exists()):
                import shutil
                shutil.move(str(self.game_dir / "winhttp.dll"),
                            str(self.game_dir / "version.dll"))
                info("已将 winhttp.dll 重命名为 version.dll（避免 Windows Known DLLs 拦截）")
            return any((self.game_dir / n).exists() for n in proxy_priority)
        except Exception as e:
            warning(f"Doorstop 解压失败: {e}")
            return False

    def _check_doorstop_conflict(self):
        """检测游戏目录中 winhttp.dll 是否已被游戏自身依赖。"""
        game_winhttp = self.game_dir / "winhttp.dll"
        if game_winhttp.exists() and game_winhttp.stat().st_size > 500000:
            # 可能是游戏自身的 winhttp.dll（而非 Doorstop 代理），大小通常 > 500KB
            warning(f"检测到游戏自带 winhttp.dll ({game_winhttp.stat().st_size:,} bytes)")
            warning("Doorstop 可能覆盖系统 WinHTTP 导致游戏崩溃")
            warning("将使用 version.dll 作为代理 DLL 替代")
            # BepInEx 支持多种代理名，但 zip 内默认是 winhttp.dll
            # 部署后需要重命名
            self._use_alt_doorstop = True
        else:
            self._use_alt_doorstop = False

    def _write_doorstop_config(self):
        r"""写入 doorstop_config.ini。

        Mono (BepInEx 5.x)：target_assembly=BepInEx\core\BepInEx.Preloader.dll
        IL2CPP (BepInEx 6.x)：target_assembly=BepInEx\core\BepInEx.Unity.IL2CPP.dll
                              + [Il2Cpp] 段指定 CoreCLR 运行时路径
        """
        config_path = self.game_dir / "doorstop_config.ini"

        # 检查是否需要写入（已有正确配置就跳过）
        if config_path.exists():
            existing = config_path.read_text(encoding="utf-8")
            if self.arch == UnityArchitecture.il2cpp:
                if "[Il2Cpp]" in existing and "coreclr_path" in existing:
                    debug("doorstop_config.ini 已存在（IL2CPP 格式），跳过")
                    return
            else:
                if "[General]" in existing and "target_assembly" in existing:
                    debug("doorstop_config.ini 已存在（[General] 格式），跳过")
                    return

        if self.arch == UnityArchitecture.il2cpp:
            # IL2CPP 完整配置（参考「游戏一键汉化工具」验证方案）
            il2cpp_config = (
                "; doorstop_config.ini (IL2CPP) — 由 game-translator 自动生成\n"
                "; 参考 BepInEx 6.x + Doorstop 4.5.0 方案\n"
                "\n"
                "[General]\n"
                "enabled=true\n"
                "target_assembly=BepInEx\\core\\BepInEx.Unity.IL2CPP.dll\n"
                "redirect_output_log=false\n"
                "ignore_disable_switch=false\n"
                "\n"
                "[UnityMono]\n"
                "dll_search_path_override=\n"
                "debug_enabled=false\n"
                "debug_address=127.0.0.1:10000\n"
                "debug_suspend=false\n"
                "\n"
                "[Il2Cpp]\n"
                "coreclr_path=dotnet\\coreclr.dll\n"
                "corlib_dir=dotnet\n"
            )
            config_path.write_text(il2cpp_config, encoding="utf-8")
            info("doorstop_config.ini: 已写入 IL2CPP 格式（含 [Il2Cpp] CoreCLR 路径）")
        else:
            # Mono 配置
            mono_config = (
                "; doorstop_config.ini (Mono) — 由 game-translator 自动生成\n"
                "; 参考 BepInEx 5.x + Doorstop 4.4.1 方案\n"
                "\n"
                "[General]\n"
                "enabled=true\n"
                "target_assembly=BepInEx\\core\\BepInEx.Preloader.dll\n"
                "redirect_output_log=false\n"
                "ignore_disable_switch=false\n"
                "\n"
                "[UnityMono]\n"
                "dll_search_path_override=\n"
                "debug_enabled=false\n"
                "debug_address=127.0.0.1:10000\n"
                "debug_suspend=false\n"
            )
            config_path.write_text(mono_config, encoding="utf-8")
            info("doorstop_config.ini: 已写入 Mono 格式")

    def _write_il2cpp_config(self):
        """IL2CPP 模式：生成 BepInEx/config/BepInEx.cfg。

        这个文件对 IL2CPP 注入至关重要——缺少它，BepInEx 会静默失败：
        游戏正常启动但无任何插件加载，LogOutput.log 无错误。
        """
        cfg_dir = self.game_dir / "BepInEx" / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = cfg_dir / "BepInEx.cfg"

        cfg = (
            "[Preloader.Entrypoint]\n"
            "Assembly=UnityEngine.CoreModule.dll\n"
            "Type=UnityEngine.Application\n"
            "Method=.cctor\n"
        )

        if cfg_path.exists():
            existing = cfg_path.read_text(encoding="utf-8")
            if "Preloader.Entrypoint" in existing:
                return

        cfg_path.write_text(cfg, encoding="utf-8")
        info("BepInEx.cfg (IL2CPP) 已写入")

    def _get_bepinex_zip(self, variant: str, url: str) -> Path | None:
        """获取 BepInEx zip 文件。

        优先级：打包数据 > 本地缓存 > 参考工具打包 > 远程下载
        """
        TOOLS_CACHE.mkdir(parents=True, exist_ok=True)
        cache_name = f"bepinex_{variant}.zip"
        cache_path = TOOLS_CACHE / cache_name

        # 0. PyInstaller 打包数据（优先）
        if _BUNDLED_DATA:
            bundled = _BUNDLED_DATA / cache_name
            if bundled.exists() and bundled.stat().st_size > 100000:
                info(f"使用打包的 BepInEx: {bundled}")
                return bundled

        # 1. 本地缓存
        if cache_path.exists() and cache_path.stat().st_size > 100000:
            info(f"使用缓存的 BepInEx: {cache_path}")
            return cache_path

        # 2. 从参考工具打包（IL2CPP 变体）
        ref_variant_map = {
            "il2cpp_x64": _REF_TOOL_IL2CPP_X64,
            "il2cpp_x86": _REF_TOOL_IL2CPP_X86,
            "mono_x64": _REF_TOOL_MONO_X64,
            "mono_x86": _REF_TOOL_MONO_X86,
        }
        ref_dir = ref_variant_map.get(variant)
        if ref_dir and ref_dir.is_dir():
            info(f"从参考工具打包 BepInEx ({variant})...")
            try:
                with zipfile.ZipFile(str(cache_path), "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in ref_dir.rglob("*"):
                        if f.is_file():
                            arcname = str(f.relative_to(ref_dir)).replace("\\", "/")
                            zf.write(str(f), arcname)
                info(f"BepInEx 已打包: {cache_path} ({cache_path.stat().st_size:,} bytes)")
                return cache_path
            except Exception as e:
                warning(f"从参考工具打包失败: {e}")

        # 3. 远程下载（仅当 url 是 http/https 地址）
        if url.startswith("http://") or url.startswith("https://"):
            info(f"下载 BepInEx ({variant})...")
            try:
                import urllib.request
                urllib.request.urlretrieve(url, str(cache_path))
                info(f"BepInEx 下载完成: {cache_path}")
                return cache_path
            except Exception as e:
                warning(f"BepInEx 下载失败: {e}")
                warning(f"下载地址: {url}")
        else:
            warning(
                f"BepInEx ({variant}) 未缓存且无法自动获取。\n"
                f"请将参考工具 {ref_dir or variant} 目录放置到位，或手动下载到:\n"
                f"  {cache_path}"
            )

        return None

# ---------------------------------------------------------------------------
# XUnity.AutoTranslator 插件部署
# ---------------------------------------------------------------------------

class XUnityPluginDeployer:
    """XUnity.AutoTranslator 插件部署。"""

    def __init__(self, game_dir: Path, arch: str = "mono"):
        self.game_dir = game_dir
        self.arch = arch  # "mono" 或 "il2cpp"

    def is_deployed(self) -> bool:
        """仅在核心插件和当前架构桥接 DLL 都存在时返回 True。"""
        return not self._missing_required_files(self.game_dir)

    def deploy(self) -> bool:
        """事务化安装 XUnity；失败时保留原有可用安装。"""
        if self.is_deployed():
            info(f"XUnity.AutoTranslator 已就绪 ({self.arch})")
            return True

        info(f"部署 XUnity.AutoTranslator ({self.arch})...")
        zip_path = self._get_xunity_zip()
        if not zip_path:
            return False

        work_dir = Path(tempfile.mkdtemp(prefix=".engaixt-xunity-", dir=self.game_dir))
        staged_root = work_dir / "staged"
        backup_root = work_dir / "backup"
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.namelist():
                    if "IPA" in member or "Managed" in member:
                        continue
                    if member.endswith("/"):
                        continue

                    normalized = member.replace("\\", "/")
                    if not normalized.startswith("BepInEx/"):
                        continue
                    relative = Path(*normalized.split("/"))
                    if ".." in relative.parts:
                        raise ValueError(f"XUnity 压缩包包含不安全路径: {member}")
                    dest = staged_root / relative
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)

            missing = self._missing_required_files(staged_root)
            if missing:
                raise ValueError("XUnity 压缩包不完整: " + ", ".join(map(str, missing)))

            moved_existing: list[tuple[Path, Path]] = []
            installed: list[Path] = []
            try:
                for staged_path, target_path, relative in self._package_targets(staged_root):
                    backup_path = backup_root / relative
                    if target_path.exists():
                        backup_path.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(target_path, backup_path)
                        moved_existing.append((backup_path, target_path))

                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged_path, target_path)
                    installed.append(target_path)

                missing = self._missing_required_files(self.game_dir)
                if missing:
                    raise ValueError(
                        "XUnity 安装后校验失败: " + ", ".join(map(str, missing))
                    )
            except Exception:
                for target_path in reversed(installed):
                    if target_path.is_dir():
                        shutil.rmtree(target_path, ignore_errors=True)
                    else:
                        target_path.unlink(missing_ok=True)
                for backup_path, target_path in reversed(moved_existing):
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup_path, target_path)
                raise

            info("XUnity.AutoTranslator 部署完成并通过完整性校验")
            return True
        except Exception as e:
            error(f"XUnity.AutoTranslator 部署失败，原安装未被提前清理: {e}")
            return False
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _required_relative_paths(self) -> tuple[Path, ...]:
        bridge = (
            "XUnity.AutoTranslator.Plugin.BepInEx-IL2CPP.dll"
            if self.arch == "il2cpp"
            else "XUnity.AutoTranslator.Plugin.BepInEx.dll"
        )
        return (
            Path("BepInEx/core/XUnity.Common.dll"),
            Path("BepInEx/plugins/XUnity.AutoTranslator/XUnity.AutoTranslator.Plugin.Core.dll"),
            Path("BepInEx/plugins/XUnity.AutoTranslator") / bridge,
        )

    def _missing_required_files(self, root: Path) -> list[Path]:
        return [path for path in self._required_relative_paths() if not (root / path).is_file()]

    def _package_targets(self, staged_root: Path) -> list[tuple[Path, Path, Path]]:
        relative_targets = (
            Path("BepInEx/core/XUnity.Common.dll"),
            Path("BepInEx/plugins/XUnity.AutoTranslator"),
            Path("BepInEx/plugins/XUnity.ResourceRedirector"),
            Path("BepInEx/plugins/README (AutoTranslator).md"),
        )
        return [
            (staged_root / relative, self.game_dir / relative, relative)
            for relative in relative_targets
            if (staged_root / relative).exists()
        ]

    def _get_xunity_zip(self) -> Path | None:
        """获取 XUnity.AutoTranslator zip（按架构选择正确版本）。

        Mono:   本地 5.6.1（参考工具 trans/mono_x64 或 trans/mono_x86）
        IL2CPP: 从参考工具 trans/x64 或 GitHub 下载已验证的 5.6.1
        缓存缺失时自动从参考工具打包，无需联网。
        """
        TOOLS_CACHE.mkdir(parents=True, exist_ok=True)
        download_spec = XUNITY_DOWNLOAD_URLS.get(self.arch, XUNITY_DOWNLOAD_URLS["mono"])

        # Mono: download_spec 是本地文件名（XUnity.AutoTranslator-mono.zip）
        if self.arch == "mono":
            cache_path = TOOLS_CACHE / download_spec
            # PyInstaller 打包数据优先
            if _BUNDLED_DATA:
                bundled = _BUNDLED_DATA / download_spec
                if bundled.exists() and bundled.stat().st_size > 100000:
                    return bundled
            if cache_path.exists() and cache_path.stat().st_size > 100000:
                return cache_path

            # 从参考工具 mono_x64 或 mono_x86 打包（XUAT 插件是 AnyCPU 的）
            ref_dir = _REF_TOOL_MONO_X64 if _REF_TOOL_MONO_X64.is_dir() else _REF_TOOL_MONO_X86
            if ref_dir.is_dir():
                info(f"从参考工具打包 XUnity.AutoTranslator 5.6.1 ({ref_dir.name})...")
                if self._build_xunity_zip_from_ref(cache_path, ref_dir):
                    return cache_path

            warning(
                "XUnity.AutoTranslator 5.6.1 未找到，且参考工具不可用。\n"
                "请将参考工具 trans 目录放置到:\n"
                f"  {_REF_TOOL_TRANS}\n"
                f"或手动打包 XUAT 到: {cache_path}"
            )
            return None

        # IL2CPP: 使用版本化缓存，避免旧版 5.4.1 被误当成当前包。
        cache_path = TOOLS_CACHE / "XUnity.AutoTranslator-il2cpp-5.6.1.zip"
        if cache_path.exists() and cache_path.stat().st_size > 100000:
            return cache_path

        # 尝试从参考工具打包 IL2CPP 版 XUAT（与 Mono 版插件相同，但来自验证过的源）
        ref_il2cpp = _REF_TOOL_IL2CPP_X64 if _REF_TOOL_IL2CPP_X64.is_dir() else _REF_TOOL_IL2CPP_X86
        if ref_il2cpp.is_dir():
            info(f"从参考工具打包 XUnity.AutoTranslator IL2CPP ({ref_il2cpp.name})...")
            if self._build_xunity_zip_from_ref(cache_path, ref_il2cpp):
                return cache_path

        # 回退：从 GitHub 下载
        info(f"下载 XUnity.AutoTranslator ({self.arch})...")
        try:
            import urllib.request
            urllib.request.urlretrieve(download_spec, str(cache_path))
            info("XUnity.AutoTranslator 下载完成")
            return cache_path
        except Exception as e:
            warning(f"下载失败: {e}")
            warning(f"请手动下载 XUnity.AutoTranslator ({self.arch}) 并放置到: {cache_path}")
            warning(f"下载地址: {download_spec}")
            return None

    def _build_xunity_zip_from_ref(self, cache_path: Path, ref_dir: Path | None = None) -> bool:
        """从参考工具 trans/<variant> 打包 XUAT zip。

        zip 内容：BepInEx/core/XUnity.Common.dll + BepInEx/plugins/*/...
        """
        if ref_dir is None:
            ref_dir = _REF_TOOL_MONO_X64
        core_src = ref_dir / "BepInEx" / "core" / "XUnity.Common.dll"
        plugins_src = ref_dir / "BepInEx" / "plugins"

        if not core_src.exists():
            warning(f"参考工具缺少 XUnity.Common.dll: {core_src}")
            return False
        if not plugins_src.is_dir():
            warning(f"参考工具缺少 plugins 目录: {plugins_src}")
            return False

        try:
            with zipfile.ZipFile(str(cache_path), "w", zipfile.ZIP_DEFLATED) as zf:
                # core/XUnity.Common.dll
                zf.write(str(core_src), "BepInEx/core/XUnity.Common.dll")
                # plugins/ (递归)
                for f in plugins_src.rglob("*"):
                    if f.is_file():
                        arcname = "BepInEx/plugins/" + str(f.relative_to(plugins_src)).replace("\\", "/")
                        zf.write(str(f), arcname)
            info(f"XUAT 已打包: {cache_path} ({cache_path.stat().st_size:,} bytes)")
            return True
        except Exception as e:
            warning(f"打包 XUAT 失败: {e}")
            return False


# ---------------------------------------------------------------------------
# 字体部署器
# ---------------------------------------------------------------------------


from engines.xunity.fonts import FontDeployer
from engines.xunity.overlay_guard import UGUIOverlayGuardDeployer
from engines.xunity.translation_cache import TranslationCacheGenerator
