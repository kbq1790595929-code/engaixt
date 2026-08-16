"""BGI / Ethornell / Buriko General Interpreter engine."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from engines.base import EngineBase, EngineCapabilities, TextItem, registry
from utils.arc20 import build_arc20, parse_arc20
from utils.bgi_packfile import build_packfile, parse_packfile
from utils.bgi_dsc import (
    BGI_V1_MAGIC,
    DSC_MAGIC,
    SjisTunnelEncoder,
    compress_dsc,
    decompress_dsc,
    extract_bgi_strings,
    get_dsc_key,
    inspect_bgi_v0_script,
    inspect_bgi_v1_raw_script,
    patch_bgi_script,
)
from utils.bgi_slot_rewrite import (
    build_bgi_tunnel_table_for_translations,
    collect_bgi_slot_rewrite_candidates,
    rewrite_bgi_slots_sync,
)
from utils.logger import info, warning


class BGIEngine(EngineBase):
    name = "bgi"
    label = "BGI / Ethornell"
    support_level = "beta"
    supports_extract = True
    supports_repack = True
    capabilities = EngineCapabilities(
        extract=True,
        repack=True,
        static_patch=True,
        runtime_patch=True,
        creates_launcher=True,
        portable_after_patch=True,
        requires_python=False,
        requires_frida=False,
        notes=(
            "静态脚本回填后生成原生汉化启动器。",
            "中文显示依赖 sjis_ext.bin 与原生 GDI hook 运行文件。",
        ),
    )
    detect_priority = 60
    limitations = [
        "Beta 版：已支持脚本提取、静态回填与原生免 Python/Frida 启动器。",
        "已支持 ARC20/PackFile 与 DSC 脚本处理，并带封包尺寸保护。",
        "中文显示使用 sjis_ext.bin 字符隧道和原生 GDI 显示 hook。",
    ]

    def detect(self, path: Path) -> bool:
        game_dir = path if path.is_dir() else path.parent
        has_arcs = bool(list(game_dir.rglob("*.arc")))
        has_gdb = (game_dir / "BGI.gdb").exists()
        has_system = (game_dir / "system.arc").exists() or (game_dir / "sysprg.arc").exists()
        return has_arcs and (has_gdb or has_system)

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        game_dir = path if path.is_dir() else path.parent
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "bgi_game_dir.txt").write_text(str(game_dir), encoding="utf-8")

        items: list[TextItem] = []
        arc_count = 0
        script_count = 0

        for arc_file in _iter_likely_script_arcs(game_dir):
            try:
                archive_kind, entries = _read_bgi_archive(_source_bgi_arc_path(arc_file))
            except Exception:
                continue
            if not entries:
                continue
            arc_count += 1
            for entry_name, _offset, _size, data in entries:
                refs = extract_bgi_strings(data)
                if not refs:
                    continue
                script_count += 1
                items.extend(
                    _items_from_bgi_refs(
                        game_dir,
                        arc_file,
                        archive_kind,
                        entry_name,
                        refs,
                    )
                )

        _add_context(items)
        choice_count = sum(1 for item in items if item.context == "choice")
        if choice_count:
            info(f"BGI: detected {choice_count} scenario choice texts")
        if not items:
            warning("BGI: no Japanese script text extracted; current BGI static patch supports Japanese originals only")
        info(f"BGI: scanned {arc_count} script ARC candidates, extracted {len(items)} texts from {script_count} scripts")
        return items

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        game_dir = Path(getattr(self, "_game_dir", "")) if getattr(self, "_game_dir", None) else None
        if not game_dir or not game_dir.exists():
            marker = workspace / "bgi_game_dir.txt"
            if marker.exists():
                game_dir = Path(marker.read_text(encoding="utf-8").strip())
        if not game_dir or not game_dir.exists():
            raise RuntimeError("BGI repack needs a valid game directory")

        translations: dict[str, str] = {}
        translations_by_entry: dict[tuple[str, str], dict[str, str]] = {}
        for item in items:
            translated = (item.translated or "").strip()
            if translated and translated != item.original:
                translations[item.original] = translated
                arc = _item_bgi_arc(item)
                entry = _item_bgi_entry(item)
                if arc and entry:
                    translations_by_entry.setdefault((arc, entry), {})[item.original] = translated
        if not translations:
            info("BGI: no translated items to repack")
            return
        if not translations_by_entry:
            warning("BGI: translated items have no script entry metadata; skipped unsafe global repack")
            return

        arcs = _group_items_by_arc(items)
        active_arcs = [
            (arc_rel, arc_items)
            for arc_rel, arc_items in sorted(arcs.items())
            if any((arc_rel, _item_bgi_entry(item) or "") in translations_by_entry for item in arc_items)
        ]
        info(
            "BGI: 正在回填脚本文本。此阶段需要解包 ARC、改写脚本槽位、重新压缩 DSC 并重建封包，"
            f"共 {len(active_arcs)} 个脚本包；文本较多时可能需要几分钟，请不要关闭工具。"
        )
        encoder = SjisTunnelEncoder()
        touched = 0
        total_scripts = 0
        total_refs = 0
        total_patch_stats = {"direct": 0, "compressed": 0, "ai_rewritten": 0, "appended": 0, "skipped_too_long": 0}
        skipped_details: list[dict[str, object]] = []
        slot_rewrites: dict[tuple[str, int], str] = {}
        max_arc_ratio = 1.20
        max_script_ratio = 1.25
        arc_paths = [
            _source_bgi_arc_path(game_dir / arc_rel)
            for arc_rel, _arc_items in active_arcs
            if (game_dir / arc_rel).exists()
        ]
        warmed_table = build_bgi_tunnel_table_for_translations(
            arc_paths,
            game_dir,
            translations,
        )
        encoder = SjisTunnelEncoder(warmed_table)

        if getattr(self, "_enable_ai_slot_rewrite", False):
            candidates = collect_bgi_slot_rewrite_candidates(
                arc_paths,
                game_dir,
                translations,
                warmed_table,
            )
            info(f"BGI: AI slot rewrite candidates: {len(candidates)}")
            slot_rewrites, ai_report = rewrite_bgi_slots_sync(
                candidates,
                table=warmed_table,
                cache_path=workspace / "bgi_ai_slot_rewrite_cache.json",
                model=getattr(self, "_ai_slot_model", "deepseek-chat"),
            )
            if slot_rewrites:
                warmed = SjisTunnelEncoder(warmed_table)
                for value in slot_rewrites.values():
                    warmed.encode(value)
                encoder = warmed
            (workspace / "bgi_ai_slot_rewrite_report.json").write_text(
                json.dumps(ai_report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            info(f"BGI: AI slot rewrite accepted: {len(slot_rewrites)}")

        for arc_index, (arc_rel, arc_items) in enumerate(active_arcs, 1):
            arc_path = game_dir / arc_rel
            if not arc_path.exists():
                warning(f"BGI: ARC not found, skipped: {arc_rel}")
                continue

            info(f"BGI: 正在回填脚本包 {arc_index}/{len(active_arcs)}: {arc_rel}")
            source_arc_path = _source_bgi_arc_path(arc_path)
            archive_kind, original_entries = _read_bgi_archive(source_arc_path)
            if not original_entries:
                continue

            new_entries: list[tuple[str, bytes]] = []
            patched_entry_names: set[str] = set()
            arc_scripts = 0
            arc_refs = 0
            for name, _offset, _size, data in original_entries:
                entry_translations = translations_by_entry.get((str(arc_rel).replace("\\", "/"), name), {})
                if not entry_translations:
                    new_entries.append((name, data))
                    continue

                was_dsc = data.startswith(DSC_MAGIC)
                dsc_key = get_dsc_key(data)
                script = decompress_dsc(data)
                is_v0_script = inspect_bgi_v0_script(script, require_japanese=False, include_empty=True) is not None
                is_raw_v1_script = inspect_bgi_v1_raw_script(script, require_japanese=False, include_empty=True) is not None
                if script.startswith(BGI_V1_MAGIC) or is_raw_v1_script or is_v0_script:
                    patched, stats = patch_bgi_script(
                        script,
                        entry_translations,
                        encoder,
                        allow_append=is_raw_v1_script or is_v0_script,
                        slot_rewrites=slot_rewrites,
                    )
                    if stats.patched:
                        script_ratio = len(patched) / max(1, len(script))
                        script_ratio_limit = 4.00 if is_raw_v1_script or is_v0_script else max_script_ratio
                        if script_ratio > script_ratio_limit:
                            raise RuntimeError(
                                f"BGI: {arc_rel}::{name} decompressed script grew to "
                                f"{script_ratio:.2f}x ({len(script)} -> {len(patched)})"
                            )
                        data = compress_dsc(patched, key=dsc_key) if was_dsc else patched
                        patched_entry_names.add(name)
                        arc_scripts += 1
                        arc_refs += stats.patched
                        total_patch_stats["direct"] += stats.direct
                        total_patch_stats["compressed"] += stats.compressed
                        total_patch_stats["ai_rewritten"] += stats.ai_rewritten
                        total_patch_stats["appended"] += stats.appended
                        total_patch_stats["skipped_too_long"] += stats.skipped_too_long
                    for detail in stats.skipped_details:
                        row = dict(detail)
                        row["arc"] = str(arc_rel)
                        row["entry"] = name
                        skipped_details.append(row)
                new_entries.append((name, data))

            if arc_refs == 0:
                continue

            patched_arc = _build_bgi_archive(archive_kind, new_entries)
            arc_ratio = len(patched_arc) / max(1, source_arc_path.stat().st_size)
            arc_ratio_limit = 4.00 if archive_kind == "packfile" else max_arc_ratio
            if arc_ratio > arc_ratio_limit:
                raise RuntimeError(
                    f"BGI: {arc_rel} rebuilt ARC grew to {arc_ratio:.2f}x "
                    f"({source_arc_path.stat().st_size} -> {len(patched_arc)})"
                )
            _verify_bgi_arc(patched_arc, arc_rel, patched_entry_names)

            backup = arc_path.with_suffix(arc_path.suffix + ".pre_tool")
            if not backup.exists():
                shutil.copy2(arc_path, backup)
            arc_path.write_bytes(patched_arc)
            touched += 1
            total_scripts += arc_scripts
            total_refs += arc_refs
            info(f"BGI: repacked {arc_rel} -> {arc_scripts} scripts, {arc_refs} refs, {arc_ratio:.2f}x")

        sjis_ext = game_dir / "sjis_ext.bin"
        sjis_ext.write_bytes(encoder.mapping_table())
        skipped_report = workspace / "bgi_static_skipped.json"
        skipped_report.write_text(
            json.dumps(
                {
                    "count": len(skipped_details),
                    "items": skipped_details,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        info(f"BGI: SJIS tunnel table written: {sjis_ext.name} ({len(encoder.mapping_table()) // 2} chars)")
        info(f"BGI: static skipped report written: {skipped_report} ({len(skipped_details)} items)")
        info(
            "BGI: repack summary: "
            f"{touched} ARC, {total_scripts} scripts, {total_refs} refs, "
            f"direct={total_patch_stats['direct']}, "
            f"ai_rewritten={total_patch_stats['ai_rewritten']}, "
            f"compressed={total_patch_stats['compressed']}, "
            f"appended={total_patch_stats['appended']}, "
            f"skipped_too_long={total_patch_stats['skipped_too_long']}"
        )


def _iter_likely_script_arcs(game_dir: Path) -> list[Path]:
    arcs: list[Path] = []
    archive_dir = game_dir / "Archive"
    candidates = sorted(game_dir.glob("*.arc"))
    if archive_dir.exists():
        candidates += sorted(archive_dir.glob("*.arc"))

    for arc in candidates:
        name = arc.name.lower()
        if name in {"system.arc", "sysprg.arc"}:
            arcs.append(arc)
            continue
        stem_digits = "".join(ch for ch in arc.stem if ch.isdigit())
        number = int(stem_digits) if stem_digits else -1
        if 1000 <= number < 1800:
            arcs.append(arc)
            continue
        try:
            if arc.stat().st_size < 2_000_000:
                arcs.append(arc)
        except OSError:
            pass
    return arcs


def _read_bgi_archive(arc_path: Path) -> tuple[str, list[tuple[str, int, int, bytes]]]:
    data = arc_path.read_bytes()
    entries = parse_arc20(data)
    if entries:
        return "arc20", entries
    entries = parse_packfile(data)
    if entries:
        return "packfile", entries
    return "unknown", []


def _build_bgi_archive(kind: str, entries: list[tuple[str, bytes]]) -> bytes:
    if kind == "arc20":
        return build_arc20(entries)
    if kind == "packfile":
        return build_packfile(entries)
    raise RuntimeError(f"BGI: unsupported archive kind: {kind}")


def _source_bgi_arc_path(arc_path: Path) -> Path:
    backup = arc_path.with_suffix(arc_path.suffix + ".pre_tool")
    if backup.exists():
        return backup
    return arc_path


def _arc_rel(game_dir: Path, arc_file: Path, entry_name: str) -> str:
    return f"{arc_file.relative_to(game_dir).as_posix()}::{entry_name}"


def _item_bgi_arc(item: TextItem) -> str:
    arc = item.meta.get("arc") if getattr(item, "meta", None) else None
    if not arc and "::" in item.file:
        arc = item.file.split("::", 1)[0]
    return str(arc).replace("\\", "/") if arc else ""


def _item_bgi_entry(item: TextItem) -> str:
    entry = item.meta.get("entry") if getattr(item, "meta", None) else None
    if not entry and "::" in item.file:
        entry = item.file.split("::", 1)[1]
    return str(entry) if entry else ""


def _items_from_bgi_refs(
    game_dir: Path,
    arc_file: Path,
    archive_kind: str,
    entry_name: str,
    refs,
    *,
    multilingual_fallback: bool = False,
) -> list[TextItem]:
    rel = _arc_rel(game_dir, arc_file, entry_name)
    items: list[TextItem] = []
    for idx, ref in enumerate(refs):
        meta = {
            "source_game_dir": str(game_dir),
            "arc": str(arc_file.relative_to(game_dir)),
            "archive_kind": archive_kind,
            "entry": entry_name,
            "operand_offset": ref.operand_offset,
            "text_offset": ref.text_offset,
            "kind": ref.kind,
        }
        if multilingual_fallback:
            meta["multilingual_fallback"] = True
        items.append(
            TextItem(
                file=rel,
                key=f"{ref.operand_offset:x}:{idx}:{ref.kind}",
                original=ref.text,
                context=ref.kind,
                meta=meta,
            )
        )
    return items


def _group_items_by_arc(items: list[TextItem]) -> dict[str, list[TextItem]]:
    grouped: dict[str, list[TextItem]] = {}
    for item in items:
        arc = _item_bgi_arc(item)
        if not arc:
            continue
        grouped.setdefault(arc, []).append(item)
    return grouped


def _verify_bgi_arc(data: bytes, arc_rel: str, patched_entry_names: set[str] | None = None) -> None:
    entries = parse_arc20(data) or parse_packfile(data)
    if not entries:
        raise RuntimeError(f"BGI: rebuilt ARC cannot be parsed: {arc_rel}")
    patched_entry_names = patched_entry_names or set()
    for name, _offset, _size, entry_data in entries:
        if not entry_data.startswith(DSC_MAGIC):
            continue
        try:
            script = decompress_dsc(entry_data)
        except Exception as exc:
            raise RuntimeError(f"BGI: rebuilt DSC cannot be decompressed: {arc_rel}::{name}") from exc
        if patched_entry_names and name not in patched_entry_names:
            continue
        if (
            script.startswith(BGI_V1_MAGIC)
            or inspect_bgi_v1_raw_script(script, require_japanese=False, include_empty=True)
            or inspect_bgi_v0_script(script, require_japanese=False, include_empty=True)
        ):
            continue
        if patched_entry_names:
            raise RuntimeError(f"BGI: rebuilt patched DSC script cannot be parsed: {arc_rel}::{name}")


def _add_context(items: list[TextItem]) -> None:
    by_file: dict[str, list[TextItem]] = {}
    for item in items:
        by_file.setdefault(item.file, []).append(item)
    for group in by_file.values():
        for idx, item in enumerate(group):
            if idx > 0:
                item.meta["prev_text"] = group[idx - 1].original
            if idx + 1 < len(group):
                item.meta["next_text"] = group[idx + 1].original


registry.register(BGIEngine())
