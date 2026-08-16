import json
import os
from pathlib import Path
from dataclasses import dataclass, field, fields


CONFIG_PATH = Path.home() / ".game_translator_config.json"
DEFAULT_TOOL_UPDATE_INTERVAL_HOURS = 5 * 24

# --- 配置 schema 的权威定义 ---------------------------------------------
# 敏感字段：绝不允许进入 diagnostics、日志或任何导出。
# 按名称词根判定，新增 *_api_key/token/secret 类字段自动被覆盖。
SENSITIVE_FIELD_TOKENS = ("api_key", "apikey", "secret", "token", "password")


def is_sensitive_field(name: str) -> bool:
    lowered = str(name).lower()
    if lowered.endswith("_tokens") and any(
        metric in lowered
        for metric in ("input", "output", "prompt", "completion", "translation")
    ):
        return False
    if lowered in {"total_tokens", "max_tokens"}:
        return False
    return any(tok in lowered for tok in SENSITIVE_FIELD_TOKENS)


# 允许写入 diagnostics 快照的运行时字段（与排查问题相关的行为开关）。
# 这是"可导出字段"的单一权威清单；diagnostics 侧只做引用不做定义。
DIAGNOSTIC_SNAPSHOT_FIELDS = frozenset({
    "active_translator",
    "target_lang",
    "ui_language",
    "openai_model",
    "deepseek_model",
    "qwen_model",
    "zhipu_model",
    "moonshot_model",
    "doubao_model",
    "anthropic_model",
    "hy_mt2_model",
    "hy_mt2_context_size",
    "hy_mt2_batch_size",
    "hy_mt2_idle_timeout_seconds",
    "max_batch_size",
    "max_concurrency",
    "translation_coverage",
    "minimum_translation_coverage",
    "translation_cache_enabled",
    "translation_cache_auto_cleanup",
    "translation_cache_max_size_gb",
    "keep_workspace",
    "workspace_dir",
    "tools_dir",
    "auto_download_tools",
    "auto_update_tools",
    "tool_update_interval_hours",
    "auto_launch",
    "bgi_font_height_scale",
    "kirikiri_use_external_krkrdump",
    "kirikiri_auto_launch_dump",
    "kirikiri_enable_static_patch",
    "kirikiri_runtime_completion_mode",
    "kirikiri_runtime_merge_captures",
    "kirikiri_no_window_timeout_seconds",
    "kirikiri_overlay_font_family",
    "kirikiri_overlay_font_size",
    "kirikiri_overlay_height",
    "kirikiri_overlay_max_width",
    "kirikiri_overlay_text_only",
    "kirikiri_overlay_show_speaker",
    "kirikiri_overlay_show_original",
    "kirikiri_overlay_live_translate",
    "kirikiri_overlay_follow_window",
    "kirikiri_overlay_locked",
    "kirikiri_overlay_remember_position",
})


