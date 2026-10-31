"""TyranoScript/TyranoBuilder static translation engine."""

from __future__ import annotations

import shutil
from collections import defaultdict
from pathlib import Path

from engines.base import EngineBase, EngineCapabilities, TextItem
from engines.tyrano.archive import (
    find_packed_tyrano_exe,
    is_packed_tyrano_exe,
    is_tyrano_archive,
    patch_embedded_asar,
    patch_embedded_zip,
    read_archive_entries,
    scenario_entry_names,
)
from engines.tyrano.script import (
    TYRANO_SPAN_CONTRACT,
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
    support_level = "beta"
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
        packed_exe, markers = self._select_source(path)
        return packed_exe is not None or markers is not None

    def detect_confidence(self, path: Path) -> tuple[int, list[str]]:
        packed_exe, markers = self._select_source(path)
        if packed_exe is not None:
            if packed_exe.suffix.casefold() == ".asar":
                description = "找到 Electron app.asar TyranoScript 包"
            elif packed_exe.name.casefold() == "package.nw":
                description = "找到 NW.js package.nw TyranoScript 包"
            else:
                description = "找到 NW.js 内嵌 TyranoScript 包"
            return 99, [
                f"{description}: {packed_exe.name}",
                "包内包含 data/scenario、package.json 与 KAG 运行时",
            ]
        if markers is not None:
            return 99, [
                "找到 tyrano/plugins/kag 运行时",
                "找到 data/scenario 场景目录",
                f"找到 {markers[0].name} 与 {markers[1].name}",
            ]
        return 0, []

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        game_dir = self._game_root(path)
        original_dir = workspace / "original"
        original_dir.mkdir(parents=True, exist_ok=True)
        self._game_dir = game_dir

        packed_exe, markers = self._select_source(path)
        if path.is_file() and (
            path.suffix.casefold() in {".exe", ".asar"}
            or path.name.casefold() == "package.nw"
        ):
            if packed_exe is None and markers is None:
                raise ValueError("所选文件或所在目录没有可识别的 Tyrano 项目")
        self._packed_archive = packed_exe is not None
        self._packed_exe = packed_exe
        if packed_exe is not None:
            return self._unpack_embedded(packed_exe, original_dir)

        project_dir = markers[0].parent if markers is not None else game_dir
        scenario_dir = project_dir / "data" / "scenario"
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
        self._tyrano_staged_outputs: dict[str, Path] = {}
        translated = [item for item in items if item.translated and item.translated != item.original]
        grouped: dict[str, list[TextItem]] = defaultdict(list)
        for item in translated:
            grouped[str(item.meta.get("script_rel") or item.file)].append(item)
        if not grouped:
            warning("TyranoScript: 没有可回填的有效译文")
            return

        patched_files = 0
        patched_items = 0
        font_replacements = 0
        embedded_replacements: dict[str, dict[str, Path]] = defaultdict(dict)
        for rel, file_items in sorted(grouped.items()):
            target = self._workspace_source(workspace, rel, file_items[0])
            if not target.is_file():
                raise FileNotFoundError(f"TyranoScript: 回填源文件不存在: {rel}")
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
            archive_name = str(
                file_items[0].meta.get("packed_archive_name")
                or file_items[0].meta.get("packed_exe_name")
                or ""
            )
            staged_script = workspace / "tyrano_staged" / "entries" / Path(*Path(rel).parts)
            staged_script.parent.mkdir(parents=True, exist_ok=True)
            staged_script.write_bytes(encode_ks(updated, encoding))
            if archive_name:
                embedded_replacements[archive_name][rel] = staged_script
            else:
                self._tyrano_staged_outputs[rel] = staged_script.resolve()
            patched_files += 1
            patched_items += len(file_items)

        archive_verified = False
        if embedded_replacements:
            game_dir = Path(getattr(self, "_game_dir", ""))
            for archive_name, entry_replacements in embedded_replacements.items():
                source_archive = game_dir / Path(*Path(archive_name).parts)
                font_replacements += self._add_cjk_font_replacements(
                    source_archive, workspace, entry_replacements,
                )
                staged = workspace / "tyrano_staged" / Path(*Path(archive_name).parts)
                if source_archive.suffix.casefold() == ".asar":
                    result = patch_embedded_asar(source_archive, staged, entry_replacements)
                else:
                    result = patch_embedded_zip(source_archive, staged, entry_replacements)
                if not result.get("verified"):
                    staged.unlink(missing_ok=True)
                    raise ValueError("TyranoScript embedded archive verification failed")
                self._tyrano_staged_outputs[archive_name] = staged.resolve()
            archive_verified = True

        self._tyrano_repack_stats = {
            "patched_files": patched_files,
            "patched_items": patched_items,
            "font_replacements": font_replacements,
            "embedded_archive": bool(embedded_replacements),
            "archive_verified": archive_verified,
        }
        info(f"TyranoScript: 已精确回填 {patched_items} 条译文到 {patched_files} 个脚本")

    def _add_cjk_font_replacements(
        self, source_archive: Path, workspace: Path, replacements: dict[str, Path],
    ) -> int:
        """Replace Tyrano's Japanese font when Chinese output is being written."""
        target_lang = str(getattr(self, "_target_lang", "zh-CN") or "zh-CN").casefold()
        if not target_lang.startswith("zh"):
            return 0
        try:
            from core.open_source_fonts import find_source_han_sans

            font_source = find_source_han_sans()
        except Exception as exc:
            warning(f"Tyrano 中文字体检查失败，保留原字体: {exc}")
            return 0
        if font_source is None:
            warning("未找到开源中文字体，Tyrano 可能仍有简体字缺失")
            return 0

        candidates = [
            "data/others/JKG-M_3.ttf",
            "data/others/JKG-L_3.ttf",
        ]
        existing = read_archive_entries(source_archive, candidates)
        if not existing:
            return 0
        staged_font = workspace / "tyrano_staged" / "fonts" / font_source.name
        staged_font.parent.mkdir(parents=True, exist_ok=True)
        if not staged_font.exists() or staged_font.stat().st_size != font_source.stat().st_size:
            shutil.copy2(font_source, staged_font)
        for entry in existing:
            replacements[entry] = staged_font
        info(f"Tyrano: 已为中文译文准备 CJK 字体 ({len(existing)} 个字体条目)")
        return len(existing)

    def verify_repack(self, game_dir: Path, translated: list[TextItem], result: dict) -> dict:
        grouped: dict[str, list[TextItem]] = defaultdict(list)
        for item in translated:
            grouped[str(item.meta.get("script_rel") or item.file)].append(item)

        checked = 0
        hits = 0
        packed_name = next(
            (
                str(item.meta.get("packed_archive_name") or item.meta.get("packed_exe_name") or "")
                for item in translated
                if item.meta.get("packed_archive_name") or item.meta.get("packed_exe_name")
            ),
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

    @staticmethod
    def augment_checkpoint_items(items: list[TextItem], workspace: Path) -> int:
        """Add newly supported my_Item labels from the checkpoint's originals."""
        original_root = (Path(workspace) / "original").resolve()
        grouped: dict[tuple[str, str], TextItem] = {}
        existing = {
            (item.file, item.key, item.original)
            for item in items
            if getattr(item, "meta", {}).get("engine") == "tyrano"
        }
        for item in items:
            meta = getattr(item, "meta", {}) or {}
            script_rel = str(meta.get("script_rel") or "")
            source_rel = str(meta.get("source_rel") or "")
            if script_rel and source_rel:
                grouped.setdefault((script_rel, source_rel), item)

        additions: list[TextItem] = []
        for (script_rel, source_rel), template in grouped.items():
            source = (
                original_root / Path(*source_rel.replace("\\", "/").split("/"))
            ).resolve()
            if not source.is_relative_to(original_root) or not source.is_file():
                continue
            content, encoding = decode_ks(source.read_bytes())
            spans = extract_spans(
                content,
                require_kana=not is_japanese_visible_script(content),
            )
            meta = getattr(template, "meta", {}) or {}
            candidates = TyranoEngine._items_for_spans(
                item_file=template.file,
                script_rel=script_rel,
                source_rel=source_rel,
                spans=spans,
                encoding=encoding,
                source_kind=str(meta.get("source_kind") or "current"),
                packed_exe_name=str(meta.get("packed_exe_name") or ""),
                packed_archive_name=str(meta.get("packed_archive_name") or ""),
            )
            for candidate in candidates:
                if not str(candidate.meta.get("role") or "").startswith("variable:my_Item."):
                    continue
                key = (candidate.file, candidate.key, candidate.original)
                if key in existing:
                    continue
                existing.add(key)
                additions.append(candidate)

        items.extend(additions)
        return len(additions)

    def find_exe(self, path: Path) -> Path | None:
        from core.exe_selector import find_main_exes

        # Honour a program explicitly selected by the user, as the launcher does.
        if path.is_file() and path.suffix.lower() == ".exe":
            return path
        game_dir = self._game_root(path)
        if path.is_dir() and path.name.casefold() == "package.nw" and self._marker_paths(path):
            game_dir = path.parent
        packed_exe, markers = self._select_source(path)
        if packed_exe is not None and packed_exe.suffix.casefold() == ".exe":
            return packed_exe

        excluded = {"nwjc.exe", "notification_helper.exe", "crashpad_handler.exe"}
        external_package = (
            (packed_exe is not None and packed_exe.name.casefold() == "package.nw")
            or (packed_exe is not None and packed_exe.suffix.casefold() == ".asar")
            or (markers is not None and markers[0].parent.name.casefold() == "package.nw")
        )

        def usable(candidate: Path) -> bool:
            if candidate.name.casefold() in excluded:
                return False
            # A different self-contained game will not load this package.nw.
            return not (external_package and is_packed_tyrano_exe(candidate))

        for recursive in (False, True):
            for candidate in find_main_exes(game_dir, recursive=recursive):
                if usable(candidate):
                    return candidate

        fallback = super().find_exe(path)
        return fallback if fallback and usable(fallback) else None

    def _unpack_embedded(self, packed_archive: Path, original_dir: Path) -> list[TextItem]:
        script_names = scenario_entry_names(packed_archive)
        archive_name = packed_archive.relative_to(self._game_dir).as_posix()
        requested = script_names + [f"{name}.bak" for name in script_names]
        entries = read_archive_entries(packed_archive, requested)
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
                item_file=archive_name,
                script_rel=script_rel,
                source_rel=source_rel,
                spans=spans,
                encoding=encoding,
                source_kind=source_kind,
                packed_exe_name=packed_archive.name,
                packed_archive_name=archive_name,
            ))

        self._tyrano_extract_stats = {
            "files_scanned": len(script_names),
            "items": len(items),
            "packed_archive": archive_name,
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

    @classmethod
    def _select_source(cls, path: Path) -> tuple[Path | None, tuple[Path, Path] | None]:
        """Use one consistent source for detection, extraction, and launch."""
        game_dir = cls._game_root(path)
        if path.is_file() and path.name.casefold() == "package.nw":
            return (path, None) if is_tyrano_archive(path) else (None, None)
        if path.is_file() and path.suffix.casefold() == ".asar":
            return (path, None) if is_tyrano_archive(path) else (None, None)
        if path.is_file() and path.suffix.lower() == ".exe":
            # An explicit choice may not be replaced by a different packed game.
            if is_packed_tyrano_exe(path):
                return path, None
            return cls._select_external_source(game_dir)

        packed_exe = find_packed_tyrano_exe(game_dir)
        if packed_exe is not None:
            return packed_exe, None
        return cls._select_external_source(game_dir)

    @classmethod
    def _select_external_source(cls, game_dir: Path) -> tuple[Path | None, tuple[Path, Path] | None]:
        package = game_dir / "package.nw"
        if is_tyrano_archive(package):
            return package, None
        if package.is_dir():
            markers = cls._marker_paths(package)
            if markers is not None:
                return None, markers
        for asar in (game_dir / "resources" / "app.asar", game_dir / "app.asar"):
            if is_tyrano_archive(asar):
                return asar, None
        return None, cls._marker_paths(game_dir)

    @staticmethod
    def _game_root(path: Path) -> Path:
        if path.is_dir():
            return path
        if path.suffix.casefold() == ".asar" and path.parent.name.casefold() == "resources":
            return path.parent.parent
        return path.parent

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
        packed_archive_name: str = "",
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
                    "translation_contract": TYRANO_SPAN_CONTRACT,
                    **({"kind": "speaker"} if span.role == "speaker" else {}),
                    "speaker": span.speaker,
                    "span_start": span.start,
                    "span_end": span.end,
                    "encoding": encoding,
                    "source_kind": source_kind,
                    "script_rel": script_rel,
                    "source_rel": source_rel,
                    "packed_exe_name": packed_exe_name,
                    "packed_archive_name": packed_archive_name or packed_exe_name,
                },
            ))
        return result

    def _workspace_source(self, workspace: Path, rel: str, item: TextItem) -> Path:
        source_rel = str(item.meta.get("source_rel") or rel)
        target = workspace / "original" / source_rel
        if target.exists():
            return target
        if item.meta.get("packed_archive_name") or item.meta.get("packed_exe_name"):
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
