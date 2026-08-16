from __future__ import annotations

import csv
import configparser
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from engines.base import EngineBase, EngineCapabilities, TextItem, registry
from utils.logger import info
from utils.text_extract import is_translatable
from utils.extract_guard import ExtractionGuard, is_config_key


class GenericEngine(EngineBase):
    name = "generic"
    label = "通用文本扫描"
    support_level = "partial"
    capabilities = EngineCapabilities(
        extract=True,
        repack=True,
        static_patch=True,
        runtime_patch=False,
        creates_launcher=False,
        portable_after_patch=True,
        requires_python=False,
        requires_frida=False,
        notes=(
            "仅处理明文文本/JSON/CSV/XML/INI/Lua 等文件。",
            "不处理封包、加密资源或复杂二进制脚本。",
        ),
    )
    detect_priority = 1
    limitations = [
        "只处理明文文本/JSON/CSV/XML/INI/Lua 等文件，不处理封包或加密资源。",
    ]

    _TEXT_EXTS = {
        ".txt", ".json", ".jsonl", ".csv", ".tsv", ".xml", ".ini", ".cfg",
        ".yaml", ".yml", ".lua", ".po", ".pot", ".md", ".ks", ".tjs", ".rpy",
        ".gd", ".tscn", ".tres", ".strings",
    }
    _SKIP_DIRS = {
        "__pycache__", ".git", "node_modules", "lib", "renpy", "python",
        "save", "saves", "cache", "logs", "BepInEx", ".godot",
    }

    def detect(self, path: Path) -> bool:
        return True

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        game_dir = path if path.is_dir() else path.parent
        items: list[TextItem] = []

        for root, dirs, files in game_dir.walk():
            dirs[:] = [d for d in dirs if d not in self._SKIP_DIRS]
            for f in files:
                ext = Path(f).suffix.lower()
                if ext not in self._TEXT_EXTS:
                    continue
                filepath = Path(root) / f
                rel_path = str(filepath.relative_to(game_dir))
                try:
                    size = filepath.stat().st_size
                except OSError:
                    continue
                if size > 5 * 1024 * 1024:
                    continue

                self._copy_source_file(filepath, game_dir, workspace)

                if ext == ".json":
                    items.extend(self._extract_json(filepath, rel_path))
                elif ext == ".jsonl":
                    items.extend(self._extract_jsonl(filepath, rel_path))
                elif ext in (".csv", ".tsv"):
                    items.extend(self._extract_csv(filepath, rel_path, delimiter="\t" if ext == ".tsv" else ","))
                elif ext == ".xml":
                    items.extend(self._extract_xml(filepath, rel_path))
                elif ext in (".ini", ".cfg"):
                    items.extend(self._extract_ini(filepath, rel_path))
                elif ext in (".po", ".pot"):
                    items.extend(self._extract_po(filepath, rel_path))
                elif ext == ".lua":
                    items.extend(self._extract_lua(filepath, rel_path))
                else:
                    items.extend(self._extract_text(filepath, rel_path))

        info(f"通用扫描提取到 {len(items)} 条可翻译文本")
        return items

    def _extract_json(self, filepath: Path, rel_path: str) -> list[TextItem]:
        items = []
        try:
            data = json.loads(filepath.read_text(encoding="utf-8-sig", errors="ignore"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return items
        self._walk_json(data, rel_path, "", items)
        return items

    def _extract_jsonl(self, filepath: Path, rel_path: str) -> list[TextItem]:
        items: list[TextItem] = []
        for line_no, line in enumerate(filepath.read_text(encoding="utf-8-sig", errors="ignore").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                if is_translatable(line):
                    items.append(TextItem(file=rel_path, key=f"line_{line_no}", original=line, line=line_no))
                continue
            line_items: list[TextItem] = []
            self._walk_json(data, rel_path, f"line_{line_no}", line_items)
            items.extend(line_items)
        return items

    def _walk_json(self, node, path: str, key_prefix: str, items: list):
        if isinstance(node, dict):
            for k, v in node.items():
                full_key = f"{key_prefix}.{k}" if key_prefix else k
                if isinstance(v, str) and is_translatable(v):
                    # Skip config/technical keys (id, path, type, script, etc.)
                    if not is_config_key(full_key):
                        items.append(TextItem(file=path, key=full_key, original=v))
                elif isinstance(v, (dict, list)):
                    self._walk_json(v, path, full_key, items)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                full_key = f"{key_prefix}[{i}]"
                if isinstance(v, str) and is_translatable(v):
                    items.append(TextItem(file=path, key=full_key, original=v))
                elif isinstance(v, (dict, list)):
                    self._walk_json(v, path, full_key, items)

    def _extract_text(self, filepath: Path, rel_path: str) -> list[TextItem]:
        items = []
        content = self._read_text_guess(filepath)
        if content is None:
            return items
        lines = content.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or len(stripped) >= 500:
                continue
            # Skip code-heavy lines: many {}();= but few letters
            code_chars = sum(1 for c in stripped if c in "{}();=")
            letters = sum(1 for c in stripped if c.isalpha())
            if code_chars > 0 and letters < 8 and code_chars >= letters * 0.5:
                continue
            if is_translatable(stripped):
                items.append(TextItem(file=rel_path, key=f"line_{i}", original=stripped, line=i + 1))
        return items

    def _extract_csv(self, filepath: Path, rel_path: str, delimiter: str = ",") -> list[TextItem]:
        items: list[TextItem] = []
        content = self._read_text_guess(filepath)
        if content is None:
            return items
        try:
            reader = csv.reader(content.splitlines(), delimiter=delimiter)
            for row_idx, row in enumerate(reader, 1):
                for col_idx, value in enumerate(row):
                    text = value.strip()
                    if is_translatable(text) and len(text) < 500:
                        items.append(TextItem(file=rel_path, key=f"R{row_idx}C{col_idx}", original=text, line=row_idx))
        except Exception:
            items.extend(self._extract_text(filepath, rel_path))
        return items

    def _extract_xml(self, filepath: Path, rel_path: str) -> list[TextItem]:
        items: list[TextItem] = []
        content = self._read_text_guess(filepath)
        if content is None:
            return items
        try:
            root = ET.fromstring(content)
        except Exception:
            return self._extract_text(filepath, rel_path)

        def walk(elem, path: str):
            if elem.text and is_translatable(elem.text.strip()):
                items.append(TextItem(file=rel_path, key=f"{path}.text", original=elem.text.strip()))
            for attr, value in elem.attrib.items():
                if is_translatable(value):
                    items.append(TextItem(file=rel_path, key=f"{path}@{attr}", original=value))
            for idx, child in enumerate(list(elem)):
                walk(child, f"{path}/{child.tag}[{idx}]")

        walk(root, root.tag)
        return items

    def _extract_ini(self, filepath: Path, rel_path: str) -> list[TextItem]:
        items: list[TextItem] = []
        content = self._read_text_guess(filepath)
        if content is None:
            return items
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            parser.read_string(content)
            for section in parser.sections():
                for key, value in parser.items(section):
                    if is_translatable(value):
                        items.append(TextItem(file=rel_path, key=f"{section}.{key}", original=value))
        except Exception:
            # Many game cfg files are not strict INI; fall back to key=value lines.
            for line_no, line in enumerate(content.splitlines(), 1):
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip().strip('"')
                if is_translatable(value):
                    items.append(TextItem(file=rel_path, key=f"line_{line_no}:{key.strip()}", original=value, line=line_no))
        return items

    def _extract_po(self, filepath: Path, rel_path: str) -> list[TextItem]:
        content = self._read_text_guess(filepath)
        if content is None:
            return []
        items: list[TextItem] = []
        current = ""
        for line_no, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("msgid "):
                current = _unquote(stripped[6:].strip())
            elif stripped.startswith('"') and current:
                current += _unquote(stripped)
            elif stripped.startswith("msgstr"):
                if current and is_translatable(current):
                    items.append(TextItem(file=rel_path, key=f"msgid_{line_no}", original=current, line=line_no))
                current = ""
        return items

    def _extract_lua(self, filepath: Path, rel_path: str) -> list[TextItem]:
        content = self._read_text_guess(filepath)
        if content is None:
            return []
        items: list[TextItem] = []
        # Keyed strings are much safer than replacing every Lua literal.
        pattern = re.compile(
            r'(?P<key>\b(?:text|message|msg|name|title|desc|description|dialog|dialogue|line|choice)\w*)'
            r'\s*=\s*(?P<quote>["\'])(?P<value>(?:\\.|(?!\2).)*?)(?P=quote)',
            re.IGNORECASE,
        )
        for match in pattern.finditer(content):
            value = match.group("value")
            if is_translatable(value):
                line = content.count("\n", 0, match.start()) + 1
                items.append(TextItem(file=rel_path, key=f"lua_{match.start()}", original=value, line=line))
        if not items:
            items.extend(self._extract_text(filepath, rel_path))
        return items

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        if not items:
            return

        file_groups: dict[str, list[TextItem]] = {}
        for item in items:
            if item.translated and item.translated != item.original:
                file_groups.setdefault(item.file, []).append(item)

        for filename, file_items in file_groups.items():
            target = workspace / "original" / filename
            if not target.exists():
                continue
            ext = Path(filename).suffix.lower()

            if ext == ".json":
                self._repack_json(target, file_items)
            else:
                self._repack_text(target, file_items)

        info(f"回填完成: {len(file_groups)} 个文件")

    def _copy_source_file(self, filepath: Path, game_dir: Path, workspace: Path):
        rel = filepath.relative_to(game_dir)
        dest = workspace / "original" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            try:
                dest.write_bytes(filepath.read_bytes())
            except Exception:
                pass

    def _read_text_guess(self, filepath: Path) -> str | None:
        for enc in ("utf-8-sig", "utf-16", "cp932", "shift_jis", "gbk"):
            try:
                return filepath.read_text(encoding=enc, errors="strict")
            except Exception:
                continue
        try:
            return filepath.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None

    def _repack_json(self, path: Path, items: list[TextItem]):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return
        for item in items:
            self._set_json_field(data, item.key, item.translated)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _set_json_field(self, data, key_path: str, value: str):
        import re
        parts = key_path.split(".")
        current = data
        for part in parts[:-1]:
            if part.startswith("[") and part.endswith("]"):
                current = current[int(part[1:-1])]
                continue
            m = re.match(r"^(.+)\[(\d+)\]$", part)
            if m:
                current = current[m.group(1)][int(m.group(2))]
            else:
                current = current[part]
        last = parts[-1]
        if last.startswith("[") and last.endswith("]"):
            current[int(last[1:-1])] = value
            return
        m = re.match(r"^(.+)\[(\d+)\]$", last)
        if m:
            current[m.group(1)][int(m.group(2))] = value
        else:
            current[last] = value

    def _repack_text(self, path: Path, items: list[TextItem]):
        content = self._read_text_guess(path) or ""
        lines = content.split("\n")
        for item in items:
            if 0 < item.line <= len(lines):
                if lines[item.line - 1].strip() == item.original:
                    lines[item.line - 1] = lines[item.line - 1].replace(item.original, item.translated, 1)
                elif item.original in lines[item.line - 1]:
                    lines[item.line - 1] = lines[item.line - 1].replace(item.original, item.translated, 1)
            elif item.original in content:
                content = content.replace(item.original, item.translated, 1)
        if any(0 < item.line <= len(lines) for item in items):
            content = "\n".join(lines)
        path.write_text(content, encoding="utf-8")

    def find_exe(self, path: Path) -> Path | None:
        return super().find_exe(path)


registry.register(GenericEngine())


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1]
    return value.replace(r"\"", '"').replace(r"\n", "\n").replace(r"\t", "\t")
