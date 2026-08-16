from __future__ import annotations

import json
import os
from pathlib import Path

from engines.base import TextItem
from utils.logger import debug, info


class TranslationCacheGenerator:
    """Generate translation files consumed by XUnity.AutoTranslator 5.x."""

    def __init__(self, game_dir: Path):
        self.game_dir = game_dir
        # XUAT resolves [Files] Directory=Translation\{Lang}\Text relative to BepInEx.
        self._cache_dir = game_dir / "BepInEx" / "Translation" / "zh" / "Text"
        self._legacy_cache_file = (
            game_dir / "BepInEx" / "Translation" / "zh" / "Translation_zh.txt"
        )

    @property
    def cache_file(self) -> Path:
        return self._cache_dir / "Translation_zh.txt"

    def generate(self, items: list[TextItem]) -> Path:
        entries = self._load_existing()
        for item in items:
            if item.translated and item.translated != item.original:
                entries[item.original] = _mark_xunity_translation(item.translated)

        self._write_entries(entries)
        info(f"XUnity translation cache generated: {self.cache_file} ({len(entries)} entries)")
        return self.cache_file

    def seed_builtin_dict(self) -> int:
        try:
            from utils.translation_server import _BUILTIN_JA_ZH
        except ImportError:
            return 0

        entries = self._load_existing()
        added = 0
        for source, translated in _BUILTIN_JA_ZH.items():
            if source not in entries:
                entries[source] = _mark_xunity_translation(translated)
                added += 1

        if added:
            self._write_entries(entries)
            debug(f"XUnity built-in cache seeded: +{added} entries")
        return added

    def sync_from_server_cache(self) -> int:
        server_cache_path = (
            Path.home() / "Downloads" / ".game_translator" / "proxy_cache.json"
        )
        if not server_cache_path.exists():
            return 0

        try:
            data = json.loads(server_cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            debug(f"Unable to read XUnity server cache: {exc}")
            return 0

        server_entries: dict[str, str] = {}
        for key, value in data.get("entries", {}).items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            if key.startswith("ja:zh:"):
                original = key[6:]
            elif key.startswith("en:zh:"):
                original = key[6:]
            else:
                continue
            translated = value.lstrip("\u180e")
            if original and translated and original != translated:
                server_entries[original] = translated

        if not server_entries:
            return 0

        entries = self._load_existing()
        added = 0
        for original, translated in server_entries.items():
            if original not in entries:
                entries[original] = translated
                added += 1

        if added:
            self._write_entries(entries)
            info(f"XUnity server cache synchronized: +{added} entries ({len(entries)} total)")
        return added

    def _load_existing(self) -> dict[str, str]:
        entries: dict[str, str] = {}
        # Load the obsolete location first so a valid current entry wins.
        for path in (self._legacy_cache_file, self.cache_file):
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError as exc:
                debug(f"Unable to read XUnity cache {path}: {exc}")
                continue
            for physical_line in text.splitlines():
                line = physical_line.lstrip("\ufeff")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parsed = self._decode_line(line)
                if parsed is None:
                    continue
                source, translated = parsed
                translated = translated.lstrip("\u180e")
                if source and translated and source != translated and translated != "__NOT_TRANSLATED__":
                    entries[source] = translated
        return entries

    def _write_entries(self, entries: dict[str, str]) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            f"{self._encode_text(source)}={self._encode_text(translated)}"
            for source, translated in sorted(
                entries.items(), key=lambda pair: len(pair[0]), reverse=True
            )
            if translated and translated != source
        ]
        tmp_path = Path(str(self.cache_file) + ".tmp")
        tmp_path.write_text("\n".join(lines), encoding="utf-8-sig")
        os.replace(tmp_path, self.cache_file)

    @staticmethod
    def _encode_text(text: str) -> str:
        """Match XUAT 5.6.1 TextHelper.EscapeNewlines exactly."""
        output: list[str] = []
        index = 0
        while index < len(text):
            char = text[index]
            if char == "/" and index + 1 < len(text) and text[index + 1] == "/":
                output.append("\\/\\/")
                index += 2
                continue
            if char == "\\":
                output.append("\\\\")
            elif char == "=":
                output.append("\\=")
            elif char == "\n":
                output.append("\\n")
            elif char == "\r":
                output.append("\\r")
            else:
                output.append(char)
            index += 1
        return "".join(output)

    @staticmethod
    def _decode_line(line: str) -> tuple[str, str] | None:
        fields = [""]
        escaped = False
        index = 0
        while index < len(line):
            char = line[index]
            if escaped:
                if char == "n":
                    fields[-1] += "\n"
                elif char == "r":
                    fields[-1] += "\r"
                elif char in {"=", "\\", "/"}:
                    fields[-1] += char
                else:
                    fields[-1] += "\\" + char
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "=" and len(fields) == 1:
                fields.append("")
            elif char == "=" or (char == "/" and index + 1 < len(line) and line[index + 1] == "/"):
                if char == "/":
                    break
                return None
            else:
                fields[-1] += char
            index += 1
        if escaped:
            fields[-1] += "\\"
        if len(fields) != 2:
            return None
        return fields[0], fields[1]


def _mark_xunity_translation(text: str) -> str:
    # XUAT handles redirected-resource markers itself; cache text must stay clean.
    return text
