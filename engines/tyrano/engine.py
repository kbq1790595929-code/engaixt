"""TyranoScript/TyranoBuilder static translation engine."""

from __future__ import annotations

import shutil
from collections import defaultdict
from pathlib import Path

from engines.base import EngineBase, EngineCapabilities, TextItem
from engines.tyrano.archive import (
    find_packed_tyrano_exe,
    patch_embedded_zip,
    read_archive_entries,
    scenario_entry_names,
)
from engines.tyrano.script import (
    apply_span_translations,
    decode_ks,
    encode_ks,
    extract_spans,
    is_japanese_visible_script,
)
from utils.logger import info, warning


class TyranoEngine(EngineBase):
    name = "tyrano"
    label = "TyranoScript / TyranoBuilder"
    support_level = "experimental"
    detect_priority = 99
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
            "结构化处理 data/scenario 下的 TyranoScript .ks 场景。",
            "优先使用同名 .ks.bak 日文原稿，避免把已有中文再次翻译。",
            "只替换可见文本 span，不修改脚本标签、资源路径和表达式。",
        ),
    )
    limitations = [
        "没有日文备份的既有中文脚本无法还原成日文原稿。",
        "自定义插件中通过 JavaScript 动态拼接的文本暂不做静态回填。",
    ]

    def detect(self, path: Path) -> bool:
        game_dir = path if path.is_dir() else path.parent
        return self._marker_paths(game_dir) is not None or find_packed_tyrano_exe(game_dir) is not None

    def detect_confidence(self, path: Path) -> tuple[int, list[str]]:
        game_dir = path if path.is_dir() else path.parent
        markers = self._marker_paths(game_dir)
        if markers is not None:
            return 99, [
                "找到 tyrano/plugins/kag 运行时",
                "找到 data/scenario 场景目录",
                f"找到 {markers[0].name} 与 {markers[1].name}",
            ]
        packed_exe = find_packed_tyrano_exe(game_dir)
        if packed_exe is not None:
            return 99, [
                f"找到 NW.js 内嵌 TyranoScript 包: {packed_exe.name}",
                "内嵌包包含 data/scenario、package.json 与 KAG 运行时",
            ]
        return 0, []

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        game_dir = path if path.is_dir() else path.parent
        scenario_dir = game_dir / "data" / "scenario"
        original_dir = workspace / "original"
        original_dir.mkdir(parents=True, exist_ok=True)
        self._game_dir = game_dir

        packed_exe = find_packed_tyrano_exe(game_dir)
        self._packed_archive = packed_exe is not None
        self._packed_exe = packed_exe
        if packed_exe is not None:
            return self._unpack_embedded(packed_exe, original_dir)

        items: list[TextItem] = []
        source_counts = {"bak": 0, "current": 0, "skipped_existing_translation": 0}
        files_scanned = 0
        for target in sorted(scenario_dir.rglob("*.ks"), key=lambda value: str(value).casefold()):
            if self._skip_script(target, scenario_dir):
                continue
            files_scanned += 1
            backup = Path(str(target) + ".bak")
            current_content, current_encoding = decode_ks(target.read_bytes())
            source = target
            source_kind = "current"
            content = current_content
            encoding = current_encoding
            if not is_japanese_visible_script(current_content) and backup.is_file():
                backup_content, backup_encoding = decode_ks(backup.read_bytes())
                if is_japanese_visible_script(backup_content):
                    source = backup
                    source_kind = "bak"
                    content = backup_content
                    encoding = backup_encoding
            require_kana = not is_japanese_visible_script(content)
            rel = target.relative_to(game_dir).as_posix()
            workspace_target = original_dir / rel
            workspace_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, workspace_target)

            spans = extract_spans(content, require_kana=require_kana)
            if source_kind == "current" and not spans:
                source_counts["skipped_existing_translation"] += 1
                continue
            source_counts[source_kind] += 1
            items.extend(self._items_for_spans(
                item_file=rel,
                script_rel=rel,
                source_rel=rel,
                spans=spans,
                encoding=encoding,
                source_kind=source_kind,
            ))

        self._tyrano_extract_stats = {
            "files_scanned": files_scanned,
            "items": len(items),
            **source_counts,
        }
        info(
            "TyranoScript: "
            f"扫描 {files_scanned} 个场景脚本，提取 {len(items)} 条日文可见文本；"
            f"日文备份 {source_counts['bak']} 个，当前日文脚本 {source_counts['current']} 个，"
            f"跳过无日文原稿脚本 {source_counts['skipped_existing_translation']} 个"
        )
        return items

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        translated = [item for item in items if item.translated and item.translated != item.original]
        grouped: dict[str, list[TextItem]] = defaultdict(list)
        for item in translated:
            grouped[str(item.meta.get("script_rel") or item.file)].append(item)
        if not grouped:
            warning("TyranoScript: 没有可回填的有效译文")
            return

        patched_files = 0
        patched_items = 0
        embedded_replacements: dict[str, Path] = {}
        for rel, file_items in sorted(grouped.items()):
            target = self._workspace_source(workspace, rel, file_items[0])
            if not target.is_file():
                warning(f"TyranoScript: 回填源文件不存在: {rel}")
                continue
            content, detected_encoding = decode_ks(target.read_bytes())
            encoding = str(file_items[0].meta.get("encoding") or detected_encoding)
            replacements = [
                (
                    int(item.meta["span_start"]),
                    int(item.meta["span_end"]),
                    item.original,
                    item.translated,
                )
                for item in file_items
            ]
            before_tag_count = content.count("[")
            updated = apply_span_translations(content, replacements)
            if updated.count("[") != before_tag_count:
                raise ValueError(f"TyranoScript tag structure changed during repack: {rel}")
            target.write_bytes(encode_ks(updated, encoding))
            if file_items[0].meta.get("packed_exe_name"):
                embedded_replacements[rel] = target
            patched_files += 1
            patched_items += len(file_items)

        archive_verified = False
        if embedded_replacements:
            packed_name = str(translated[0].meta.get("packed_exe_name") or "")
            packed_exe = Path(getattr(self, "_game_dir", "")) / packed_name
            output_tmp = packed_exe.with_name(packed_exe.name + ".engaixt_tmp")
            result = patch_embedded_zip(packed_exe, output_tmp, embedded_replacements)
            if not result.get("verified"):
                output_tmp.unlink(missing_ok=True)
                raise ValueError("TyranoScript embedded ZIP verification failed")
            output_tmp.replace(packed_exe)
            archive_verified = True

        self._tyrano_repack_stats = {
            "patched_files": patched_files,
            "patched_items": patched_items,
            "embedded_archive": bool(embedded_replacements),
            "archive_verified": archive_verified,
        }
        info(f"TyranoScript: 已精确回填 {patched_items} 条译文到 {patched_files} 个脚本")

    def verify_repack(self, game_dir: Path, translated: list[TextItem], result: dict) -> dict:
        grouped: dict[str, list[TextItem]] = defaultdict(list)
        for item in translated:
            grouped[str(item.meta.get("script_rel") or item.file)].append(item)

        checked = 0
        hits = 0
        packed_name = next(
            (str(item.meta.get("packed_exe_name") or "") for item in translated if item.meta.get("packed_exe_name")),
            "",
        )
        if packed_name:
            executable = game_dir / packed_name
            payloads = read_archive_entries(executable, list(grouped))
            for rel, file_items in grouped.items():
                data = payloads.get(rel)
                if data is None:
                    continue
                content, _encoding = decode_ks(data)
                checked += 1
                hits += sum(1 for item in file_items if item.translated in content)
        else:
            for rel, file_items in grouped.items():
                target = game_dir / rel
                if not target.is_file():
                    continue
                content, _encoding = decode_ks(target.read_bytes())
                checked += 1
                hits += sum(1 for item in file_items if item.translated in content)

        result.update({
            "checked": checked > 0,
            "hits": hits,
            "files_checked": checked,
            "archive_verified": bool(
                getattr(self, "_tyrano_repack_stats", {}).get("archive_verified", False)
            ),
            "note": "TyranoScript 场景条目解包复核",
        })
        return result

    def find_exe(self, path: Path) -> Path | None:
        game_dir = path if path.is_dir() else path.parent
        excluded = {"nwjc.exe", "notification_helper.exe", "crashpad_handler.exe"}
        candidates = [
            candidate
            for candidate in game_dir.glob("*.exe")
            if candidate.name.casefold() not in excluded
        ]
        if candidates:
            return max(candidates, key=lambda candidate: candidate.stat().st_size)
        return super().find_exe(path)

    def _unpack_embedded(self, packed_exe: Path, original_dir: Path) -> list[TextItem]:
        script_names = scenario_entry_names(packed_exe)
        requested = script_names + [f"{name}.bak" for name in script_names]
        entries = read_archive_entries(packed_exe, requested)
        items: list[TextItem] = []
        source_counts = {"bak": 0, "current": 0, "skipped_existing_translation": 0}

        for script_rel in script_names:
            current_data = entries.get(script_rel)
            if current_data is None:
                continue
            content, encoding = decode_ks(current_data)
            source_data = current_data
            source_kind = "current"
            backup_data = entries.get(f"{script_rel}.bak")
            if not is_japanese_visible_script(content) and backup_data is not None:
                backup_content, backup_encoding = decode_ks(backup_data)
                if is_japanese_visible_script(backup_content):
                    content = backup_content
                    encoding = backup_encoding
                    source_data = backup_data
                    source_kind = "bak"

            require_kana = not is_japanese_visible_script(content)
            spans = extract_spans(content, require_kana=require_kana)
            source_rel = f"__tyrano_embedded__/{script_rel}"
            workspace_target = original_dir / source_rel
            workspace_target.parent.mkdir(parents=True, exist_ok=True)
            workspace_target.write_bytes(source_data)
            if source_kind == "current" and not spans:
                source_counts["skipped_existing_translation"] += 1
                continue
            source_counts[source_kind] += 1
            items.extend(self._items_for_spans(
                item_file=packed_exe.name,
                script_rel=script_rel,
                source_rel=source_rel,
                spans=spans,
                encoding=encoding,
                source_kind=source_kind,
                packed_exe_name=packed_exe.name,
            ))

        self._tyrano_extract_stats = {
            "files_scanned": len(script_names),
            "items": len(items),
            "packed_exe": packed_exe.name,
            **source_counts,
        }
        info(
            "TyranoScript 内嵌包: "
            f"扫描 {len(script_names)} 个场景脚本，提取 {len(items)} 条日文可见文本；"
            f"当前日文脚本 {source_counts['current']} 个，备份原稿 {source_counts['bak']} 个"
        )
        return items

    @staticmethod
    def _marker_paths(game_dir: Path) -> tuple[Path, Path] | None:
        scenario = game_dir / "data" / "scenario"
        kag = game_dir / "tyrano" / "plugins" / "kag" / "kag.tag_system.js"
        if not scenario.is_dir() or not kag.is_file():
            return None
        index = game_dir / "index.html"
        package = game_dir / "package.json"
        if not index.is_file() or not package.is_file():
            return None
        return index, package

    @staticmethod
    def _skip_script(path: Path, scenario_dir: Path) -> bool:
        rel_parts = {part.casefold() for part in path.relative_to(scenario_dir).parts}
        return bool(rel_parts & {"node_modules", "_translation_meta"})

    @staticmethod
    def _items_for_spans(
        *,
        item_file: str,
        script_rel: str,
        source_rel: str,
        spans,
        encoding: str,
        source_kind: str,
        packed_exe_name: str = "",
    ) -> list[TextItem]:
        result: list[TextItem] = []
        for index, span in enumerate(spans):
            previous = spans[index - 1].text if index > 0 else ""
            following = spans[index + 1].text if index + 1 < len(spans) else ""
            context_parts = []
            if span.speaker:
                context_parts.append(f"speaker: {span.speaker}")
            if previous:
                context_parts.append(f"previous: {previous[:160]}")
            if following:
                context_parts.append(f"next: {following[:160]}")
            result.append(TextItem(
                file=item_file,
                key=f"{script_rel}:span:{span.start}:{span.end}:{span.role}",
                original=span.text,
                context="\n".join(context_parts),
                line=span.line,
                meta={
                    "engine": "tyrano",
                    "role": span.role,
                    "speaker": span.speaker,
                    "span_start": span.start,
                    "span_end": span.end,
                    "encoding": encoding,
                    "source_kind": source_kind,
                    "script_rel": script_rel,
                    "source_rel": source_rel,
                    "packed_exe_name": packed_exe_name,
                },
            ))
        return result

    def _workspace_source(self, workspace: Path, rel: str, item: TextItem) -> Path:
        source_rel = str(item.meta.get("source_rel") or rel)
        target = workspace / "original" / source_rel
        if target.exists():
            return target
        if item.meta.get("packed_exe_name"):
            return target
        game_dir = Path(getattr(self, "_game_dir", ""))
        source = game_dir / rel
        if item.meta.get("source_kind") == "bak":
            backup = Path(str(source) + ".bak")
            if backup.is_file():
                source = backup
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return target
