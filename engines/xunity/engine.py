"""XUnityRealtimeEngine 引擎类：BepInEx + XUAT 运行时注入实时翻译。"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

from engines.base import EngineBase, EngineCapabilities, TextItem
from utils.logger import info, warning
from utils.lang_detect import detect_batch_language
from utils.translation_server import start_server

from engines.xunity.config_gen import (
    generate_language_override_config,
    generate_xunity_config,
)
from engines.xunity.detect import (
    UnityArchitecture,
    detect_unity_arch,
    detect_unity_bits,
    detect_unity_version,
    is_legacy_unity_runtime_injection_blocked,
    parse_unity_year,
)
from engines.xunity.diagnostics import (
    check_antivirus,
    check_path_encoding,
    detect_custom_assembly_resolver,
)
from engines.xunity.extract import UnityResourceScanner
from engines.xunity.runtime import (
    BepInExDeployer,
    FontDeployer,
    TranslationCacheGenerator,
    UGUIOverlayGuardDeployer,
    XUnityPluginDeployer,
)


# ---------------------------------------------------------------------------
# 主引擎
# ---------------------------------------------------------------------------

class XUnityRealtimeEngine(EngineBase):
    """通用 Unity 运行时翻译引擎。

    不修改游戏原始资源文件。通过 BepInEx 注入运行时，
    XUnity.AutoTranslator hook 文本渲染管线，在显示前替换为中文。

    流程：
    1. detect  — 识别 Unity 游戏（Mono / IL2CPP）
    2. unpack  — 扫描资源提取文本 + 生成翻译
    3. repack  — 部署 BepInEx + XUnity + 字体 + 配置 + 缓存
    4. 启动游戏 — 运行时自动注入翻译
    """

    name = "xunity_realtime"
    label = "Unity 运行时注入 (XUnity.AutoTranslator)"
    support_level = "stable"
    capabilities = EngineCapabilities(
        extract=True,
        repack=True,
        static_patch=False,
        runtime_patch=True,
        creates_launcher=False,
        portable_after_patch=True,
        requires_python=False,
        requires_frida=False,
        notes=(
            "部署 BepInEx 与 XUnity.AutoTranslator，通过显示层替换文本。",
            "不直接修改 Unity 原始资源，适合 Mono/IL2CPP 通用运行时翻译。",
        ),
    )
    detect_priority = 88

    def detect(self, path: Path) -> bool:
        """检测是否为 Unity 游戏。"""
        game_dir = path if path.is_dir() else path.parent

        # 检查标准 Unity 特征
        data_dirs = list(game_dir.glob("*_Data"))
        if data_dirs:
            for dd in data_dirs:
                if (dd / "globalgamemanagers").exists():
                    return True
                if (dd / "resources.assets").exists():
                    return True
                if (dd / "Managed").is_dir():
                    return True

        # IL2CPP 特征
        if (game_dir / "GameAssembly.dll").exists():
            return True

        # 子目录
        for sub in ["contents", "game", "data"]:
            d = game_dir / sub
            if d.is_dir() and self.detect(d):
                return True

        return False

    def detect_confidence(self, path: Path) -> tuple[int, list[str]]:
        game_dir = path if path.is_dir() else path.parent
        evidence = []
        score = 0

        data_dirs = list(game_dir.glob("*_Data"))
        if data_dirs:
            evidence.append(f"找到 {len(data_dirs)} 个 *_Data 目录")
            score = max(score, 86)
            for dd in data_dirs:
                if (dd / "globalgamemanagers").exists():
                    evidence.append(f"{dd.name}/globalgamemanagers")
                    score = max(score, 91)
                if (dd / "resources.assets").exists():
                    evidence.append(f"{dd.name}/resources.assets")
                    score = max(score, 90)
                if (dd / "Managed").is_dir():
                    evidence.append(f"{dd.name}/Managed")
                    score = max(score, 92)

        if (game_dir / "GameAssembly.dll").exists():
            evidence.append("找到 GameAssembly.dll (IL2CPP)")
            score = max(score, 92)
        if (game_dir / "UnityPlayer.dll").exists() or (game_dir / "UnityPlayer_.dll").exists():
            evidence.append("找到 UnityPlayer.dll")
            score = max(score, 88)

        for sub in ["contents", "game", "data"]:
            d = game_dir / sub
            if d.is_dir():
                sub_score, sub_evidence = self.detect_confidence(d)
                if sub_score:
                    return max(score, sub_score - 2), [f"{sub}/ 内匹配"] + sub_evidence

        return score, evidence

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        """扫描游戏资源，提取可翻译文本。"""
        game_dir = path if path.is_dir() else path.parent
        self._game_dir = game_dir

        info(f"扫描 Unity 游戏资源: {game_dir}")

        # 架构检测
        arch = detect_unity_arch(game_dir)
        bits = detect_unity_bits(game_dir)
        info(f"Unity 架构: {arch} ({bits})")

        # 资源扫描
        scanner = UnityResourceScanner(game_dir)
        items = scanner.scan()

        if not items:
            warning("未提取到可翻译文本")
            # 仍然返回空列表，repack 阶段会部署运行时环境

        return items

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        """部署运行时翻译环境。

        1. 部署 BepInEx（不修改原始 DLL）
        2. 安装 XUnity.AutoTranslator 插件
        3. 部署中文字体 + TMP/UGUI fallback
        4. 生成翻译缓存（本地优先命中）
        5. 启动 AI 翻译代理服务器（在线回退）
        6. 写入配置文件
        """
        game_dir = getattr(self, "_game_dir", None)
        if game_dir is None:
            game_dir = workspace.parent

        # 源语言：优先从 engine 存储的属性读取，否则从 items 检测
        from_lang = getattr(self, "_source_lang", None)
        if not from_lang and items:
            texts = [it.original for it in items[:50]]
            from_lang = detect_batch_language(texts) if texts else "en"
        if not from_lang:
            from_lang = "en"

        # 从全局配置获取翻译器和目标语言
        from config import get_config
        config_cfg = get_config()
        target_lang = config_cfg.target_lang if config_cfg else "zh-CN"
        # 映射 target_lang 到 XUAT 格式
        xuat_target = "zh" if target_lang.startswith("zh") else target_lang
        provider = config_cfg.active_translator if config_cfg else "deepseek"

        info("=" * 50)
        info("部署 Unity 运行时翻译环境")
        info("=" * 50)

        # ---- Step 0: 预检（路径、杀软、语言锁、Assembly Resolver） ----
        path_warnings = check_path_encoding(game_dir)
        for w in path_warnings:
            warning(w)

        av_guidance = check_antivirus()
        if av_guidance:
            info("")
            for line in av_guidance:
                info(f"  {line}")

        # 多语言锁检测
        lang_lock_guidance = generate_language_override_config(game_dir)
        if lang_lock_guidance:
            info("")
            for line in lang_lock_guidance.split("\n"):
                warning(line) if "建议" in line else info(f"  {line}")

        # 自定义 Assembly Resolver 检测
        has_resolver, resolver_guidance = detect_custom_assembly_resolver(game_dir)
        if has_resolver and resolver_guidance:
            info("")
            for line in resolver_guidance.split("\n"):
                warning(line)

        # ---- Step 1: BepInEx ----
        info("\n[1/6] 部署 BepInEx 运行时...")
        bepinex = BepInExDeployer(game_dir)
        if not bepinex.deploy():
            blocked, version = is_legacy_unity_runtime_injection_blocked(game_dir)
            if blocked:
                warning(
                    f"Unity {version} 已跳过运行时注入；为了避免无法点击，"
                    "不会继续安装 XUnity/BepInEx 插件。"
                )
                translated_items = [it for it in items if it.translated and it.translated != it.original]
                if translated_items:
                    TranslationCacheGenerator(game_dir).generate(translated_items)
                return
            warning("BepInEx 部署失败，后续步骤可能无法正常工作")
            return

        # ---- Step 2: XUnity.AutoTranslator ----
        info("\n[2/6] 安装 XUnity.AutoTranslator 插件...")
        xunity_arch = "il2cpp" if bepinex.arch == UnityArchitecture.il2cpp else "mono"
        xunity_plugin = XUnityPluginDeployer(game_dir, arch=xunity_arch)
        if not xunity_plugin.deploy():
            raise RuntimeError("XUnity.AutoTranslator 部署失败或安装不完整，已中止游戏启动")

        # ---- Step 3: UGUI overlay guard ----
        info("\n[3/7] 部署 UGUI 覆盖层防卡句辅助插件...")
        UGUIOverlayGuardDeployer(game_dir).deploy()

        # ---- Step 4: 字体 ----
        info("\n[4/7] 部署中文字体...")
        font = FontDeployer(game_dir)
        deployed = font.deploy()
        use_engaixt_tmp_fallback = False
        if xunity_arch == "il2cpp" and parse_unity_year(detect_unity_version(game_dir)) >= 6000:
            use_engaixt_tmp_fallback = font.deploy_tmp_font_bundle()
        if deployed:
            if use_engaixt_tmp_fallback:
                info("Unity 6000 TMP will use the EngAixt asynchronous CJK fallback plugin")
            else:
                info("TMP 字体 fallback 将使用 Microsoft YaHei（需要 TextMeshPro 3.2+）")
        else:
            warning("中文字体未部署，中文可能无法正常显示")

        # ---- Step 5: 翻译缓存 ----
        info("\n[5/7] 生成翻译缓存...")
        translated_items = [it for it in items if it.translated and it.translated != it.original]
        cache_gen = TranslationCacheGenerator(game_dir)
        # 先从服务器持久化缓存同步历史翻译（免 token，消灭二次加载延迟）
        synced = cache_gen.sync_from_server_cache()
        if synced > 0:
            info(f"从服务器缓存恢复 {synced} 条历史翻译到本地缓存")
        if translated_items:
            cache_gen.generate(items)
        # 写入内置词典作为种子缓存（零 token）
        builtin_count = cache_gen.seed_builtin_dict()
        if builtin_count > 0:
            info(f"种子缓存（内置词典零 token）: {builtin_count} 条常用词汇已写入")
        if not translated_items and builtin_count == 0 and synced == 0:
            info("没有预翻译文本，运行时将全部通过在线代理翻译")

        # ---- Step 6: 启动 AI 翻译代理 ----
        info("\n[6/7] 启动 AI 翻译代理服务器...")
        proxy_port = 5120
        if self.ensure_translation_proxy(provider=provider, port=proxy_port):
            info(f"翻译代理已启动: http://127.0.0.1:{proxy_port}/translate ({provider})")
            info("XUAT 运行时未命中缓存时将自动请求此代理")
        else:
            raise RuntimeError(
                f"Unity 实时翻译代理未能监听 127.0.0.1:{proxy_port}，已中止游戏启动"
            )

        # ---- Step 7: 配置 ----
        info("\n[7/7] 生成配置文件...")
        generate_xunity_config(game_dir, target_lang=xuat_target,
                               from_lang=from_lang, proxy_port=proxy_port,
                               use_engaixt_tmp_fallback=use_engaixt_tmp_fallback)

        info("\n" + "=" * 50)
        info("部署完成！启动游戏即可自动加载翻译。")
        info(f"  - 源语言: {from_lang} → 目标语言: {target_lang}")
        info(f"  - 翻译代理: http://127.0.0.1:{proxy_port}/translate ({provider})")
        info(f"  - BepInEx: {game_dir / 'BepInEx'}")
        info(f"  - 配置: {game_dir / 'BepInEx' / 'config' / 'AutoTranslatorConfig.ini'}")
        info(f"  - 翻译缓存: {game_dir / 'BepInEx' / 'Translation'}")
        info(f"  - 字体: {game_dir / 'BepInEx' / 'Translation' / 'zh' / 'Font'}")
        info("=" * 50)

    def ensure_translation_proxy(self, provider: str | None = None, port: int = 5120) -> bool:
        """Ensure the in-process XUnity translation proxy is actually listening."""
        provider = provider or getattr(self, "_translation_provider", "deepseek")
        self._translation_provider = provider

        proxy = getattr(self, "_translation_proxy", None)
        if proxy is not None and self._proxy_is_listening(port):
            self._record_runtime_event("proxy_ready", port=port, provider=provider)
            return True

        if proxy is not None:
            try:
                proxy.stop()
            except Exception:
                pass
            self._translation_proxy = None

        self._record_runtime_event("proxy_start_requested", port=port, provider=provider)
        self._translation_proxy = start_server(port=port, provider=provider)
        if self._translation_proxy is None:
            self._record_runtime_event("proxy_start_failed", port=port, provider=provider)
            return False

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self._proxy_is_listening(port):
                self._record_runtime_event("proxy_ready", port=port, provider=provider)
                return True
            time.sleep(0.05)

        try:
            self._translation_proxy.stop()
        except Exception:
            pass
        self._translation_proxy = None
        self._record_runtime_event("proxy_healthcheck_failed", port=port, provider=provider)
        return False

    @staticmethod
    def _proxy_is_listening(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            return False

    def _record_runtime_event(self, event: str, **details: object) -> None:
        game_dir = getattr(self, "_game_dir", None)
        if not game_dir:
            return
        try:
            meta_dir = Path(game_dir) / "_translation_meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "event": event,
                "pid": os.getpid(),
                **details,
            }
            with (meta_dir / "xunity_runtime.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def find_exe(self, path: Path) -> Path | None:
        """找到游戏主可执行文件。"""
        game_dir = path if path.is_dir() else path.parent
        from core.exe_selector import find_main_exe

        return find_main_exe(game_dir, recursive=False)

    def generate_config_only(self, game_dir: Path) -> bool:
        """仅生成/更新 XUnity 配置文件（用于首次安装后微调）。"""
        self._game_dir = game_dir
        from_lang = getattr(self, "_source_lang", "en")
        from config import get_config
        config_cfg = get_config()
        target_lang = config_cfg.target_lang if config_cfg else "zh-CN"
        xuat_target = "zh" if target_lang.startswith("zh") else target_lang
        generate_xunity_config(game_dir, target_lang=xuat_target, from_lang=from_lang)
        return True
