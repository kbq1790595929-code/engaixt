from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from engines.base import EngineBase, EngineCapabilities, TextItem, registry
from utils.logger import info, warning, debug
from utils.text_extract import is_translatable


class UnrealEngine(EngineBase):
    name = "unreal"
    label = "Unreal Engine"
    support_level = "partial"
    supports_extract = True
    supports_repack = False
    capabilities = EngineCapabilities(
        extract=True,
        repack=False,
        static_patch=False,
        runtime_patch=False,
        creates_launcher=False,
        portable_after_patch=False,
        requires_python=False,
        requires_frida=False,
        needs_external_tool=True,
        notes=(
            "扫描已解包或明文本地化资源；可尝试 UnrealPak 解包 .pak。",
            "IoStore 与 .uasset 自动回填尚未启用。",
        ),
    )
    detect_priority = 58
    limitations = [
        "可扫描已解包 Unreal 文本资源，并在有 UnrealPak.exe 时尝试解包 .pak。",
        "IoStore (.ucas/.utoc) 与大多数 .uasset 二进制回填仍建议使用 FModel/UE 专用流程。",
    ]

    _TEXT_EXTS = {".po", ".pot", ".ini", ".int", ".json", ".csv", ".txt", ".locmeta", ".locres"}
    _SKIP_DIRS = {"Binaries", "Saved", "Intermediate", "DerivedDataCache", ".git"}

    def detect(self, path: Path) -> bool:
        if path.is_file():
            path = path.parent
        has_pak = (
            _has_path_match(path, "*.pak")
            or _has_path_match(path, "*/*.pak")
            or _has_path_match(path, "**/Paks/*.pak")
        )
        has_iostore = _has_path_match(path, "**/*.utoc") or _has_path_match(path, "**/*.ucas")
        engine_dir = (path / "Engine").is_dir() or (path / "engine").is_dir()
        project_marker = bool(list(path.glob("*.uproject"))) or (path / "Content").is_dir()
        return has_pak or has_iostore or engine_dir or project_marker

    def detect_confidence(self, path: Path) -> tuple[int, list[str]]:
        game_dir = path if path.is_dir() else path.parent
        evidence = []
        score = 0
        pak_count = _count_path_matches_limited(game_dir, "**/*.pak", limit=20)
        if pak_count:
            evidence.append(f"找到 {pak_count} 个 .pak 文件")
            score = max(score, 68)
        if _has_path_match(game_dir, "**/*.utoc") or _has_path_match(game_dir, "**/*.ucas"):
            evidence.append("找到 IoStore .utoc/.ucas 文件")
            score = max(score, 66)
        if (game_dir / "Engine").is_dir() or (game_dir / "Content").is_dir():
            evidence.append("找到 Unreal Engine/Content 目录")
            score = max(score, 58)
        if _has_path_match(game_dir, "*.uproject"):
            evidence.append("找到 .uproject")
            score = max(score, 72)
        return score, evidence

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        game_dir = path if path.is_dir() else path.parent
        extract_dir = workspace / "original"
        extract_dir.mkdir(parents=True, exist_ok=True)

        pak_files = sorted(game_dir.glob("**/*.pak"))
        if pak_files:
            self._try_extract_paks(pak_files, extract_dir)
        if list(game_dir.glob("**/*.utoc")) or list(game_dir.glob("**/*.ucas")):
            warning("检测到 Unreal IoStore (.ucas/.utoc)。当前会扫描明文资源；完整解包建议使用 FModel。")

        # Copy existing loose text resources so the generic scanning path works
        # for modded/unpacked games and exported Localization folders.
        for root, dirs, files in game_dir.walk():
            dirs[:] = [d for d in dirs if d not in self._SKIP_DIRS]
            for name in files:
                src = root / name
                if src.suffix.lower() not in self._TEXT_EXTS:
                    continue
                try:
                    if src.stat().st_size > 10 * 1024 * 1024:
                        continue
                except OSError:
                    continue
                rel = src.relative_to(game_dir)
                dest = extract_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    shutil.copy2(src, dest)

        items = self._scan_text_resources(extract_dir)
        info(f"Unreal 扫描提取到 {len(items)} 条可翻译文本")
        return items

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        info("Unreal Engine 自动回填未启用；请将导出的翻译用于 .po/.locres 或 UE 专用打包流程。")

    def _try_extract_paks(self, pak_files: list[Path], extract_dir: Path) -> bool:
        unrealpak = self._find_unrealpak()
        if not unrealpak:
            warning("未找到 UnrealPak.exe；将只扫描已解包/明文资源。")
            return False
        ok = False
        for pak in pak_files[:20]:
            out = extract_dir / pak.stem
            out.mkdir(parents=True, exist_ok=True)
            try:
                result = subprocess.run(
                    [str(unrealpak), str(pak), "-Extract", str(out)],
                    capture_output=True, text=True, timeout=600,
                )
                if result.returncode == 0 and any(out.rglob("*")):
                    info(f"  UnrealPak 解包成功: {pak.name}")
                    ok = True
                else:
                    debug(f"UnrealPak 解包失败 {pak.name}: {result.stderr}")
            except subprocess.TimeoutExpired:
                warning(f"UnrealPak 解包超时: {pak.name}")
            except Exception as e:
                debug(f"UnrealPak 解包异常 {pak.name}: {e}")
        return ok

    def _find_unrealpak(self) -> Path | None:
        try:
            from core.tool_manager import find_tool
            tool = find_tool("unrealpak")
            if tool:
                return tool
        except Exception:
            pass
        found = shutil.which("UnrealPak")
        return Path(found) if found else None

    def _scan_text_resources(self, root: Path) -> list[TextItem]:
        items: list[TextItem] = []
        for file_path in root.rglob("*"):
            if not file_path.is_file() or file_path.suffix.lower() not in self._TEXT_EXTS:
                continue
            try:
                size = file_path.stat().st_size
            except OSError:
                continue
            if size < 4 or size > 10 * 1024 * 1024:
                continue
            rel = str(file_path.relative_to(root))
            ext = file_path.suffix.lower()
            if ext == ".json":
                items.extend(self._extract_json(file_path, rel))
            elif ext in (".po", ".pot"):
                items.extend(self._extract_po(file_path, rel))
            elif ext == ".locres":
                items.extend(self._extract_binary_utf16(file_path, rel))
            else:
                items.extend(self._extract_lines(file_path, rel))
        return items

    def _extract_json(self, file_path: Path, rel: str) -> list[TextItem]:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8-sig", errors="replace"))
        except Exception:
            return []
        items: list[TextItem] = []
        self._walk_json(data, rel, "", items)
        return items

    def _walk_json(self, node, rel: str, prefix: str, items: list[TextItem]):
        if isinstance(node, dict):
            for key, value in node.items():
                full_key = f"{prefix}.{key}" if prefix else str(key)
                if isinstance(value, str) and is_translatable(value):
                    items.append(TextItem(file=rel, key=full_key, original=value))
                elif isinstance(value, (dict, list)):
                    self._walk_json(value, rel, full_key, items)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                full_key = f"{prefix}[{idx}]"
                if isinstance(value, str) and is_translatable(value):
                    items.append(TextItem(file=rel, key=full_key, original=value))
                elif isinstance(value, (dict, list)):
                    self._walk_json(value, rel, full_key, items)

    def _extract_po(self, file_path: Path, rel: str) -> list[TextItem]:
        content = file_path.read_text(encoding="utf-8-sig", errors="replace")
        items: list[TextItem] = []
        current = ""
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("msgid "):
                current = _unquote_po(stripped[6:].strip())
            elif stripped.startswith('"') and current is not None and not stripped.startswith('msgstr'):
                current += _unquote_po(stripped)
            elif stripped.startswith("msgstr"):
                if current and is_translatable(current):
                    items.append(TextItem(file=rel, key=f"msgid_{i}", original=current, line=i))
                current = ""
        return items

    def _extract_lines(self, file_path: Path, rel: str) -> list[TextItem]:
        for enc in ("utf-8-sig", "utf-16", "cp932"):
            try:
                content = file_path.read_text(encoding=enc, errors="strict")
                break
            except Exception:
                content = ""
        if not content:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        items: list[TextItem] = []
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip().strip('"')
            if is_translatable(stripped) and len(stripped) < 500:
                items.append(TextItem(file=rel, key=f"line_{i}", original=stripped, line=i))
        return items

    def _extract_binary_utf16(self, file_path: Path, rel: str) -> list[TextItem]:
        try:
            data = file_path.read_bytes()
        except Exception:
            return []
        items: list[TextItem] = []
        seen: set[str] = set()
        # Many locres files store UTF-16LE strings with null terminators. This
        # is read-only extraction, but it gives users usable text even when
        # automatic repack is unsafe.
        pattern = re.compile(rb'(?:[\x20-\xff]\x00){3,}')
        for idx, match in enumerate(pattern.finditer(data)):
            try:
                text = match.group(0).decode("utf-16le", errors="ignore").strip("\x00").strip()
            except Exception:
                continue
            if text in seen or not is_translatable(text):
                continue
            seen.add(text)
            items.append(TextItem(file=rel, key=f"utf16_{idx}", original=text))
        return items

    def find_exe(self, path: Path) -> Path | None:
        game_dir = path if path.is_dir() else path.parent
        for exe_name in [f"{path.stem}.exe", "game.exe", "shipping.exe"]:
            exe = game_dir / exe_name
            if exe.exists():
                return exe
        return super().find_exe(path)


def _unquote_po(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    return value.replace(r"\"", '"').replace(r"\n", "\n").replace(r"\t", "\t")


def _has_path_match(path: Path, pattern: str) -> bool:
    try:
        return next(path.glob(pattern), None) is not None
    except OSError:
        return False


def _count_path_matches_limited(path: Path, pattern: str, *, limit: int) -> int:
    count = 0
    try:
        for _match in path.glob(pattern):
            count += 1
            if count >= limit:
                return count
    except OSError:
        return 0
    return count


registry.register(UnrealEngine())
