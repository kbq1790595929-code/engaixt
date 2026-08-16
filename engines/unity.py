from __future__ import annotations

import re
import subprocess
import shutil
from pathlib import Path

from engines.base import EngineBase, EngineCapabilities, TextItem, registry
from utils.logger import info, debug, warning
from utils.text_extract import is_translatable


class UnityEngine(EngineBase):
    name = "unity"
    label = "Unity 引擎"
    support_level = "partial"
    supports_repack = True  # binary string replacement in .assets files
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
            "直接扫描/替换 Unity 资源中的可见字符串。",
            "二进制资源覆盖率有限，通常作为 XUnity 之外的回退路径。",
        ),
    )
    detect_priority = 70
    limitations = [
        "物理回填 Unity assets 成功率有限；普通 Unity 游戏建议优先使用 xunity_realtime。",
    ]

    def detect(self, path: Path) -> bool:
        if path.is_file():
            path = path.parent
        data_dirs = list(path.glob("*_Data"))
        if data_dirs:
            for dd in data_dirs:
                if (dd / "globalgamemanagers").exists() or (dd / "data.unity3d").exists():
                    return True
                if (dd / "resources.assets").exists():
                    return True
        exe_files = list(path.glob("*.exe"))
        data_folders = [p for p in path.iterdir() if p.is_dir() and p.name.endswith("_Data")]
        return len(exe_files) > 0 and len(data_folders) > 0

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        game_dir = path if path.is_dir() else path.parent

        items: list[TextItem] = self._try_unitypy(game_dir, workspace)
        if not items:
            items = self._try_assetstudio_cli(game_dir, workspace)
        if not items:
            items = self._try_uabea(game_dir, workspace)
        if not items:
            items = self._fallback_scan(game_dir)

        info(f"提取到 {len(items)} 条可翻译文本")
        return items

    def _try_unitypy(self, game_dir: Path, workspace: Path) -> list[TextItem]:
        try:
            import UnityPy
        except ImportError:
            info("UnityPy 未安装，跳过（可运行: pip install UnityPy）")
            return []

        items = []
        data_dirs = list(game_dir.glob("*_Data"))
        for dd in data_dirs:
            asset_files = list(dd.glob("*.assets")) + list(dd.glob("*.unity3d")) + list(dd.glob("sharedassets*"))
            for asset_file in asset_files:
                try:
                    env = UnityPy.load(str(asset_file))
                    for obj in env.objects:
                        if obj.type.name in ("MonoBehaviour", "TextAsset"):
                            data = obj.read()
                            if hasattr(data, "m_Script") and data.m_Script:
                                text = data.dump() if hasattr(data, "dump") else str(data)
                                self._extract_strings(text, asset_file.name, items)
                            elif hasattr(data, "m_Script") is False and hasattr(data, "script"):
                                text = str(data.script)
                                self._extract_strings(text, asset_file.name, items)
                except Exception as e:
                    debug(f"UnityPy 处理 {asset_file.name} 失败: {e}")

        return items

    def _try_assetstudio_cli(self, game_dir: Path, workspace: Path) -> list[TextItem]:
        """使用 AssetStudio/AssetRipper CLI 解包。"""
        try:
            from core.tool_manager import find_tool, ensure_tool
        except Exception:
            find_tool = None
            ensure_tool = None

        tool_paths = [
            Path("tools/AssetStudio/AssetStudioCLI.exe"),
            Path("tools/AssetStudioCLI.exe"),
            shutil.which("AssetStudioCLI"),
        ]
        exe = None
        for p in tool_paths:
            if p and (Path(p).exists() if not isinstance(p, str) else Path(p).exists()):
                exe = Path(p)
                break

        if not exe:
            if find_tool:
                exe = find_tool("assetripper")
            if not exe and ensure_tool:
                exe = ensure_tool("assetripper")

        if not exe:
            info("AssetStudioCLI/AssetRipper 未找到，跳过 Unity 外部解包")
            return []

        extract_dir = workspace / "translated" / "unity_extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        data_dirs = list(game_dir.glob("*_Data"))
        if not data_dirs:
            return []
        data_dir = data_dirs[0]

        commands = [
            [str(exe), str(data_dir), "-o", str(extract_dir), "-t", "text"],
            [str(exe), str(game_dir), "-o", str(extract_dir)],
            [str(exe), str(game_dir), "--output", str(extract_dir)],
        ]
        for cmd in commands:
            try:
                info(f"调用 {Path(exe).name} 解包 Unity 资源...")
                before = _count_files(extract_dir)
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                after = _count_files(extract_dir)
                if result.returncode == 0 and after > before:
                    break
            except subprocess.TimeoutExpired:
                warning(f"{Path(exe).name} 超时")
            except Exception as e:
                warning(f"{Path(exe).name} 失败: {e}")

        return self._scan_extracted_text(extract_dir, game_dir)

    def _try_uabea(self, game_dir: Path, workspace: Path) -> list[TextItem]:
        """使用 UABEA (Unity Assets Bundle Extractor Avalonia) 解包。"""
        tool_paths = [
            Path("tools/UABEA/UABEAvalonia.exe"),
            Path("tools/UABEA.exe"),
        ]
        exe = None
        for p in tool_paths:
            if p.exists():
                exe = p
                break
        if not exe:
            return []

        info("UABEA 仅支持 GUI 交互模式，此版本暂不集成批量解包，请优先使用 UnityPy")
        return []

    def _fallback_scan(self, game_dir: Path) -> list[TextItem]:
        """兜底：扫描常见 Unity 文本资源文件格式。"""
        items = []
        import json

        data_dirs = list(game_dir.glob("*_Data"))
        for dd in data_dirs:
            # 扫描 StreamingAssets 中的 JSON/文本
            streaming = dd / "StreamingAssets"
            if streaming.is_dir():
                for f in streaming.rglob("*"):
                    if f.suffix.lower() in (".json", ".txt", ".csv"):
                        try:
                            content = f.read_text(encoding="utf-8-sig", errors="ignore")
                            if f.suffix.lower() == ".json":
                                data = json.loads(content)
                                self._extract_json_strings(data, str(f.relative_to(game_dir)), items)
                            else:
                                for i, line in enumerate(content.split("\n")):
                                    line = line.strip()
                                    if is_translatable(line) and len(line) < 500:
                                        items.append(TextItem(
                                            file=str(f.relative_to(game_dir)),
                                            key=f"line_{i}", original=line, line=i + 1,
                                        ))
                        except Exception:
                            continue

        return items

    def _extract_strings(self, text: str, source: str, items: list[TextItem]):
        import re
        string_pattern = re.compile(r'["\']([^"\']{4,})["\']')
        for m in string_pattern.finditer(text):
            s = m.group(1)
            if is_translatable(s):
                items.append(TextItem(file=source, original=s))

    def _extract_json_strings(self, data, path: str, items: list[TextItem], prefix: str = ""):
        import json
        if isinstance(data, dict):
            for k, v in data.items():
                key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, str) and is_translatable(v):
                    items.append(TextItem(file=path, key=key, original=v))
                elif isinstance(v, (dict, list)):
                    self._extract_json_strings(v, path, items, key)
        elif isinstance(data, list):
            for i, v in enumerate(data):
                key = f"{prefix}[{i}]"
                if isinstance(v, str) and is_translatable(v):
                    items.append(TextItem(file=path, key=key, original=v))
                elif isinstance(v, (dict, list)):
                    self._extract_json_strings(v, path, items, key)

    def _scan_extracted_text(self, extract_dir: Path, game_dir: Path) -> list[TextItem]:
        items = []
        for f in extract_dir.rglob("*"):
            if f.suffix.lower() in (".txt", ".json", ".csv"):
                try:
                    content = f.read_text(encoding="utf-8-sig", errors="ignore")
                    for i, line in enumerate(content.split("\n")):
                        line = line.strip()
                        if is_translatable(line):
                            items.append(TextItem(
                                file=str(f.relative_to(extract_dir)),
                                key=f"line_{i}", original=line, line=i + 1,
                            ))
                except Exception:
                    continue
        return items

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        if not items:
            return

        translated = [it for it in items if it.translated and it.translated != it.original]
        if not translated:
            info("没有需要回填的翻译")
            return

        game_dir = Path(getattr(self, "_game_dir", "."))
        data_dirs = list(game_dir.glob("*_Data"))
        if not data_dirs:
            warning("Unity 回填：未找到 *_Data 目录")
            return

        data_dir = data_dirs[0]

        # Build translation map
        trans_map: dict[str, str] = {}
        for it in translated:
            if it.translated and it.translated != it.original:
                trans_map[it.original] = it.translated

        info(f"UnityPy 回填: {len(trans_map)} 条翻译 -> {data_dir}")

        try:
            import UnityPy
        except ImportError:
            warning("UnityPy 未安装，无法回填")
            return

        asset_files = list(data_dir.glob("*.assets")) + list(data_dir.glob("sharedassets*"))
        total = 0

        for asset_file in asset_files:
            if not asset_file.suffix == ".assets" and not asset_file.name.startswith("sharedassets"):
                continue
            if not asset_file.exists():
                continue

            try:
                env = UnityPy.load(str(asset_file))
                modified = 0

                for obj in env.objects:
                    if obj.type.name != "TextAsset":
                        continue
                    data = obj.read()
                    text = getattr(data, "m_Script", None)
                    if not isinstance(text, str):
                        continue

                    new_text = _replace_in_text(text, trans_map)
                    if new_text != text:
                        data.m_Script = new_text
                        if hasattr(data, "save"):
                            data.save()
                        modified += 1

                if modified > 0:
                    env.save()
                    total += modified
                    info(f"  {asset_file.name}: {modified} TextAssets 已更新")
            except Exception as e:
                debug(f"  {asset_file.name}: {e}")

        info(f"UnityPy 回填完成: {total} TextAssets 更新")

    def find_exe(self, path: Path) -> Path | None:
        game_dir = path if path.is_dir() else path.parent
        from core.exe_selector import find_main_exe

        return find_main_exe(game_dir, recursive=False) or super().find_exe(path)


registry.register(UnityEngine())


def _replace_in_text(text: str, trans_map: dict[str, str]) -> str:
    """Replace Japanese text with Chinese in CSV-formatted TextAsset content.
    Preserves CSV structure — only replaces message column values."""
    if "scenarioId" not in text[:200]:
        # Plain text or non-CSV: direct string replacement
        result = text
        for orig, trans in trans_map.items():
            if orig in result:
                result = result.replace(orig, trans)
        return result

    # CSV format: replace message column only
    lines = text.split("\r\n")
    if len(lines) < 2:
        return text

    hdr = lines[0].split(",")
    try:
        msg_idx = hdr.index("message")
    except ValueError:
        return text

    new_lines = [lines[0]]
    for line in lines[1:]:
        if not line.strip():
            new_lines.append(line)
            continue
        parts = line.split(",")
        if msg_idx < len(parts) and parts[msg_idx].strip() in trans_map:
            trans = trans_map[parts[msg_idx].strip()]
            if trans != parts[msg_idx].strip():
                parts[msg_idx] = trans
        new_lines.append(",".join(parts))

    return "\r\n".join(new_lines)


def _count_files(path: Path) -> int:
    try:
        return sum(1 for p in path.rglob("*") if p.is_file())
    except Exception:
        return 0
