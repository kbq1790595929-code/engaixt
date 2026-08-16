"""通用 Unity 运行时翻译引擎 — BepInEx + XUnity.AutoTranslator。

核心思路：不修改游戏原始资源，通过运行时注入接管文本显示。
- 自动检测 Mono / IL2CPP 架构
- 自动部署 BepInEx 运行时（不修改原始 DLL）
- 自动安装 XUnity.AutoTranslator 插件
- 自动部署中文字体 + TMP/UGUI fallback 配置
- UnityPy 遍历资源提取可翻译文本
- 兼容模式处理轻度混淆（xor、头偏移、简单压缩）
- 生成离线翻译缓存，无需联网
- 运行时自动 hook UGUI / TMP / Lua 文本层动态替换
"""

from engines.base import registry

from engines.xunity.config_gen import (
    generate_language_override_config,
    generate_xunity_config,
)
from engines.xunity.constants import (
    BEPINEX_PRELOADER_ENTRY,
    BEPINEX_SOURCES,
    BEPINEX_VERSION_MATRIX,
    FONT_DOWNLOAD_URL,
    SYSTEM_FONT_CANDIDATES,
    TOOLS_CACHE,
    XUAT_REDIRECT_MARKER,
    XUNITY_DOWNLOAD_URLS,
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
    parse_unity_version_tuple,
)
from engines.xunity.diagnostics import (
    check_antivirus,
    check_path_encoding,
    detect_custom_assembly_resolver,
    detect_language_lock,
)
from engines.xunity.engine import XUnityRealtimeEngine
from engines.xunity.extract import UnityResourceScanner
from engines.xunity.runtime import (
    BepInExDeployer,
    FontDeployer,
    TranslationCacheGenerator,
    UGUIOverlayGuardDeployer,
    XUnityPluginDeployer,
)

registry.register(XUnityRealtimeEngine())
