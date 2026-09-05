"""KiriKiriEngine 引擎类：检测、静态提取、回填与运行时补丁部署。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import zipfile
from pathlib import Path

from engines.base import EngineBase, EngineCapabilities, TextItem
from utils.kirikiri_psb import (
    PsbFormatError,
    extract_kirikiri_scn_texts_and_storage_refs,
    is_kirikiri_scn,
    patch_kirikiri_scn_texts,
)
from utils.logger import debug, info, warning
from utils.text_extract import is_translatable

from engines.kirikiri import dump_targets as _dump_targets_mod
from engines.kirikiri import garbro as _garbro_mod
from engines.kirikiri.codec import (
    _KirikiriSjisTunnelEncoder,
    _count_files,
    _count_kirikiri_script_files,
    _decode_xp3_script_filter_for_static_extract,
    _detect_xp3_static_filter_schemes,
    _is_probably_binary_script,
    _is_probably_protected_text_script,
    _is_probably_scriptless_kirikiri_payload,
    _kirikiri_patch_text_value,
    _kirikiri_should_use_ascii_placeholder,
    _kirikiri_should_use_sjis_tunnel,
    _load_kirikiri_sjis_tunnel_encoder,
    _norm_rel,
    _read_text_guess,
    _safe_output_path,
    _try_decode_xp3_single_byte_xor_filter,
    _write_kirikiri_placeholder_map,
    _write_kirikiri_sjis_tunnel_table,
    _write_text,
    _xp3_may_contain_script_markers,
)
from engines.kirikiri.diagnostics import (
    _ContentSignature,
    _ExtractionDiagnosis,
    _ProtectionLayer,
    _classify_xp3_payload,
    _diagnose_xp3_archive,
    _diagnosis_to_dict,
    _primary_xp3_diagnosis,
    _shannon_entropy,
)
from engines.kirikiri.dump_targets import (
    _append_kirikiri_dump_targets_from_entries,
    _append_kirikiri_dump_targets_from_names,
    _expand_kirikiri_storage_refs,
    _write_kirikiri_explicit_dump_targets,
)
from engines.kirikiri.garbro import (
    _ExternalToolProbe,
    _extract_garbro_entries,
    _garbro_script_entries,
)
from engines.kirikiri.external_tools import (
    find_vntextpatch_json,
    load_vntextpatch_json_items,
    promote_script_files,
    run_msg_tool_unpack,
    run_vntextpatch_import,
    run_vntextpatch_export,
    run_xp3pack,
    write_vntextpatch_translation_json,
)
from engines.kirikiri.extract_progress import (
    archive_index_message,
    archive_script_message,
    emit_extract_progress,
    external_tool_message,
    script_parse_message,
    should_report_item,
)
from engines.kirikiri.patch_policy import changed_items_require_stream_bridge
from engines.kirikiri.spans import (
    _allow_plain_kag_script,
    _clean_runtime_capture_text,
    _extract_kirikiri_text_spans,
    _is_kirikiri_control_text_item,
    _is_kirikiri_runtime_overlay_patch_file,
    _is_kirikiri_safe_repack_item,
    _is_runtime_capture_text,
    _is_unsafe_kag_command_attr_item,
    _is_unsafe_kag_command_attr_span,
)
from engines.kirikiri.xp3 import (
    _Xp3Entry,
    _append_xp3_replacement_index,
    _apply_xp3_patch_filter,
    _collect_xp3_patch_filters,
    _patch_xp3_replacements_in_original_slots,
    _read_xp3_entry_bytes,
    _read_xp3_index,
    _rewrite_xp3_with_replacements,
    _select_script_xp3_files,
    _write_xp3_patch,
    _xp3_entry_is_obfuscated_script_candidate,
    _xp3_entry_is_script,
)


class KiriKiriEngine(EngineBase):
    name = "kirikiri"
    label = "吉里吉里 / KiriKiri"
    support_level = "beta"
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
        needs_external_tool=True,
        notes=(
            "第一版覆盖明文 .ks/.tjs 与普通 XP3 中的 KAG/TJS 脚本。",
            "支持 KiriKiri 标准 FE FE 加扰 .ks/.tjs 脚本自动解扰、回填后按原模式重新加扰。",
            "可解析 KiriKiri/PSB 型 .txt.scn，优先抽取 scenes/texts/selects。",
            "内置 XP3 解析器会优先只提取脚本文件；可用 GARbro.Console 时作为补充解包。",
            "回填会保持脚本原编码；必要时回退到 UTF-16 BOM 容纳中文。",
            "默认不做静态脚本回填；翻译完成后生成 x86 原生汉化启动器，在显示层用本地译文表替换文本。",
            "patch.xp3/脚本回填保留为实验开关，只有显式启用 kirikiri_enable_static_patch 时才使用。",
            "保护/混淆脚本默认不会启动 dump；提取不到时应先捕获运行时文本，再走离线批量翻译。",
        ),
    )
    detect_priority = 74
    limitations = [
        "不处理加密 XP3、PIMG 与厂商魔改封包；PSB/SCN 仅覆盖可正常读出的 KiriKiri scenes/texts/selects。",
        "保护/混淆 XP3 可能只能读到索引；标准 FE FE 加扰脚本可处理，非标准加密默认阻断，避免运行时 dump 影响桌面环境。",
        "没有 GARbro.Console 时仍会尝试内置 XP3 脚本提取和目录内已解包明文脚本。",
        "静态 patch.xp3/补丁加载层为实验路径，默认禁用，避免不同 KRKR 魔改保护导致启动报错。",
        "运行时 hook 默认不实时请求 API；它只捕获缺失文本或预加载本地译文。",
    ]

    _SCRIPT_EXTS = {".ks", ".tjs", ".scn"}
    _SYSTEM_KS_ALLOW_RE = re.compile(r"(?:^|/)(?:about|first|title|select|choice|menu|staff|ending)[^/]*\.ks$", re.I)
    _SKIP_DIRS = {
        "__pycache__", ".git", "node_modules", "save", "savedata",
        "_translation_meta", "plugin", "Plugins",
    }

    def detect(self, path: Path) -> bool:
        game_dir = path if path.is_dir() else path.parent
        has_exe = any(game_dir.glob("*.exe"))
        has_xp3 = any(game_dir.glob("*.xp3"))
        has_krkr_marker = any((game_dir / name).exists() for name in ("data.xp3", "startup.tjs", "Config.tjs"))
        has_scripts = any(self._iter_script_files(game_dir, limit=3))
        return has_exe and (has_xp3 or has_krkr_marker or has_scripts)

    def detect_confidence(self, path: Path) -> tuple[int, list[str]]:
        game_dir = path if path.is_dir() else path.parent
        evidence: list[str] = []
        xp3_count = len(list(game_dir.glob("*.xp3")))
        if xp3_count:
            evidence.append(f"找到 {xp3_count} 个 XP3 封包")
        for marker in ("data.xp3", "startup.tjs", "Config.tjs"):
            if (game_dir / marker).exists():
                evidence.append(f"找到 {marker}")
        script_count = sum(1 for _ in self._iter_script_files(game_dir, limit=20))
        if script_count:
            evidence.append(f"找到 {script_count} 个 KAG/TJS 明文脚本")
        if not any(game_dir.glob("*.exe")):
            return 0, []
        if xp3_count or evidence:
            return self.detect_priority + (8 if xp3_count else 0), evidence
        return 0, []

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        game_dir = path if path.is_dir() else path.parent
        original_dir = workspace / "original"
        original_dir.mkdir(parents=True, exist_ok=True)
        meta_dir = workspace / "kirikiri_meta"
        meta_dir.mkdir(parents=True, exist_ok=True)

        self._game_dir = game_dir
        self._workspace = workspace
        self._source_files: dict[str, Path] = {}
        self._script_meta: dict[str, dict[str, object]] = {}
        self._xp3_extracted = False
        self._protected_archives: list[str] = []
        self._protected_script_count = 0
        self._runtime_dump_count = 0
        self._runtime_dump_rels: set[str] = set()
        self._explicit_dump_targets: set[str] = set()
        self._write_runtime_dump_targets = _dump_targets_mod._kirikiri_runtime_dump_targets_enabled()
        self._protected_formats: set[str] = set()
        self._static_external_extractor = ""
        self._static_external_extractor_path = ""
        self._static_external_archives: list[str] = []
        self._static_external_extracted_files = 0
        self._static_external_script_files = 0
        self._static_external_decrypt_succeeded = False
        self._static_external_tool_probes: list[_ExternalToolProbe] = []
        self._static_external_tool_attempts: list[dict] = []
        self._static_external_promoted_files: set[Path] = set()
        self._vntextpatch_export_dir = ""
        self._static_repack_failed_files: list[str] = []
        self._kirikiri_xp3pack_used = False
        self._protected_script_samples: list[dict[str, object]] = []
        self._protected_payload_kinds: set[str] = set()
        self._xp3_extraction_diagnoses: list[_ExtractionDiagnosis] = []
        self._xp3_static_filter_schemes: set[str] = set()
        self._meta_lock = threading.Lock()

        xp3_files = sorted(game_dir.glob("*.xp3"), key=lambda p: p.name.lower())
        self._xp3_static_filter_schemes = _detect_xp3_static_filter_schemes(game_dir)
        if xp3_files:
            candidate_xp3 = _select_script_xp3_files(xp3_files)
            emit_extract_progress(self, f"KRKR：准备解析 {len(candidate_xp3)} 个脚本封包")
            internal_before = {
                path.relative_to(original_dir)
                for path in original_dir.rglob("*")
                if path.is_file()
            }
            internal_extracted = self._try_extract_internal_xp3(original_dir, candidate_xp3)
            self._xp3_internal_files = {
                path.relative_to(original_dir)
                for path in original_dir.rglob("*")
                if path.is_file() and path.relative_to(original_dir) not in internal_before
            }
            needs_external_static = (
                not internal_extracted
                or bool(getattr(self, "_protected_archives", []))
                or int(getattr(self, "_protected_script_count", 0) or 0) > 0
            )
            external_extracted = self._try_extract_xp3(original_dir, candidate_xp3) if needs_external_static else False
            if external_extracted:
                # A validated external result is authoritative. Do not merge
                # an earlier partial internal extraction into it.
                for relative in getattr(self, "_xp3_internal_files", set()):
                    stale = original_dir / relative
                    try:
                        if stale.is_file() and relative not in getattr(self, "_static_external_promoted_files", set()):
                            stale.unlink()
                    except OSError:
                        warning(f"KiriKiri 清理未采用的内置 XP3 结果失败: {relative}")
            self._xp3_extracted = internal_extracted or external_extracted

        dump_dir = game_dir / "_translation_meta" / "kirikiri_dump"
        if dump_dir.exists():
            self._runtime_dump_count = self._copy_runtime_dump_scripts(dump_dir, original_dir)
            if self._runtime_dump_count:
                info(f"KiriKiri 从受保护运行时 dump 载入脚本 {self._runtime_dump_count} 个")

        if not self._runtime_dump_count:
            dump_zip = game_dir / "_translation_meta" / "kirikiri_dump.zip"
            if dump_zip.exists():
                self._runtime_dump_count = self._copy_runtime_dump_archive(dump_zip, original_dir)
                if self._runtime_dump_count:
                    info(f"KiriKiri loaded runtime dump archive scripts: {self._runtime_dump_count}")

        items: list[TextItem] = []
        seen_rel: set[str] = set()
        script_jobs: list[tuple[Path, Path, bool, str]] = []
        for source_root, from_xp3 in ((original_dir, True), (game_dir, False)):
            if not source_root.exists():
                continue
            for script_file in self._iter_script_files(source_root):
                rel = _norm_rel(script_file.relative_to(source_root))
                if rel in seen_rel:
                    continue
                if not from_xp3:
                    workspace_copy = original_dir / rel
                    workspace_copy.parent.mkdir(parents=True, exist_ok=True)
                    if not workspace_copy.exists():
                        shutil.copy2(script_file, workspace_copy)
                    script_file = workspace_copy
                seen_rel.add(rel)
                script_jobs.append((script_file, original_dir, from_xp3, rel))
        items.extend(self._extract_script_jobs(script_jobs))

        if not items:
            # VNTextPatch is a parser fallback only. Its JSON export is
            # converted back to TextItem and therefore cannot create a second
            # translation/checkpoint format.
            items.extend(self._try_vntextpatch_export(original_dir))

        runtime_items = self._load_runtime_capture_items(game_dir)
        if runtime_items:
            existing_originals = {item.original for item in items}
            missing_runtime_items = [
                item for item in runtime_items
                if item.original not in existing_originals
            ]
            if missing_runtime_items:
                items.extend(missing_runtime_items)
                info(f"KiriKiri 从运行时捕获记录补充 {len(missing_runtime_items)} 条文本")

        if not items:
            warning("KiriKiri 未找到可翻译的 .ks/.tjs/.scn 文本；受保护 XP3 需要对应静态解密支持。")
        self._write_extract_diagnostics(meta_dir, items, seen_rel)
        self._write_extract_diagnostics(game_dir / "_translation_meta", items, seen_rel)
        _append_kirikiri_dump_targets_from_names(game_dir, self._explicit_dump_targets)
        _write_kirikiri_explicit_dump_targets(game_dir, self._explicit_dump_targets)
        info(f"KiriKiri 提取到 {len(items)} 条文本，脚本文件 {len(seen_rel)} 个")
        return items

    def _extract_script_jobs(self, jobs: list[tuple[Path, Path, bool, str]]) -> list[TextItem]:
        out: list[TextItem] = []
        total = len(jobs)
        for current, (script_file, root, from_xp3, rel) in enumerate(jobs, start=1):
            if should_report_item(current, total):
                emit_extract_progress(self, script_parse_message(rel, current, total))
            out.extend(self._extract_script_file(script_file, root, from_xp3=from_xp3))
        return out

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        items = self.filter_repack_items(items)
        if not hasattr(self, "_static_external_tool_attempts"):
            self._static_external_tool_attempts = []
        self._static_repack_failed_files = []
        changed: dict[str, list[TextItem]] = {}
        for item in items:
            if _is_kirikiri_control_text_item(item):
                continue
            if item.translated and item.translated != item.original:
                changed.setdefault(_norm_rel(item.file), []).append(item)
        if not changed:
            info("KiriKiri 没有需要回填的译文")
            return

        original_dir = workspace / "original"
        modified_files: list[Path] = []
        failed_files: dict[str, list[TextItem]] = {}
        game_dir = getattr(self, "_game_dir", None)
        sjis_tunnel_encoder: _KirikiriSjisTunnelEncoder | None = None
        sjis_tunnel_files: list[str] = []
        placeholder_map: dict[str, str] = {}
        for rel, file_items in changed.items():
            target = original_dir / rel
            if not target.exists():
                failed_files[rel] = file_items
            if not target.exists():
                warning(f"KiriKiri 回填跳过不存在文件: {rel}")
                continue
            file_tunnel_encoder = None
            use_placeholder = isinstance(game_dir, Path) and _kirikiri_should_use_ascii_placeholder(file_items)
            if isinstance(game_dir, Path) and _kirikiri_should_use_sjis_tunnel(file_items):
                if sjis_tunnel_encoder is None:
                    sjis_tunnel_encoder = _load_kirikiri_sjis_tunnel_encoder(game_dir)
                file_tunnel_encoder = sjis_tunnel_encoder
            if self._patch_script_file(
                target,
                file_items,
                sjis_tunnel_encoder=file_tunnel_encoder,
                placeholder_map=placeholder_map if use_placeholder else None,
            ):
                modified_files.append(target)
                if file_tunnel_encoder is not None:
                    sjis_tunnel_files.append(rel)
            else:
                failed_files[rel] = file_items

        if failed_files:
            recovered = self._try_vntextpatch_import(original_dir, failed_files)
            for rel in recovered:
                target = original_dir / rel
                if target not in modified_files:
                    modified_files.append(target)
                failed_files.pop(rel, None)
            self._static_repack_failed_files = sorted(failed_files)
            if failed_files:
                warning(
                    "KiriKiri 静态回填仍有脚本失败: "
                    + ", ".join(sorted(failed_files)[:8])
                )

        if not modified_files:
            warning("KiriKiri 没有成功修改任何脚本文件")
            return

        if isinstance(game_dir, Path) and sjis_tunnel_encoder is not None and sjis_tunnel_files:
            _write_kirikiri_sjis_tunnel_table(game_dir, sjis_tunnel_encoder, sjis_tunnel_files)
        if isinstance(game_dir, Path) and placeholder_map:
            _write_kirikiri_placeholder_map(game_dir, placeholder_map, modified_files, original_dir)

        runtime_resource_overlay = bool(getattr(self, "_runtime_resource_overlay", False))
        patch_modified_files = modified_files
        # ``changed`` can also contain persisted runtime-capture entries. They
        # have no workspace script to patch and are correctly skipped above;
        # they must not decide how the successfully patched static scripts are
        # deployed. This keeps a previous realtime session from forcing a
        # normal root patch through an EXE-specific stream bridge.
        patched_rels = {
            _norm_rel(path.relative_to(original_dir))
            for path in modified_files
        }
        patch_changed = {
            _norm_rel(rel): group
            for rel, group in changed.items()
            if _norm_rel(rel) in patched_rels
        }
        if runtime_resource_overlay:
            patch_modified_files = [
                path for path in modified_files
                if _is_kirikiri_runtime_overlay_patch_file(_norm_rel(path.relative_to(original_dir)))
            ]
            skipped_runtime_files = sorted(
                _norm_rel(path.relative_to(original_dir))
                for path in modified_files
                if path not in patch_modified_files
            )
            if skipped_runtime_files:
                info(
                    "KiriKiri runtime overlay skipped system/UI scripts: "
                    f"{len(skipped_runtime_files)}"
                )
            allowed_runtime_rels = {
                _norm_rel(path.relative_to(original_dir))
                for path in patch_modified_files
            }
            patch_changed = {
                _norm_rel(rel): group
                for rel, group in changed.items()
                if _norm_rel(rel) in allowed_runtime_rels
            }
            if not patch_modified_files:
                warning("KiriKiri runtime overlay 没有可安全覆盖的剧情脚本，跳过补丁生成")
                return
        if runtime_resource_overlay or any(bool(item.meta.get("from_xp3")) for group in patch_changed.values() for item in group):
            patch_path = original_dir / "patch.xp3"
            use_native_root_patch = (
                isinstance(game_dir, Path)
                and self._should_use_native_root_patch(game_dir, patch_changed)
                and not runtime_resource_overlay
            )
            patch_filters = _collect_xp3_patch_filters(
                patch_changed,
                neutralize_koihazi=use_native_root_patch,
            )
            self._write_patch_archive(
                patch_path,
                original_dir,
                patch_modified_files,
                filters_by_rel=patch_filters,
                include_basename_aliases=not runtime_resource_overlay,
            )
            if isinstance(game_dir, Path):
                use_patch_bridge = self._should_use_patch_bridge(game_dir, changed)
                deployed = game_dir / "patch.xp3"
                bridge_patch = game_dir / "_translation_meta" / "kirikiri_patch.xp3"
                bridge_patch.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(patch_path, bridge_patch)
                if use_patch_bridge or runtime_resource_overlay:
                    if deployed.exists():
                        try:
                            deployed.unlink()
                        except OSError as exc:
                            warning(f"KiriKiri root patch removal failed: {exc}")
                else:
                    shutil.copy2(patch_path, deployed)
                self._deploy_patch_directory(
                    game_dir,
                    original_dir,
                    patch_modified_files,
                    include_basename_aliases=not runtime_resource_overlay,
                )
                self._write_patch_diagnostics(game_dir, original_dir, patch_modified_files, root_patch=not (use_patch_bridge or runtime_resource_overlay))
                self._write_patch_manifest(game_dir, original_dir, patch_modified_files)
                static_result = (
                    {
                        "rebuilt_archives": [],
                        "rebuilt_files": [],
                        "rebuilt_files_by_archive": {},
                        "replacement_count": 0,
                        "signature_disabled": [],
                        "skipped": [],
                        "failed": [],
                        "reason": "runtime_resource_overlay",
                    }
                    if runtime_resource_overlay
                    else self._try_rebuild_static_filtered_xp3(game_dir, original_dir, modified_files, changed)
                )
                if static_result.get("rebuilt_archives"):
                    static_rebuilt_files = {
                        _norm_rel(rel)
                        for rel in static_result.get("rebuilt_files", [])
                        if str(rel or "").strip()
                    }
                    remaining_files = [
                        path for path in modified_files
                        if _norm_rel(path.relative_to(original_dir)) not in static_rebuilt_files
                    ]
                    if remaining_files:
                        remaining_rels = {_norm_rel(path.relative_to(original_dir)) for path in remaining_files}
                        remaining_changed = {
                            _norm_rel(rel): group
                            for rel, group in changed.items()
                            if _norm_rel(rel) in remaining_rels
                        }
                        remaining_use_native_root_patch = (
                            isinstance(game_dir, Path)
                            and self._should_use_native_root_patch(game_dir, remaining_changed)
                            and not runtime_resource_overlay
                        )
                        self._write_patch_archive(
                            patch_path,
                            original_dir,
                            remaining_files,
                            filters_by_rel=_collect_xp3_patch_filters(
                                remaining_changed,
                                neutralize_koihazi=remaining_use_native_root_patch,
                            ),
                            include_basename_aliases=True,
                        )
                        if use_patch_bridge or runtime_resource_overlay:
                            shutil.copy2(patch_path, bridge_patch)
                            if deployed.exists():
                                try:
                                    deployed.unlink()
                                except OSError as exc:
                                    warning(f"KiriKiri root patch removal failed: {exc}")
                        else:
                            shutil.copy2(patch_path, deployed)
                        self._deploy_patch_directory(game_dir, original_dir, remaining_files)
                        self._write_patch_diagnostics(game_dir, original_dir, remaining_files, root_patch=not (use_patch_bridge or runtime_resource_overlay))
                        self._write_patch_manifest(game_dir, original_dir, remaining_files)
                        static_result["shadow_patch_mode"] = "pruned_static_rebuilt_files"
                        static_result["shadow_patch_remaining_files"] = sorted(remaining_rels)
                    else:
                        disabled_patches = self._disable_static_xp3_shadow_patches(game_dir)
                        if disabled_patches:
                            static_result["disabled_shadow_patches"] = disabled_patches
                    self._write_static_rebuild_diagnostics(game_dir, static_result)
                    info(
                        "KiriKiri static XP3 rebuild deployed: "
                        + ", ".join(str(name) for name in static_result["rebuilt_archives"])
                    )
                if runtime_resource_overlay:
                    self._write_static_rebuild_diagnostics(game_dir, static_result)
                    info("KiriKiri runtime resource overlay deployed: _translation_meta/kirikiri_patch.xp3")
                elif use_patch_bridge:
                    info("KiriKiri patch deployed through native bridge: _translation_meta/kirikiri_patch.xp3")
                else:
                    self._write_static_rebuild_diagnostics(game_dir, static_result)
                    info(f"KiriKiri 已部署 XP3 补丁包: {deployed.name}")
            info(f"KiriKiri 生成 XP3 补丁包: {patch_path.name} ({len(modified_files)} 个脚本)")

        info(f"KiriKiri 回填完成: {len(modified_files)} 个脚本文件")

    def find_exe(self, path: Path) -> Path | None:
        game_dir = path if path.is_dir() else path.parent
        for preferred in ("krkr.exe", "krkrz.exe"):
            candidate = game_dir / preferred
            if candidate.is_file():
                return candidate
        return EngineBase.find_exe(self, path)

    def filter_repack_items(self, items: list[TextItem]) -> list[TextItem]:
        return [
            item for item in items
            if not _is_kirikiri_control_text_item(item)
            and _is_kirikiri_safe_repack_item(item)
        ]

    def _should_use_patch_bridge(self, game_dir: Path, changed: dict[str, list[TextItem]] | None = None) -> bool:
        # Decide from the sources being patched, not from an unrelated archive
        # diagnosis. A single protected non-script entry must not make every
        # normal, statically extracted KAG script depend on an EXE-specific
        # bridge signature.
        if changed is not None:
            return changed_items_require_stream_bridge(changed)

        # This fallback is retained for callers that query policy before they
        # have constructed a changed-item map. ``repack`` always supplies the
        # map above, so stale dump files from an earlier run cannot affect it.
        return bool(
            int(getattr(self, "_runtime_dump_count", 0) or 0)
            or getattr(self, "_runtime_dump_rels", set())
        )

    def _should_use_native_root_patch(self, game_dir: Path, changed: dict[str, list[TextItem]] | None = None) -> bool:
        if changed:
            for group in changed.values():
                for item in group:
                    xp3_filter = (item.meta or {}).get("xp3_filter")
                    if isinstance(xp3_filter, dict) and str(xp3_filter.get("kind") or "") == "koihazi_xp3dec":
                        return True
        return "koihazi_xp3dec" in set(getattr(self, "_xp3_static_filter_schemes", set()) or [])

    def _try_rebuild_static_filtered_xp3(
        self,
        game_dir: Path,
        original_dir: Path,
        modified_files: list[Path],
        changed: dict[str, list[TextItem]],
    ) -> dict[str, object]:
        has_static_xp3_replacements = any(
            isinstance((item.meta or {}).get("xp3_filter"), dict)
            for group in changed.values()
            for item in group
        )
        if self._should_use_native_root_patch(game_dir, changed):
            return {
                "rebuilt_archives": [],
                "rebuilt_files": [],
                "rebuilt_files_by_archive": {},
                "replacement_count": 0,
                "signature_disabled": [],
                "skipped": [],
                "failed": [],
                "reason": "native_root_patch_preferred",
            }
        if self._should_use_patch_bridge(game_dir, changed):
            return {
                "rebuilt_archives": [],
                "rebuilt_files": [],
                "rebuilt_files_by_archive": {},
                "replacement_count": 0,
                "signature_disabled": [],
                "skipped": [],
                "failed": [],
                "reason": "patch_bridge_preferred",
            }
        if not self._should_use_patch_bridge(game_dir, changed) and not has_static_xp3_replacements:
            return {
                "rebuilt_archives": [],
                "replacement_count": 0,
                "signature_disabled": [],
                "skipped": [],
                "failed": [],
                "reason": "root_patch_preferred",
            }

        modified_rels = {_norm_rel(path.relative_to(original_dir)) for path in modified_files}
        replacements_by_archive: dict[str, dict[str, bytes]] = {}
        skipped: list[str] = []

        for rel, file_items in changed.items():
            rel = _norm_rel(rel)
            if rel not in modified_rels:
                continue
            xp3_filter = None
            for item in file_items:
                meta = item.meta or {}
                candidate = meta.get("xp3_filter")
                if isinstance(candidate, dict):
                    xp3_filter = candidate
                    break
            if not xp3_filter:
                continue
            archive = str(xp3_filter.get("archive") or "").replace("\\", "/").lstrip("/")
            if not archive or archive.startswith("../") or "/../" in archive:
                skipped.append(rel)
                continue
            source = original_dir / rel
            if not source.exists():
                skipped.append(rel)
                continue
            data = source.read_bytes()
            data = _apply_xp3_patch_filter(data, xp3_filter)
            replacements_by_archive.setdefault(archive, {})[rel] = data

        rebuilt: list[str] = []
        rebuilt_files: list[str] = []
        rebuilt_files_by_archive: dict[str, list[str]] = {}
        failed: list[dict[str, str]] = []
        signature_disabled: list[str] = []
        modes: dict[str, str] = {}
        for archive, replacements in sorted(replacements_by_archive.items()):
            xp3_path = game_dir / archive
            if not xp3_path.exists():
                failed.append({"archive": archive, "reason": "archive_missing"})
                continue
            tmp_path = xp3_path.with_suffix(xp3_path.suffix + ".tmp")
            try:
                source_index = _read_xp3_index(xp3_path)
                source_rels = {_norm_rel(entry.name) for entry in source_index.entries}
                archive_rebuilt_files: list[str] = []
                if source_index.chained:
                    backup_path = game_dir / "_translation_meta" / "original_signatures" / f"{xp3_path.name}.before_in_place_slot_patch"
                    backup_path.parent.mkdir(parents=True, exist_ok=True)
                    if not backup_path.exists():
                        shutil.copy2(xp3_path, backup_path)
                    slot_result = _patch_xp3_replacements_in_original_slots(xp3_path, replacements)
                    if slot_result["replaced"]:
                        replaced = int(slot_result["replaced"])
                        archive_rebuilt_files = [
                            _norm_rel(rel)
                            for rel in slot_result.get("replaced_files", [])
                            if str(rel or "").strip()
                        ]
                        modes[archive] = "in_place_original_segments_preserve_index"
                        for skipped_item in slot_result.get("skipped", []):
                            skipped.append(
                                f"{archive}:{skipped_item.get('file', '')}:{skipped_item.get('reason', '')}"
                            )
                    else:
                        fallback_backup = game_dir / "_translation_meta" / "original_signatures" / f"{xp3_path.name}.before_append_patch"
                        if not fallback_backup.exists():
                            shutil.copy2(xp3_path, fallback_backup)
                        replaced = _append_xp3_replacement_index(xp3_path, replacements)
                        archive_rebuilt_files = sorted(
                            rel for rel in replacements
                            if _norm_rel(rel) in source_rels
                        )
                        modes[archive] = "append_chained_index_preserve_raw_index_chunks"
                else:
                    replaced = _rewrite_xp3_with_replacements(xp3_path, tmp_path, replacements)
                    archive_rebuilt_files = sorted(
                        rel for rel in replacements
                        if _norm_rel(rel) in source_rels
                    )
                    modes[archive] = "rewrite_archive"
                if replaced <= 0:
                    tmp_path.unlink(missing_ok=True)
                    failed.append({"archive": archive, "reason": "no_matching_entries"})
                    continue
                if archive_rebuilt_files and len(archive_rebuilt_files) > replaced:
                    archive_rebuilt_files = archive_rebuilt_files[:replaced]
                if tmp_path.exists():
                    shutil.move(str(tmp_path), str(xp3_path))
                rebuilt.append(archive)
                rebuilt_files_by_archive[archive] = archive_rebuilt_files
                rebuilt_files.extend(archive_rebuilt_files)
                sig = xp3_path.with_name(xp3_path.name + ".sig")
                if sig.exists():
                    sig_store = game_dir / "_translation_meta" / "original_signatures"
                    sig_store.mkdir(parents=True, exist_ok=True)
                    stored = sig_store / f"{sig.name}.disabled"
                    if not stored.exists():
                        shutil.copy2(sig, stored)
                    sig.unlink()
                    signature_disabled.append(sig.name)
            except Exception as exc:
                tmp_path.unlink(missing_ok=True)
                failed.append({"archive": archive, "reason": str(exc)})

        return {
            "rebuilt_archives": rebuilt,
            "rebuilt_files": sorted(set(rebuilt_files)),
            "rebuilt_files_by_archive": {
                archive: sorted(set(files))
                for archive, files in rebuilt_files_by_archive.items()
            },
            "replacement_count": sum(len(items) for items in replacements_by_archive.values()),
            "signature_disabled": signature_disabled,
            "modes": modes,
            "skipped": skipped,
            "failed": failed,
        }

    def _write_patch_archive(
        self,
        output_path: Path,
        root: Path,
        files: list[Path],
        *,
        filters_by_rel: dict[str, dict[str, object]] | None = None,
        include_basename_aliases: bool = False,
    ) -> None:
        """Write an XP3 patch and use Xp3Pack only for a validated fallback."""
        try:
            _write_xp3_patch(
                output_path,
                root,
                files,
                filters_by_rel=filters_by_rel,
                include_basename_aliases=include_basename_aliases,
            )
            index = _read_xp3_index(output_path)
            if not index.entries:
                raise ValueError("EngAixt XP3 writer produced an empty index")
            return
        except Exception as exc:
            if filters_by_rel:
                raise RuntimeError(
                    "EngAixt XP3 writer failed for filtered scripts; "
                    f"Xp3Pack cannot preserve archive filters: {exc}"
                ) from exc

            candidate_root = root.parent / "external_candidates" / "kirikiri_xp3pack"
            if candidate_root.exists():
                shutil.rmtree(candidate_root)
            input_dir = candidate_root / "patch_input"
            input_dir.mkdir(parents=True, exist_ok=True)
            basename_counts: dict[str, int] = {}
            normalized: list[tuple[Path, str]] = []
            for source in files:
                try:
                    rel = _norm_rel(source.relative_to(root))
                except ValueError:
                    continue
                if not rel or rel.startswith("../") or "/../" in rel:
                    continue
                normalized.append((source, rel))
                basename = Path(rel).name.casefold()
                basename_counts[basename] = basename_counts.get(basename, 0) + 1
            for source, rel in normalized:
                target = input_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                basename = Path(rel).name
                if include_basename_aliases and "/" in rel and basename_counts.get(basename.casefold()) == 1:
                    alias = input_dir / basename
                    if not alias.exists():
                        shutil.copy2(source, alias)

            candidate_archive = candidate_root / "patch.xp3"
            result = run_xp3pack(input_dir, candidate_archive)
            attempt = {
                **result.to_dict(),
                "role": "archive_pack",
                "fallback_for": "engaixt_xp3_writer",
            }
            if not hasattr(self, "_static_external_tool_attempts"):
                self._static_external_tool_attempts = []
            self._static_external_tool_attempts.append(attempt)
            if not result.ok or not candidate_archive.is_file():
                raise RuntimeError(
                    "EngAixt XP3 writer failed and KirikiriTools Xp3Pack was unavailable: "
                    f"{exc}; {result.detail}"
                ) from exc
            try:
                fallback_index = _read_xp3_index(candidate_archive)
            except Exception as fallback_exc:
                raise RuntimeError(f"Xp3Pack output is not a readable XP3: {fallback_exc}") from fallback_exc
            if not fallback_index.entries:
                raise RuntimeError("Xp3Pack output has no entries")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate_archive, output_path)
            self._kirikiri_xp3pack_used = True
            info("KiriKiriTools Xp3Pack 已接管 XP3 补丁生成")

    def _write_static_rebuild_diagnostics(self, game_dir: Path, result: dict[str, object]) -> None:
        try:
            meta_dir = game_dir / "_translation_meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            (meta_dir / "kirikiri_static_xp3_rebuild.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            debug(f"KiriKiri failed to write static XP3 rebuild diagnostics: {exc}")

    def _disable_static_xp3_shadow_patches(self, game_dir: Path) -> list[str]:
        """Avoid patch-layer scripts shadowing a successful source XP3 rewrite.

        Some protected or filtered KiriKiri builds accept scripts from data.xp3
        after the original filter is preserved, but reject the same filtered
        bytes when they are loaded as a normal patch archive. Once source XP3
        rewrite succeeds, the source archive is the authoritative patch target.
        """
        disabled: list[str] = []
        meta_dir = game_dir / "_translation_meta"
        targets = [
            game_dir / "patch.xp3",
            meta_dir / "kirikiri_patch.xp3",
            meta_dir / "kirikiri_patch",
        ]
        for target in targets:
            if not target.exists():
                continue
            backup = target.with_name(target.name + ".disabled_static_rewrite")
            try:
                if backup.exists():
                    if backup.is_dir():
                        shutil.rmtree(backup)
                    else:
                        backup.unlink()
                target.replace(backup)
                disabled.append(_norm_rel(backup.relative_to(game_dir)))
            except Exception as exc:
                warning(f"KiriKiri failed to disable shadow patch {target.name}: {exc}")

        try:
            manifest = meta_dir / "kirikiri_patch_manifest.txt"
            if manifest.exists():
                backup = manifest.with_name(manifest.name + ".disabled_static_rewrite")
                if backup.exists():
                    backup.unlink()
                manifest.replace(backup)
                disabled.append(_norm_rel(backup.relative_to(game_dir)))
        except Exception as exc:
            debug(f"KiriKiri failed to disable patch manifest after static rewrite: {exc}")

        return disabled

    def _load_runtime_capture_items(self, game_dir: Path) -> list[TextItem]:
        capture = game_dir / "_translation_meta" / "kirikiri_runtime_capture.jsonl"
        if not capture.exists():
            return []
        items: list[TextItem] = []
        seen: set[str] = set()
        try:
            lines = capture.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except Exception as exc:
            warning(f"KiriKiri 运行时捕获记录读取失败: {exc}")
            return []
        for idx, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                raw = {"text": line, "source": "legacy"}
            role = str(raw.get("role") or "text").strip().lower()
            if role not in {"", "text", "message"}:
                continue
            text = _clean_runtime_capture_text(str(raw.get("visible_text") or raw.get("text") or ""))
            if not text or text in seen or not _is_runtime_capture_text(text):
                continue
            seen.add(text)
            digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
            items.append(TextItem(
                file="__kirikiri_runtime_capture__.jsonl",
                key=f"runtime:{digest}",
                original=text,
                context="message",
                line=idx,
                meta={
                    "engine": "kirikiri",
                    "runtime_capture": True,
                    "source": str(raw.get("hook_name") or raw.get("source") or ""),
                    "runtime_capture_role": role or "text",
                    "runtime_capture_raw_text": str(raw.get("raw_text") or ""),
                },
            ))
        return items

    def _try_extract_internal_xp3(self, extract_dir: Path, xp3_files: list[Path]) -> bool:
        extracted_files = 0
        parsed_archives = 0
        protected_archives: list[str] = []

        total_archives = len(xp3_files)
        for archive_current, xp3 in enumerate(xp3_files, start=1):
            emit_extract_progress(self, archive_index_message(xp3, archive_current, total_archives))
            try:
                index = _read_xp3_index(xp3)
            except Exception as exc:
                debug(f"KiriKiri 内置 XP3 解析失败 {xp3.name}: {exc}")
                continue

            parsed_archives += 1
            if "yuz:" in index.unknown_chunks:
                self._protected_formats.add("yuzusoft_yuz_xp3")
            diagnosis = _diagnose_xp3_archive(xp3, index)
            self._record_xp3_diagnosis(diagnosis)
            script_entries = [
                entry for entry in index.entries
                if _xp3_entry_is_script(entry) or _xp3_entry_is_obfuscated_script_candidate(xp3, entry)
            ]
            if script_entries:
                debug(f"KiriKiri 内置 XP3 发现脚本: {xp3.name} -> {len(script_entries)} 个")
            elif (
                index.entries
                and (
                    _xp3_may_contain_script_markers(xp3)
                    or diagnosis.protection_layer in {_ProtectionLayer.INDEX_OBFUSCATED, _ProtectionLayer.BOTH}
                )
            ):
                protected_archives.append(xp3.name)

            archive_extracted = 0
            skipped_binary = 0
            skipped_protected = 0
            static_filter_decrypted = 0
            total_scripts = len(script_entries)
            for script_current, entry in enumerate(script_entries, start=1):
                if should_report_item(script_current, total_scripts):
                    emit_extract_progress(
                        self,
                        archive_script_message(xp3, script_current, total_scripts),
                    )
                if entry.original_size > 8 * 1024 * 1024:
                    warning(f"KiriKiri 跳过过大的 XP3 脚本: {xp3.name}:{entry.name}")
                    continue
                try:
                    data = _read_xp3_entry_bytes(xp3, entry)
                except Exception as exc:
                    warning(f"KiriKiri XP3 脚本解出失败: {xp3.name}:{entry.name} - {exc}")
                    continue
                filter_meta: dict[str, object] | None = None
                filtered = _decode_xp3_script_filter_for_static_extract(
                    data,
                    entry=entry,
                    schemes=getattr(self, "_xp3_static_filter_schemes", set()),
                )
                if filtered[1]:
                    data, filter_meta = filtered
                if entry.name.lower().endswith(".scn"):
                    if not is_kirikiri_scn(data):
                        debug(f"KiriKiri 跳过非 PSB SCN: {xp3.name}:{entry.name}")
                        skipped_binary += 1
                        continue
                elif not Path(entry.name).suffix and is_kirikiri_scn(data):
                    pass
                elif _is_probably_binary_script(data):
                    debug(f"KiriKiri 跳过编译/二进制脚本: {xp3.name}:{entry.name}")
                    skipped_binary += 1
                    continue
                elif _try_decode_xp3_single_byte_xor_filter(data) is not None:
                    pass
                elif _is_probably_protected_text_script(data):
                    debug(f"KiriKiri 跳过不可读/受保护文本脚本: {xp3.name}:{entry.name}")
                    self._record_protected_payload_sample(xp3, entry, data)
                    skipped_protected += 1
                    continue
                out_path = _safe_output_path(extract_dir, entry.name)
                if out_path is None:
                    warning(f"KiriKiri XP3 脚本路径不安全，已跳过: {xp3.name}:{entry.name}")
                    continue
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(data)
                if filter_meta:
                    static_filter_decrypted += 1
                    rel = _norm_rel(out_path.relative_to(extract_dir))
                    with self._meta_lock:
                        self._script_meta.setdefault(rel, {})["xp3_filter"] = {
                            **filter_meta,
                            "archive": xp3.name,
                        }
                archive_extracted += 1
                extracted_files += 1

            if static_filter_decrypted:
                self._static_external_decrypt_succeeded = True
                schemes = sorted(getattr(self, "_xp3_static_filter_schemes", set()) or [])
                self._static_external_extractor = (
                    "kirikiri_static_" + "+".join(schemes)
                    if schemes
                    else "kirikiri_static_xor_filter"
                )
                self._static_external_extractor_path = ""
                if xp3.name not in self._static_external_archives:
                    self._static_external_archives.append(xp3.name)
                self._static_external_extracted_files += static_filter_decrypted
                self._static_external_script_files += static_filter_decrypted
                info(f"KiriKiri static XP3 filter decrypted scripts: {xp3.name} ({static_filter_decrypted})")

            if skipped_protected:
                self._protected_script_count += skipped_protected
                protected_archives.append(xp3.name)
                if getattr(self, "_write_runtime_dump_targets", False):
                    _append_kirikiri_dump_targets_from_entries(xp3, getattr(self, "_game_dir", None), script_entries)
                warning(
                    f"KiriKiri XP3 脚本内容疑似受保护/加密，默认不运行 dump: "
                    f"{xp3.name} ({skipped_protected} 个)"
                )
            if archive_extracted == 0 and skipped_binary:
                warning(f"KiriKiri XP3 仅发现编译/二进制脚本，第一版不做静态回填: {xp3.name} ({skipped_binary} 个)")
                if _xp3_may_contain_script_markers(xp3):
                    protected_archives.append(xp3.name)
                    if getattr(self, "_write_runtime_dump_targets", False):
                        _append_kirikiri_dump_targets_from_entries(xp3, getattr(self, "_game_dir", None), script_entries)

        if protected_archives:
            self._protected_archives = sorted(set(getattr(self, "_protected_archives", []) + protected_archives))
        if extracted_files:
            info(f"KiriKiri 内置 XP3 提取脚本 {extracted_files} 个")
            return True
        if parsed_archives and protected_archives:
            warning(
                "KiriKiri XP3 索引可读，但脚本名或脚本内容疑似被保护/混淆，"
                f"无法静态提取: {', '.join(protected_archives[:5])}"
            )
        return False

    def _record_xp3_diagnosis(self, diagnosis: _ExtractionDiagnosis) -> None:
        diagnoses = getattr(self, "_xp3_extraction_diagnoses", None)
        if diagnoses is None:
            self._xp3_extraction_diagnoses = []
            diagnoses = self._xp3_extraction_diagnoses
        diagnoses.append(diagnosis)
        if diagnosis.protection_layer in {_ProtectionLayer.INDEX_OBFUSCATED, _ProtectionLayer.BOTH}:
            archives = getattr(self, "_protected_archives", [])
            if diagnosis.archive_file not in archives:
                archives.append(diagnosis.archive_file)
                self._protected_archives = archives
        for signature in diagnosis.content_signatures:
            if signature not in {
                _ContentSignature.PLAINTEXT_UTF8,
                _ContentSignature.PLAINTEXT_CP932,
                _ContentSignature.PLAINTEXT_UTF16,
                _ContentSignature.TJS_BYTECODE,
            }:
                getattr(self, "_protected_payload_kinds", set()).add(signature.value)
        if diagnosis.protection_layer != _ProtectionLayer.NONE and diagnosis.sample_hex_head:
            sample = {
                "archive": diagnosis.archive_file,
                "entry": diagnosis.sample_entry,
                "size": None,
                "hex_head": diagnosis.sample_hex_head,
                "content_signature": diagnosis.content_signature.value,
                "entropy": diagnosis.entropy_sample,
            }
            samples = getattr(self, "_protected_script_samples", [])
            if len(samples) < 10 and sample not in samples:
                samples.append(sample)
                self._protected_script_samples = samples

    def _record_protected_payload_sample(self, xp3: Path, entry: _Xp3Entry, data: bytes) -> None:
        samples = getattr(self, "_protected_script_samples", [])
        if len(samples) >= 10:
            return
        signature = _classify_xp3_payload(data)
        entropy = _shannon_entropy(data[:8192])
        sample = {
            "archive": xp3.name,
            "entry": entry.name,
            "size": entry.original_size,
            "hex_head": data[:16].hex(),
            "content_signature": signature.value,
            "entropy": round(entropy, 3),
        }
        if sample not in samples:
            samples.append(sample)
            self._protected_script_samples = samples
        getattr(self, "_protected_payload_kinds", set()).add(signature.value)

    def _write_extract_diagnostics(self, meta_dir: Path, items: list[TextItem], script_files: set[str]) -> None:
        try:
            meta_dir.mkdir(parents=True, exist_ok=True)
            runtime_dump_script_count = int(getattr(self, "_runtime_dump_count", 0))
            runtime_dump_text_count = sum(1 for item in items if bool((item.meta or {}).get("runtime_dump")))
            runtime_capture_count = sum(1 for item in items if bool((item.meta or {}).get("runtime_capture")))
            changed_sources = {"all": items}
            needs_patch_bridge = changed_items_require_stream_bridge(changed_sources)
            diagnoses = list(getattr(self, "_xp3_extraction_diagnoses", []) or [])
            primary_diagnosis = _primary_xp3_diagnosis(diagnoses)
            payload = {
                "engine": self.name,
                "text_count": len(items),
                "script_file_count": len(script_files),
                "scn_file_count": len({item.file for item in items if str((item.meta or {}).get("format")) == "kirikiri_psb_scn"}),
                "from_xp3_count": sum(1 for item in items if bool((item.meta or {}).get("from_xp3"))),
                "runtime_dump_script_count": runtime_dump_script_count,
                "runtime_dump_text_count": runtime_dump_text_count,
                "runtime_capture_count": runtime_capture_count,
                "protected_archives": list(getattr(self, "_protected_archives", [])),
                "protected_formats": sorted(getattr(self, "_protected_formats", set())),
                "protected_script_count": int(getattr(self, "_protected_script_count", 0) or 0),
                "runtime_dump_targets_enabled": bool(getattr(self, "_write_runtime_dump_targets", False)),
                "runtime_dump_required": False,
                "static_decrypt_required": bool(
                    getattr(self, "_protected_archives", [])
                    and not bool(getattr(self, "_static_external_decrypt_succeeded", False))
                    and not items
                ),
                "static_decrypt_succeeded": bool(getattr(self, "_static_external_decrypt_succeeded", False)),
                "static_external_extractor": str(getattr(self, "_static_external_extractor", "") or ""),
                "static_external_extractor_path": str(getattr(self, "_static_external_extractor_path", "") or ""),
                "static_external_archives": list(getattr(self, "_static_external_archives", []) or []),
                "static_external_extracted_files": int(getattr(self, "_static_external_extracted_files", 0) or 0),
                "static_external_script_files": int(getattr(self, "_static_external_script_files", 0) or 0),
                "static_external_tool_probes": [
                    {
                        "tool": probe.tool,
                        "path": probe.path,
                        "archive_file": probe.archive_file,
                        "status": probe.status,
                        "message": probe.message,
                    }
                    for probe in getattr(self, "_static_external_tool_probes", []) or []
                ],
                "static_external_tool_attempts": list(
                    getattr(self, "_static_external_tool_attempts", []) or []
                ),
                "tool_attempts": list(
                    getattr(self, "_static_external_tool_attempts", []) or []
                ),
                "protection_layer": (
                    "no_protection"
                    if bool(getattr(self, "_static_external_decrypt_succeeded", False)) and items
                    else (primary_diagnosis.protection_layer.value if primary_diagnosis else "unknown")
                ),
                "index_anomaly": primary_diagnosis.index_anomaly.value if primary_diagnosis else "unknown",
                "content_signature": (
                    "decrypted_static_filter"
                    if bool(getattr(self, "_static_external_decrypt_succeeded", False)) and items
                    else (primary_diagnosis.content_signature.value if primary_diagnosis else "unknown")
                ),
                "static_filter_schemes": sorted(getattr(self, "_xp3_static_filter_schemes", set()) or []),
                "protected_payload_kinds": sorted(getattr(self, "_protected_payload_kinds", set()) or []),
                "protected_script_samples": list(getattr(self, "_protected_script_samples", []) or []),
                "xp3_extraction_diagnoses": [_diagnosis_to_dict(diagnosis) for diagnosis in diagnoses],
                "recommended_action": primary_diagnosis.recommended_action if primary_diagnosis else "",
                "needs_patch_bridge": needs_patch_bridge,
            }
            (meta_dir / "extract_diagnostics.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            debug(f"KiriKiri 写入提取诊断失败: {exc}")

    def _write_patch_diagnostics(self, game_dir: Path, root: Path, modified_files: list[Path], *, root_patch: bool = True) -> None:
        try:
            meta_dir = game_dir / "_translation_meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            scn_files = [p for p in modified_files if p.suffix.lower() == ".scn"]
            payload = {
                "engine": self.name,
                "patch_xp3": "patch.xp3" if root_patch else "_translation_meta/kirikiri_patch.xp3",
                "root_patch": root_patch,
                "patch_dir": "_translation_meta/kirikiri_patch",
                "script_file_count": len(modified_files),
                "scn_file_count": len(scn_files),
                "from_xp3": True,
                "protected_archives": list(getattr(self, "_protected_archives", [])),
                "needs_patch_bridge": not root_patch,
                "script_files": [_norm_rel(p.relative_to(root)) for p in modified_files[:200]],
            }
            (meta_dir / "kirikiri_patch_diagnostics.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            debug(f"KiriKiri 写入补丁诊断失败: {exc}")

    def _deploy_patch_directory(
        self,
        game_dir: Path,
        root: Path,
        modified_files: list[Path],
        *,
        include_basename_aliases: bool = True,
    ) -> None:
        try:
            patch_dir = game_dir / "_translation_meta" / "kirikiri_patch"
            if patch_dir.exists():
                shutil.rmtree(patch_dir)
            patch_dir.mkdir(parents=True, exist_ok=True)
            basename_counts: dict[str, int] = {}
            rels = [(src, _norm_rel(src.relative_to(root))) for src in modified_files]
            for _src, rel in rels:
                basename = Path(rel).name.lower()
                basename_counts[basename] = basename_counts.get(basename, 0) + 1
            for src in modified_files:
                rel_norm = _norm_rel(src.relative_to(root))
                rel = Path(rel_norm)
                dst = patch_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                basename = rel.name
                if include_basename_aliases and "/" in rel_norm and basename_counts.get(basename.lower()) == 1:
                    alias = patch_dir / basename
                    if not alias.exists():
                        shutil.copy2(src, alias)
        except Exception as exc:
            warning(f"KiriKiri loose patch deployment failed: {exc}")

    def _write_patch_manifest(self, game_dir: Path, root: Path, modified_files: list[Path]) -> None:
        try:
            meta_dir = game_dir / "_translation_meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            entries = {_norm_rel(p.relative_to(root)) for p in modified_files}
            entries = sorted(entries)
            (meta_dir / "kirikiri_patch_manifest.txt").write_text(
                "\n".join(entries) + ("\n" if entries else ""),
                encoding="utf-8",
            )
        except Exception as exc:
            debug(f"KiriKiri failed to write patch manifest: {exc}")

    def _copy_runtime_dump_scripts(self, dump_dir: Path, original_dir: Path) -> int:
        copied = 0
        for script_file in self._iter_runtime_dump_files(dump_dir):
            try:
                rel = _norm_rel(script_file.relative_to(dump_dir))
            except ValueError:
                continue
            out_path = _safe_output_path(original_dir, rel)
            if out_path is None:
                warning(f"KiriKiri dump 脚本路径不安全，已跳过: {rel}")
                continue
            try:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(script_file, out_path)
                self._runtime_dump_rels.add(rel)
                copied += 1
            except Exception as exc:
                warning(f"KiriKiri dump 脚本复制失败: {rel} - {exc}")
        return copied

    def _copy_runtime_dump_archive(self, archive_path: Path, original_dir: Path) -> int:
        copied = 0
        try:
            with zipfile.ZipFile(archive_path) as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    rel = member.filename.replace("\\", "/")
                    if rel.startswith("kirikiri_dump/"):
                        rel = rel[len("kirikiri_dump/"):]
                    if not rel or Path(rel).suffix.lower() not in self._SCRIPT_EXTS:
                        continue
                    if member.file_size > 8 * 1024 * 1024:
                        warning(f"KiriKiri dump archive skipped oversized script: {rel}")
                        continue
                    out_path = _safe_output_path(original_dir, rel)
                    if out_path is None:
                        warning(f"KiriKiri dump archive skipped unsafe path: {rel}")
                        continue
                    try:
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        out_path.write_bytes(zf.read(member))
                        self._runtime_dump_rels.add(_norm_rel(rel))
                        copied += 1
                    except Exception as exc:
                        warning(f"KiriKiri dump archive extract failed: {rel} - {exc}")
        except zipfile.BadZipFile as exc:
            warning(f"KiriKiri dump archive is invalid: {archive_path.name} - {exc}")
        except Exception as exc:
            warning(f"KiriKiri dump archive read failed: {archive_path.name} - {exc}")
        return copied

    def _try_extract_xp3(self, extract_dir: Path, xp3_files: list[Path]) -> bool:
        if not hasattr(self, "_static_external_archives"):
            self._static_external_archives = []
        if not hasattr(self, "_static_external_extracted_files"):
            self._static_external_extracted_files = 0
        if not hasattr(self, "_static_external_script_files"):
            self._static_external_script_files = 0
        if not hasattr(self, "_static_external_decrypt_succeeded"):
            self._static_external_decrypt_succeeded = False
        if not hasattr(self, "_static_external_tool_probes"):
            self._static_external_tool_probes = []
        if not hasattr(self, "_static_external_tool_attempts"):
            self._static_external_tool_attempts = []
        if not hasattr(self, "_static_external_promoted_files"):
            self._static_external_promoted_files = set()
        garbro = self._find_garbro()
        if garbro:
            total_archives = len(xp3_files)
            for archive_current, xp3 in enumerate(xp3_files, start=1):
                emit_extract_progress(
                    self,
                    external_tool_message("GARbro", xp3, archive_current, total_archives),
                )
                if self._try_extract_one_xp3_with_garbro(garbro, xp3, extract_dir):
                    return True
        else:
            debug("GARbro.Console 未找到，跳过自动 XP3 解包")
            if self._check_garbro_gui_available():
                info("GARbro GUI 可用；后台不会自动启动 GUI，可手动解包后重跑管线。")

        # msg-tool is deliberately a second, independent candidate.  It is
        # never merged with GARbro output and rc=0 is not considered success.
        total_archives = len(xp3_files)
        for archive_current, xp3 in enumerate(xp3_files, start=1):
            emit_extract_progress(
                self,
                external_tool_message("msg-tool", xp3, archive_current, total_archives),
            )
            candidate_dir = extract_dir / ".external_candidates" / "msg_tool" / xp3.stem
            result = run_msg_tool_unpack(xp3, candidate_dir)
            self._static_external_tool_attempts.append({
                **result.to_dict(),
                "archive_file": xp3.name,
                "role": "xp3_extract",
            })
            if not result.ok:
                continue
            promoted = promote_script_files(candidate_dir, extract_dir, result.script_files)
            if promoted <= 0:
                continue
            self._static_external_extractor = "msg_tool"
            self._static_external_extractor_path = result.path
            self._static_external_extracted_files += promoted
            self._static_external_script_files += promoted
            self._static_external_promoted_files.update(
                Path(rel) for rel in result.script_files
            )
            self._static_external_decrypt_succeeded = True
            if xp3.name not in self._static_external_archives:
                self._static_external_archives.append(xp3.name)
            return True
        return False

    def _try_extract_one_xp3_with_garbro(self, garbro: Path, xp3: Path, extract_dir: Path) -> bool:
        """Run GARbro in an isolated candidate directory."""
        candidate_dir = extract_dir / ".external_candidates" / "garbro" / xp3.stem
        list_result = _garbro_mod._probe_garbro_entries(garbro, xp3)
        self._static_external_tool_probes.append(_ExternalToolProbe(
            tool="garbro_console",
            path=str(garbro),
            archive_file=xp3.name,
            status=list_result.status,
            message=list_result.message,
        ))
        self._static_external_tool_attempts.append({
            "tool": "garbro_console",
            "path": str(garbro),
            "version": "managed",
            "archive_file": xp3.name,
            "status": list_result.status,
            "entries": len(list_result.entries),
            "role": "xp3_list",
            "output_summary": list_result.message[:500],
        })
        entries = _garbro_script_entries(xp3, list_result.entries)
        if not entries:
            debug(f"GARbro 列表未发现脚本文件: {xp3.name}")
            return False

        before_files = _count_files(candidate_dir)
        extracted_this = _extract_garbro_entries(garbro, xp3, candidate_dir, entries)
        if not extracted_this:
            warning(f"KiriKiri XP3 未能自动解包: {xp3.name}")
            self._static_external_tool_attempts.append({
                "tool": "garbro_console",
                "path": str(garbro),
                "version": "managed",
                "archive_file": xp3.name,
                "status": "no_output",
                "entries": len(entries),
                "role": "xp3_extract",
            })
            return False

        script_rels = [
            str(path.relative_to(candidate_dir)).replace("\\", "/")
            for path in candidate_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".ks", ".tjs", ".scn"}
        ]
        promoted = promote_script_files(candidate_dir, extract_dir, script_rels)
        if promoted <= 0:
            return False
        extracted_files = max(0, _count_files(candidate_dir) - before_files)
        info(f"GARbro.Console 静态提取 {xp3.name}: {promoted}/{len(entries)} 个脚本")
        self._static_external_tool_attempts.append({
            "tool": "garbro_console",
            "path": str(garbro),
            "version": "managed",
            "archive_file": xp3.name,
            "status": "success",
            "entries": len(entries),
            "generated_files": extracted_files,
            "script_files": promoted,
            "role": "xp3_extract",
        })
        self._static_external_extractor = "garbro_console"
        self._static_external_extractor_path = str(garbro)
        if xp3.name not in self._static_external_archives:
            self._static_external_archives.append(xp3.name)
        self._static_external_extracted_files += extracted_files
        self._static_external_script_files += promoted
        self._static_external_promoted_files.update(Path(rel) for rel in script_rels)
        if "yuzusoft_yuz_xp3" in getattr(self, "_protected_formats", set()) or xp3.name in getattr(self, "_protected_archives", []):
            self._static_external_decrypt_succeeded = True
        return True

    def _try_vntextpatch_export(self, original_dir: Path) -> list[TextItem]:
        """Use VNTextPatch only when native parsing yielded no usable items."""
        scripts = [
            path for path in original_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".ks", ".tjs", ".scn"}
        ]
        if not scripts:
            return []
        output_dir = original_dir.parent / "external_candidates" / "vntextpatch"
        result = run_vntextpatch_export(original_dir, output_dir)
        self._static_external_tool_attempts.append({
            **result.to_dict(),
            "role": "script_extract",
        })
        if not result.ok:
            return []
        rows = load_vntextpatch_json_items(output_dir, original_dir)
        items: list[TextItem] = []
        for row in rows:
            row.setdefault("meta", {})["from_external_tool"] = True
            items.append(TextItem(**row))
        if items:
            info(f"VNTextPatch 备用脚本提取: {len(items)} 条")
            self._static_external_extractor = "vntextpatch"
            self._static_external_extractor_path = result.path
            self._vntextpatch_export_dir = str(output_dir)
        return items

    def _try_vntextpatch_import(
        self,
        original_dir: Path,
        failed_files: dict[str, list[TextItem]],
    ) -> set[str]:
        """Use VNTextPatch insertlocal for files native patching could not write."""
        export_dir = Path(str(getattr(self, "_vntextpatch_export_dir", "") or ""))
        if not export_dir.is_dir():
            return set()
        import_root = original_dir.parent / "external_candidates" / "vntextpatch_import"
        if import_root.exists():
            shutil.rmtree(import_root)
        translation_root = import_root / "translations"
        output_root = import_root / "patched"
        recovered: set[str] = set()
        format_by_suffix = {
            ".ks": "kirikiriks",
            ".scn": "kirikiriscn",
            ".tjs": "kirikiritjs",
        }

        for rel, items in sorted(failed_files.items()):
            rel = _norm_rel(rel)
            source = original_dir / rel
            if not source.is_file():
                continue
            source_json = find_vntextpatch_json(export_dir, rel)
            if source_json is None:
                self._static_external_tool_attempts.append({
                    "tool": "vntextpatch",
                    "role": "script_repack",
                    "source_file": rel,
                    "status": "missing_export_json",
                })
                continue
            translation_json = translation_root / f"{rel}.json"
            changed = write_vntextpatch_translation_json(source_json, translation_json, items)
            if changed <= 0:
                self._static_external_tool_attempts.append({
                    "tool": "vntextpatch",
                    "role": "script_repack",
                    "source_file": rel,
                    "status": "no_matching_translation",
                })
                continue
            output = output_root / rel
            format_name = format_by_suffix.get(source.suffix.lower())
            if not format_name:
                continue
            result = run_vntextpatch_import(
                source,
                translation_json,
                output,
                format_name=format_name,
            )
            self._static_external_tool_attempts.append({
                **result.to_dict(),
                "role": "script_repack",
                "source_file": rel,
                "translated_fields": changed,
            })
            if not result.ok or not output.is_file():
                continue
            try:
                data = output.read_bytes()
                if source.suffix.lower() == ".scn":
                    valid = is_kirikiri_scn(data)
                elif source.suffix.lower() == ".tjs":
                    valid = data.startswith(b"TJS2100\x00") or _read_text_guess(output) is not None
                else:
                    valid = _read_text_guess(output) is not None
                if not valid:
                    continue
                shutil.copy2(output, source)
            except OSError:
                continue
            recovered.add(rel)

        if recovered:
            self._static_external_extractor = "vntextpatch"
        return recovered

    def _find_garbro(self) -> Path | None:
        try:
            from core.tool_manager import find_tool
            tool = find_tool("garbro_mod") or find_tool("garbro_console")
            if tool:
                return tool
        except Exception:
            pass
        for name in ("GARbro.Console.exe", "GARbro.Console"):
            found = shutil.which(name)
            if found:
                return Path(found)
        return None

    def _check_garbro_gui_available(self) -> bool:
        try:
            from core.tool_manager import find_tool
            if find_tool("garbro_gui"):
                return True
        except Exception:
            pass
        return bool(shutil.which("GARbro.GUI.exe") or shutil.which("GARbro.GUI") or shutil.which("GARbro.exe"))

    def _iter_script_files(self, root: Path, limit: int | None = None):
        count = 0
        for current, dirs, files in root.walk():
            dirs[:] = [d for d in dirs if d not in self._SKIP_DIRS and not d.startswith(".")]
            for filename in sorted(files):
                path = Path(current) / filename
                if not self._is_script_file(path, root):
                    continue
                try:
                    if path.stat().st_size > 8 * 1024 * 1024:
                        continue
                except OSError:
                    continue
                yield path
                count += 1
                if limit is not None and count >= limit:
                    return

    def _iter_runtime_dump_files(self, root: Path):
        for current, dirs, files in root.walk():
            dirs[:] = [d for d in dirs if d not in self._SKIP_DIRS and not d.startswith(".")]
            for filename in sorted(files):
                path = Path(current) / filename
                suffix = path.suffix.lower()
                if suffix not in self._SCRIPT_EXTS and suffix:
                    continue
                try:
                    if path.stat().st_size > 8 * 1024 * 1024:
                        continue
                except OSError:
                    continue
                yield path

    def _is_script_file(self, path: Path, root: Path) -> bool:
        suffix = path.suffix.lower()
        if suffix in self._SCRIPT_EXTS:
            return True
        if suffix:
            return False
        try:
            rel = _norm_rel(path.relative_to(root))
        except ValueError:
            rel = ""
        if rel and rel in getattr(self, "_runtime_dump_rels", set()):
            return True
        return _is_probably_scriptless_kirikiri_payload(path)

    def _extract_script_file(self, script_file: Path, root: Path, *, from_xp3: bool) -> list[TextItem]:
        try:
            data = script_file.read_bytes()
        except OSError:
            return []
        if script_file.suffix.lower() == ".scn" or is_kirikiri_scn(data):
            return self._extract_scn_file(script_file, root, from_xp3=from_xp3)

        decoded = _read_text_guess(script_file)
        if decoded is None:
            return []

        rel = _norm_rel(script_file.relative_to(root))
        runtime_dump = rel in getattr(self, "_runtime_dump_rels", set())
        existing_meta = dict(self._script_meta.get(rel, {}))
        xp3_filter = existing_meta.get("xp3_filter") if isinstance(existing_meta.get("xp3_filter"), dict) else None
        item_encoding = decoded.encoding
        if xp3_filter and str(xp3_filter.get("kind") or "") == "single_byte_xor":
            item_encoding = str(xp3_filter.get("encoding") or decoded.encoding)
        with self._meta_lock:
            self._script_meta[rel] = {
                **existing_meta,
                "encoding": item_encoding,
                "newline": decoded.newline,
                "scramble_mode": decoded.scramble_mode,
                "from_xp3": from_xp3,
                "runtime_dump": runtime_dump,
            }

        items: list[TextItem] = []
        lines = decoded.text.split("\n")
        allow_plain_kag = _allow_plain_kag_script(rel)
        inside_iscript = False
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if script_file.suffix.lower() == ".ks":
                if re.match(r"^\[\s*iscript\b", stripped, re.I):
                    inside_iscript = True
                    continue
                if re.match(r"^\[\s*endscript\s*\]", stripped, re.I):
                    inside_iscript = False
                    continue
                if inside_iscript:
                    continue
            for span_idx, span in enumerate(_extract_kirikiri_text_spans(
                line,
                script_suffix=script_file.suffix.lower(),
                allow_plain_kag=allow_plain_kag,
            )):
                text = span.text
                if not is_translatable(text):
                    continue
                items.append(TextItem(
                    file=rel,
                    key=f"line_{idx}_span_{span_idx}",
                    original=text,
                    line=idx + 1,
                    context="message",
                    meta={
                        "kind": "message",
                        "engine": "kirikiri",
                        "encoding": item_encoding,
                        "newline": decoded.newline,
                        "scramble_mode": decoded.scramble_mode,
                        "from_xp3": from_xp3,
                        "runtime_dump": runtime_dump,
                        "xp3_filter": xp3_filter,
                        "span_start": span.start,
                        "span_end": span.end,
                    },
                ))
        return items

    def _extract_scn_file(self, script_file: Path, root: Path, *, from_xp3: bool) -> list[TextItem]:
        rel = _norm_rel(script_file.relative_to(root))
        runtime_dump = rel in getattr(self, "_runtime_dump_rels", set())
        try:
            data = script_file.read_bytes()
            extracted = extract_kirikiri_scn_texts_and_storage_refs(data)
            refs = extracted.texts
        except (PsbFormatError, OSError) as exc:
            debug(f"KiriKiri SCN 解析跳过 {rel}: {exc}")
            return []
        if runtime_dump:
            try:
                expanded_refs = _expand_kirikiri_storage_refs(extracted.storage_refs)
                with self._meta_lock:
                    self._explicit_dump_targets.update(expanded_refs)
            except Exception as exc:
                debug(f"KiriKiri SCN 引用目标更新失败 {rel}: {exc}")

        with self._meta_lock:
            self._script_meta[rel] = {
                "encoding": "psb-utf8",
                "newline": "\\n",
                "from_xp3": from_xp3,
                "format": "kirikiri_psb_scn",
                "runtime_dump": runtime_dump,
            }

        items: list[TextItem] = []
        for seq, ref in enumerate(refs):
            text = ref.text
            if not is_translatable(text):
                continue
            items.append(TextItem(
                file=rel,
                key=f"psb_{ref.index}_{seq}",
                original=text,
                line=seq + 1,
                context=ref.kind,
                meta={
                    "kind": ref.kind,
                    "engine": "kirikiri",
                    "format": "kirikiri_psb_scn",
                    "psb_index": ref.index,
                    "psb_path": ref.path,
                    "from_xp3": from_xp3,
                    "runtime_dump": runtime_dump,
                },
            ))
        if items:
            debug(f"KiriKiri SCN 提取 {rel}: {len(items)} 条")
        return items

    def _patch_script_file(
        self,
        path: Path,
        items: list[TextItem],
        sjis_tunnel_encoder: _KirikiriSjisTunnelEncoder | None = None,
        placeholder_map: dict[str, str] | None = None,
    ) -> bool:
        if path.suffix.lower() == ".scn" or any((item.meta or {}).get("format") == "kirikiri_psb_scn" for item in items):
            return self._patch_scn_file(path, items)

        decoded = _read_text_guess(path)
        if decoded is None:
            return False

        lines = decoded.text.split("\n")
        changed = 0
        indexed_items: list[tuple[int, int, TextItem]] = []
        fallback_items: list[TextItem] = []
        for item in items:
            if not (0 < item.line <= len(lines)):
                continue
            try:
                start = int((item.meta or {}).get("span_start"))
                end = int((item.meta or {}).get("span_end"))
            except (TypeError, ValueError):
                fallback_items.append(item)
                continue
            if start < 0 or end < start:
                fallback_items.append(item)
                continue
            indexed_items.append((start, end, item))

        for start, end, item in sorted(indexed_items, key=lambda row: (row[2].line, row[0]), reverse=True):
            line = lines[item.line - 1]
            if _is_unsafe_kag_command_attr_span(line, start, end):
                continue
            if end <= len(line) and line[start:end] == item.original:
                translated = _kirikiri_patch_text_value(item, sjis_tunnel_encoder, placeholder_map)
                lines[item.line - 1] = line[:start] + translated + line[end:]
                changed += 1
            else:
                fallback_items.append(item)

        for item in fallback_items:
            line = lines[item.line - 1]
            if _is_unsafe_kag_command_attr_item(line, item):
                continue
            translated = _kirikiri_patch_text_value(item, sjis_tunnel_encoder, placeholder_map)
            patched = line.replace(item.original, translated, 1)
            if patched != line:
                lines[item.line - 1] = patched
                changed += 1
                continue
            for idx, candidate in enumerate(lines):
                patched = candidate.replace(item.original, translated, 1)
                if patched != candidate:
                    lines[idx] = patched
                    changed += 1
                    break
        if not changed:
            return False

        newline = str(items[0].meta.get("newline") or decoded.newline or "\n")
        encoding = str(items[0].meta.get("encoding") or decoded.encoding or "utf-8")
        if any(
            isinstance((item.meta or {}).get("xp3_filter"), dict)
            and str(((item.meta or {}).get("xp3_filter") or {}).get("kind") or "") == "single_byte_xor"
            for item in items
        ):
            encoding = "utf-16"
        text = newline.join(lines)
        _write_text(path, text, encoding)
        debug(f"KiriKiri patched {path.name}: {changed} 条")
        return True

    def _patch_scn_file(self, path: Path, items: list[TextItem]) -> bool:
        replacements: dict[int, str] = {}
        conflicts = 0
        for item in items:
            if not item.translated or item.translated == item.original:
                continue
            try:
                index = int((item.meta or {}).get("psb_index"))
            except (TypeError, ValueError):
                continue
            existing = replacements.get(index)
            if existing is not None and existing != item.translated:
                conflicts += 1
                continue
            replacements[index] = item.translated
        if not replacements:
            return False
        try:
            patched, stats = patch_kirikiri_scn_texts(path.read_bytes(), replacements)
        except Exception as exc:
            warning(f"KiriKiri SCN 回填失败 {path.name}: {exc}")
            return False
        if stats.unsupported:
            warning(f"KiriKiri SCN 回填跳过加密/不支持文件: {path.name}")
            return False
        if stats.patched <= 0:
            debug(f"KiriKiri SCN 没有写入译文: {path.name} skipped={stats.skipped}")
            return False
        path.write_bytes(patched)
        debug(
            f"KiriKiri patched SCN {path.name}: {stats.patched} 条 "
            f"(skipped={stats.skipped}, conflicts={conflicts + stats.conflicts})"
        )
        return True
