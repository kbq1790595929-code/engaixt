from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from engines.base import EngineBase, EngineCapabilities, TextItem, registry
from engines.wolf.csv_text import apply_script_translations, extract_script_text_items
from engines.wolf.detect import confidence, find_wolf_exe
from engines.wolf.fonts import deploy_wolf_cjk_fonts, find_wolf_font_targets
from engines.wolf.json_text import apply_translations as apply_json_translations
from engines.wolf.json_text import extract_text_items as extract_json_text_items
from engines.wolf.text_safety import sanitize_wolf_items
from engines.wolf.toolchain import (
    load_state,
    prepare,
    repack,
    resolve_json_target,
    resolve_script_target,
)
from utils.logger import info, warning


class WolfEngine(EngineBase):
    name = "wolf"
    label = "WOLF RPG Editor"
    support_level = "beta"
    supports_extract = True
    supports_repack = True
    detect_priority = 97
    capabilities = EngineCapabilities(
        extract=True,
        repack=True,
        static_patch=True,
        runtime_patch=False,
        creates_launcher=False,
        portable_after_patch=True,
        requires_python=False,
        requires_frida=False,
        needs_external_tool=True,
        notes=(
            "使用 MIT 许可的 UberWolf 处理标准归档；已验证 WOLF Pro profile 走 native 静态闭环。",
            "地图、公共事件、数据库通过 UberWolf WolfRPG 结构解析器回填，不做二进制字符串扫描。",
            "Script CSV 剧情文本按单元格回填，保留原始引号、编码和换行。",
        ),
    )
    limitations = [
        "未知 WOLF Pro 归档没有已验证 profile 时，会在 AI 翻译前停止且不修改游戏资源。",
        "数据库和 SetString 属于结构化静态回填，发布前仍需实际游戏流程验收。",
    ]

    def __init__(self):
        self._last_repack_verification: dict = {}
        self._diagnostics: dict = {}
        self._translated_cjk_chars: set[str] = set()

    def detect(self, path: Path) -> bool:
        return confidence(path)[0] > 0

    def detect_confidence(self, path: Path) -> tuple[int, list[str]]:
        return confidence(path)

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        info("WOLF RPG: 正在使用 UberWolf 解包并导出结构化文本")
        state = prepare(path, workspace)
        if state.native_archive:
            info("WOLF RPG: 已匹配受保护归档 profile，使用 native 解包与原密钥重封")
        filter_stats: dict[str, int] = {}
        json_items = extract_json_text_items(
            Path(state.json_root),
            lambda rel: resolve_json_target(state, rel),
            filter_stats,
        )
        script_items = extract_script_text_items(
            Path(state.data_root),
            lambda path: resolve_script_target(state, path),
        )
        items = json_items + script_items
        self._diagnostics = {
            **asdict(state),
            "json_files": len(list(Path(state.json_root).rglob("*.json"))),
            "structured_text_items": len(json_items),
            "script_csv_text_items": len(script_items),
            "text_items": len(items),
            "text_filter": {
                "skipped_total": sum(filter_stats.values()),
                "by_reason": dict(sorted(filter_stats.items())),
            },
            "stage": "extract",
        }
        if filter_stats:
            summary = "、".join(f"{key}={value}" for key, value in sorted(filter_stats.items()))
            info(f"WOLF RPG: 翻译前已过滤 {sum(filter_stats.values())} 条内部字段/引用（{summary}）")
        info(
            "WOLF RPG: 从地图、公共事件、数据库和 Script CSV 提取 "
            f"{len(items)} 条文本（结构化 {len(json_items)} / 剧情 CSV {len(script_items)}）"
        )
        return items

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        self.sanitize_translations(items, str(getattr(self, "_target_lang", "zh-CN") or "zh-CN"))
        items = self.filter_repack_items(items)
        self._translated_cjk_chars = {
            char
            for item in items
            for char in str(item.translated or "")
            if "\u3400" <= char <= "\u9fff"
        }
        state = load_state(workspace)
        json_changed = apply_json_translations(Path(state.json_root), items)
        has_csv_translation = any(
            (item.meta or {}).get("wolf_csv_rel") and item.translated and item.translated != item.original
            for item in items
        )
        if json_changed == 0 and not has_csv_translation:
            raise RuntimeError("WOLF RPG 没有可回填的有效译文")
        csv_changed = 0

        def apply_script_csv(generated_root: Path) -> None:
            nonlocal csv_changed
            csv_changed = apply_script_translations(Path(state.data_root), generated_root, items)

        info(f"WOLF RPG: 正在结构化回填 {json_changed} 条译文")
        self._last_repack_verification = repack(workspace, state, post_apply=apply_script_csv)
        changed = json_changed + csv_changed
        self._last_repack_verification["applied_translations"] = changed
        self._diagnostics.update({
            "stage": "repack",
            "repack": self._last_repack_verification,
        })
        info(f"WOLF RPG: 归档重封与结构校验完成（共 {changed} 条，CSV {csv_changed}）")

    def filter_repack_items(self, items: list[TextItem]) -> list[TextItem]:
        safe: list[TextItem] = []
        dropped = 0
        for item in items:
            meta = item.meta or {}
            role = str(meta.get("wolf_role") or "")
            pointer = str(meta.get("wolf_pointer") or item.key or "")
            if role in {"database_name", "database_runtime_key"} or (
                role == "game" and pointer in {"/Title", "/TitlePlus"}
            ):
                dropped += 1
                continue
            safe.append(item)
        if dropped:
            warning(f"WOLF RPG: 已阻止 {dropped} 条运行时结构键译文进入回填")
            self._diagnostics["runtime_key_translations_dropped"] = dropped
        return safe

    def sanitize_translations(self, items: list[TextItem], target_lang: str = "zh-CN") -> list[TextItem]:
        stats = sanitize_wolf_items(items, target_lang)
        self._diagnostics["text_safety"] = stats.to_dict()
        if stats.changed or stats.control_mismatch_rejected or stats.residual_kana_rejected:
            info(
                "WOLF RPG: 文本安全校验完成 "
                f"(修复 {stats.changed}，恢复控制码 {stats.leading_controls_restored}，"
                f"恢复换行 {stats.line_layout_restored}，拦截 {stats.control_mismatch_rejected + stats.residual_kana_rejected + stats.non_chinese_rejected})"
            )
        return items

    def font_targets(self, game_dir: Path) -> list[Path]:
        return find_wolf_font_targets(game_dir)

    def deploy_cjk_fonts(self, game_dir: Path, workspace: Path) -> dict[str, object]:
        if not self._translated_cjk_chars:
            return {"source": "", "targets": [], "deployed": 0, "errors": []}
        result = deploy_wolf_cjk_fonts(game_dir, workspace)
        self._diagnostics["fonts"] = result
        if result.get("deployed"):
            info(f"WOLF RPG: 已为 {result['deployed']} 个本地字体部署完整简体中文字形")
        if result.get("errors"):
            warning(f"WOLF RPG: 有 {len(result['errors'])} 个字体部署失败，部分中文可能显示为空白")
        return result

    def diagnostics_snapshot(self) -> dict:
        return dict(self._diagnostics)

    def find_exe(self, path: Path) -> Path | None:
        return find_wolf_exe(path) or super().find_exe(path)


registry.register(WolfEngine())
