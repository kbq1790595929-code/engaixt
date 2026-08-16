from __future__ import annotations

import shutil
from pathlib import Path

from core.resources import resource_path
from engines.xunity.constants import FONT_DOWNLOAD_URL, SYSTEM_FONT_CANDIDATES, TOOLS_CACHE
from utils.logger import debug, info, warning

class FontDeployer:
    """中文字体自动部署。"""

    FONT_DIR_NAME = "Translation/zh/Font"
    FONT_FILE_NAME = "zh_font.ttf"

    def __init__(self, game_dir: Path):
        self.game_dir = game_dir
        self.font_dir = game_dir / "BepInEx" / self.FONT_DIR_NAME

    def is_deployed(self) -> bool:
        return self.font_dir.is_dir() and any(self.font_dir.glob("*.ttf"))

    def deploy(self) -> bool:
        """部署中文字体。"""
        if self.is_deployed():
            return True

        self.font_dir.mkdir(parents=True, exist_ok=True)

        # 优先部署随工具分发/缓存的开源字体，避免复制 Windows 专有字体。
        try:
            from core.open_source_fonts import deploy_source_han_sans
            if deploy_source_han_sans(self.font_dir / self.FONT_FILE_NAME):
                info("开源中文字体已部署: Source Han Sans CN Regular")
                return True
        except Exception as e:
            warning(f"开源中文字体部署失败: {e}")

        # 尝试从用户安装的开源系统字体复制
        system_font = self._find_system_font()
        if system_font:
            dest = self.font_dir / self.FONT_FILE_NAME
            if system_font.suffix.lower() == ".ttc":
                dest = self.font_dir / "zh_font.ttc"
            shutil.copy2(system_font, dest)
            info(f"字体已部署: {system_font.name} → {dest.name}")
            return True

        # 尝试从工具缓存复制
        cached = self._get_cached_font()
        if cached:
            shutil.copy2(cached, self.font_dir / self.FONT_FILE_NAME)
            info(f"字体从缓存部署")
            return True

        # 尝试下载
        info("下载思源黑体...")
        try:
            import urllib.request
            TOOLS_CACHE.mkdir(parents=True, exist_ok=True)
            cache_dest = TOOLS_CACHE / self.FONT_FILE_NAME
            urllib.request.urlretrieve(FONT_DOWNLOAD_URL, str(cache_dest))
            shutil.copy2(cache_dest, self.font_dir / self.FONT_FILE_NAME)
            info("思源黑体下载完成")
            return True
        except Exception as e:
            warning(f"字体下载失败: {e}")

        return False

    # Unity 6000 IL2CPP loads this bundle through the companion plugin's
    # asynchronous AssetBundle path, not XUnity's synchronous loader.
    _TMP_FONT_BUNDLE = "NotoSansSC_sdf32_optimized_12k_lz4_2020"
    _TMP_FALLBACK_PLUGIN = "EngAixt.XUnityTmpFontFallback.dll"

    def deploy_tmp_font_bundle(self) -> bool:
        """Deploy the Unity 6000 IL2CPP TMP fallback runtime and font asset."""
        font_src = resource_path("assets", "xunity_fonts", self._TMP_FONT_BUNDLE)
        plugin_src = resource_path("assets", "xunity_plugins", self._TMP_FALLBACK_PLUGIN)
        if not font_src.exists() or not plugin_src.exists():
            warning("Unity 6000 TMP fallback assets are unavailable; skipping TMP font deployment")
            return False

        font_dst = self.game_dir / self._TMP_FONT_BUNDLE
        if not font_dst.exists() or font_dst.stat().st_size != font_src.stat().st_size:
            shutil.copy2(font_src, font_dst)
            info(f"Unity 6000 TMP font asset deployed: {font_dst.name}")

        plugin_dst = self.game_dir / "BepInEx" / "plugins" / self._TMP_FALLBACK_PLUGIN
        plugin_dst.parent.mkdir(parents=True, exist_ok=True)
        if not plugin_dst.exists() or plugin_dst.stat().st_size != plugin_src.stat().st_size:
            shutil.copy2(plugin_src, plugin_dst)
            info(f"Unity 6000 TMP fallback plugin deployed: {plugin_dst.name}")

        return True

    def _find_system_font(self) -> Path | None:
        """查找系统中的中文字体。"""
        font_dirs = [
            Path("C:/Windows/Fonts"),
            Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
        ]
        for fd in font_dirs:
            if fd.is_dir():
                for name in SYSTEM_FONT_CANDIDATES:
                    f = fd / name
                    if f.exists() and f.stat().st_size > 500000:
                        return f
        return None

    def _get_cached_font(self) -> Path | None:
        for cache in (
            TOOLS_CACHE / self.FONT_FILE_NAME,
            TOOLS_CACHE / "SourceHanSansCN-Regular.otf",
            Path.home() / "Downloads" / ".game_translator" / "cjk_fonts" / "source_han_sans_cn" / "SourceHanSansCN-Regular.otf",
        ):
            if cache.exists() and cache.stat().st_size > 500000:
                return cache
        return None

    def deploy_sdf_atlas(self, texts: list[str]) -> bool:
        """部署 SDF Atlas 用于 TMP 字体渲染。

        收集译文中的中文字符 → 生成字符集 → 调用 msdf-atlas-gen → 输出 Atlas。
        """
        try:
            from utils.font_toolkit import collect_charset, write_charset_file, generate_sdf_atlas
        except ImportError:
            debug("font_toolkit 不可用，跳过 SDF Atlas 生成")
            return False

        if not self.is_deployed():
            return False

        # 收集字符集
        charset = collect_charset(texts, include_gb2312=True)
        if not charset:
            return False

        charset_dir = self.font_dir / "charset"
        charset_dir.mkdir(parents=True, exist_ok=True)
        charset_path = write_charset_file(charset, charset_dir / "charset.txt")

        # 找到已部署的字体文件
        font_path = (
            next(self.font_dir.glob("*.ttf"), None)
            or next(self.font_dir.glob("*.otf"), None)
            or next(self.font_dir.glob("*.ttc"), None)
        )
        if not font_path:
            return False

        # 确定 Atlas 尺寸
        charset_size = len(charset)
        atlas_size = 4096 if charset_size > 2000 else 2048

        # 生成 SDF Atlas
        atlas_dir = self.font_dir / "atlas"
        atlas_png = generate_sdf_atlas(
            font_path=font_path,
            charset_path=charset_path,
            output_dir=atlas_dir,
            atlas_size=atlas_size,
        )

        if atlas_png:
            info(f"TMP SDF Atlas 已部署: {atlas_dir}")
            return True
        return False

    def write_tmp_fallback_config(self, fallback_chain: list[str] | None = None):
        """写入 TMP 字体回退配置，包含环检测。

        Args:
            fallback_chain: Fallback 字体列表，如 ["zh_font.ttf", "SourceHanSansCN-Regular.otf"]
        """
        try:
            from utils.font_toolkit import validate_fallback_chain
        except ImportError:
            validate_fallback_chain = None

        config_path = self.game_dir / "BepInEx" / "config" / "TMP_FontFallback.txt"
        config_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.is_deployed():
            return

        font_path = (
            next(self.font_dir.glob("*.ttf"), None)
            or next(self.font_dir.glob("*.otf"), None)
            or next(self.font_dir.glob("*.ttc"), None)
        )
        if not font_path:
            return

        # 构建 Fallback 链
        if fallback_chain is None:
            fallback_chain = [str(font_path.resolve())]

        # 环检测
        if validate_fallback_chain:
            valid, err_msg = validate_fallback_chain(fallback_chain)
            if not valid:
                warning(f"TMP Fallback 链配置无效: {err_msg}")
                warning("已拒绝写入，防止游戏运行时 StackOverflow 崩溃")
                return

        config_text = "\n".join(fallback_chain)
        config_path.write_text(config_text, encoding="utf-8")
        debug(f"TMP fallback 配置已写入 ({len(fallback_chain)} 级链)")

    def get_deployed_font_path(self) -> Path | None:
        """获取已部署字体文件的路径。"""
        if not self.is_deployed():
            return None
        return (
            next(self.font_dir.glob("*.ttf"), None)
            or next(self.font_dir.glob("*.otf"), None)
            or next(self.font_dir.glob("*.ttc"), None)
        )


# ---------------------------------------------------------------------------