@dataclass
class Config:
    openai_api_key: str = ""
    openai_model: str = "gpt-5.4-mini"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    qwen_api_key: str = ""
    qwen_model: str = "qwen3.7-plus"
    zhipu_api_key: str = ""
    zhipu_model: str = "glm-4.7-flash"
    moonshot_api_key: str = ""
    moonshot_model: str = "kimi-k2.6"
    doubao_api_key: str = ""
    doubao_model: str = "doubao-seed-2-1-turbo-260628"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    hy_mt2_model: str = "Hy-MT2-1.8B-Q4_K_M"
    hy_mt2_context_size: int = 4096
    hy_mt2_batch_size: int = 8
    hy_mt2_idle_timeout_seconds: int = 180
    active_translator: str = "deepseek"
    ui_language: str = "zh-CN"
    target_lang: str = "zh-CN"
    engine_whitelist: list[str] = field(default_factory=list)
    max_batch_size: int = 20
    max_concurrency: int = 55  # AI 翻译最大并行数
    translation_coverage: int = 100  # 翻译覆盖百分比: 1-100, 默认全部翻译
    minimum_translation_coverage: int = 80  # 翻译完成后最低通过覆盖率；低于该值阻止回填
    translation_cache_enabled: bool = True  # 全局 API 译文缓存；关闭后不读取/写入跨任务缓存
    translation_cache_auto_cleanup: bool = True  # 缓存超过阈值后自动清理旧记录
    translation_cache_max_size_gb: float = 1.0  # 全局译文缓存上限，默认 1GB
    keep_workspace: bool = False
    workspace_dir: str = ""  # 空则默认 Downloads/.game_translator/workspaces
    frida_timeout: int = 60  # Frida 密钥捕获超时秒数
    tools_dir: str = ""  # 外部工具目录（空则默认 Downloads/.game_translator/tools/）
    auto_download_tools: bool = True  # 自动下载缺失的工具
    auto_update_tools: bool = True  # 自动更新已由管线托管安装的外部工具
    tool_update_interval_hours: int = DEFAULT_TOOL_UPDATE_INTERVAL_HOURS  # 自动更新检查间隔；默认5天
    auto_launch: bool = True  # 翻译完成后自动启动游戏
    ui_accent: str = "#8b7cff"  # UI 主色调
    bg_color: str = "#08080d"  # UI 背景色
    bg_image: str = ""  # UI 背景图片路径
    ref_tool_dir: str = ""  # 参考工具 trans/ 目录（BepInEx/XUAT 变体打包来源，空则用默认路径）
    cjk_font_path: str = ""  # CJK 字体 TTF 路径，用于 Godot 游戏字体替换
    godot_editor_path: str = ""  # Godot Editor 路径，用于字体导入（空则自动检测）

    bgi_font_height_scale: float = 0.95  # BGI Frida Chinese fallback font scale
    kirikiri_use_external_krkrdump: bool = False  # 默认不用第三方 KrkrDumpLoader，避免桌面环境副作用
    kirikiri_auto_launch_dump: bool = False  # 默认不自动拉起游戏做 dump，避免影响加速器/桌面环境
    kirikiri_enable_static_patch: bool = False  # 实验开关：默认不做 KRKR 静态回填/patch.xp3
    kirikiri_runtime_completion_mode: str = "captured_only"  # captured_only/coverage/all
    kirikiri_runtime_merge_captures: bool = True  # 续翻时合并运行时捕获到的新文本
    kirikiri_no_window_timeout_seconds: int = 45  # KRKR 启动后无可见游戏窗口的失败判定时间
    kirikiri_overlay_font_family: str = "Microsoft YaHei UI"
    kirikiri_overlay_font_size: int = 18
    kirikiri_overlay_original_font_size: int = 11
    kirikiri_overlay_text_color: str = "#f4f7ff"
    kirikiri_overlay_speaker_color: str = "#ffd36e"
    kirikiri_overlay_original_color: str = "#a6adbb"
    kirikiri_overlay_bg_color: str = "#111318"
    kirikiri_overlay_pending_color: str = "#f6b73c"
    kirikiri_overlay_waiting_color: str = "#7c8496"
    kirikiri_overlay_opacity: float = 0.92
    kirikiri_overlay_height: int = 180
    kirikiri_overlay_max_width: int = 900
    kirikiri_overlay_text_only: bool = True
    kirikiri_overlay_show_speaker: bool = True
    kirikiri_overlay_show_original: bool = False
    kirikiri_overlay_live_translate: bool = True
    kirikiri_overlay_follow_window: bool = True
    kirikiri_overlay_locked: bool = False
    kirikiri_overlay_remember_position: bool = True
    kirikiri_overlay_saved_x: int = -1
    kirikiri_overlay_saved_y: int = -1
    kirikiri_overlay_saved_width: int = 0

    def save(self):
        d = {k: v for k, v in self.__dict__.items()}
        CONFIG_PATH.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")

    def public_dict(self) -> dict:
        """安全导出视图：剔除全部敏感字段，可用于诊断/分享/上报。"""
        return {
            k: v for k, v in self.__dict__.items() if not is_sensitive_field(k)
        }

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
                if int(data.get("tool_update_interval_hours", 0) or 0) == 24:
                    data["tool_update_interval_hours"] = DEFAULT_TOOL_UPDATE_INTERVAL_HOURS
                return cls(**cls._validated(data))
            except (json.JSONDecodeError, TypeError):
                pass
        return cls()

    @classmethod
    def _validated(cls, data: dict) -> dict:
        """按 schema 校验字段类型：未知键丢弃，类型不符回退默认值。

        配置文件可能被手改或被旧版本写坏，这里保证坏值不会
        以错误类型流进业务代码。
        """
        defaults = cls()
        kwargs: dict = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            coerced = _coerce_config_value(data[f.name], getattr(defaults, f.name))
            if coerced is not _INVALID:
                kwargs[f.name] = coerced
        return kwargs


_INVALID = object()


def _coerce_config_value(value, default):
    """把外来值安全地贴合到默认值的类型；贴合不上返回 _INVALID。"""
    if isinstance(default, bool):
        return value if isinstance(value, bool) else _INVALID
    if isinstance(default, int):
        if isinstance(value, bool):
            return _INVALID
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return _INVALID
        return _INVALID
    if isinstance(default, float):
        if isinstance(value, bool):
            return _INVALID
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return _INVALID
        return _INVALID
    if isinstance(default, str):
        return value if isinstance(value, str) else _INVALID
    if isinstance(default, list):
        if isinstance(value, list):
            return [str(item) for item in value]
        return _INVALID
    return value


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def reload_config() -> Config:
    global _config
    _config = Config.load()
    return _config


def save_config():
    if _config:
        _config.save()
