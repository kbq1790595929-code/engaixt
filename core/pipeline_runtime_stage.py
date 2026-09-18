"""runtime/verify 阶段：回填输出验证（通用/KiriKiri/BGI/Godot PCK）、翻译安全校验、
注入器自动选择、运行时启动器/依赖准备、KiriKiri 运行时捕获模式、
回填前备份与安全写回、字体替换与游戏启动。

模块级函数收 pipeline 作第一参数（Pipeline 类保留委托薄壳），
与 core/pipeline_stage_runtime.py 的拆分模式一致。
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from config import get_config
from core.engine_capabilities import can_repack
from core.launcher import (
    create_bgi_hook_launcher,
    create_godot_display_hook_launcher,
    create_kirikiri_native_launcher,
    launch_game,
    launch_translated_launcher,
    launch_with_injector,
)
from core.pipeline_kirikiri_dump import _prepare_kirikiri_krkrpatch_runtime
from core.workspace import Workspace, safe_replace
from utils.logger import error, info, warning
from utils.text_extract import validation_source_for_item, verify_translation


def _write_translation_notice(game_path: Path, engine=None, *, mode: str = "") -> Path | None:
    game_dir = game_path if game_path.is_dir() else game_path.parent
    if not game_dir.exists():
        return None
    notice = game_dir / "EngAixt汉化说明.txt"
    engine_label = str(getattr(engine, "label", "") or getattr(engine, "name", "") or "自动识别")
    mode_text = mode.strip() or "自动管线"
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    text = f"""EngAixt 汉化说明

本目录已由 EngAixt 完成翻译或汉化运行环境部署。

官网地址：https://engaixt.com/

处理信息：
- 引擎/路线：{engine_label}
- 处理模式：{mode_text}
- 生成时间：{generated_at}

免责声明：
1. EngAixt 仅提供本机单机游戏个人自用翻译工具，不提供任何游戏本体、破解补丁或版权资源。
2. 本目录中的译文、补丁或启动器仅供已合法持有游戏的用户学习、研究与个人使用。
3. 请勿将包含游戏本体、商业素材、破解文件或未经授权资源的完整游戏目录公开传播。
4. 分享汉化成果前，请自行确认原游戏版权方、发行平台和当地法律法规允许。
5. AI 译文可能存在错译、漏译、不自然表达或不适内容，请以原文和游戏官方内容为准。
"""
    try:
        notice.write_text(text, encoding="utf-8")
        return notice
    except OSError as exc:
        warning(f"写入 EngAixt 汉化说明失败: {exc}")
        return None


def enter_kirikiri_runtime_capture_mode(
    pipeline,
    game_path: Path,
    engine,
    injector: str | None,
    checkpoint: Path | None,
    *,
    launch: bool,
    extract_only: bool,
) -> bool:
    """Bootstrap KiriKiri runtime capture when static extraction finds no text."""
    game_dir = game_path if game_path.is_dir() else game_path.parent
    meta_dir = game_dir / "_translation_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    capture_path = meta_dir / "kirikiri_runtime_capture.jsonl"
    if not capture_path.exists():
        capture_path.write_text("", encoding="utf-8")

    warning("KiriKiri 静态提取为空，切换到运行时 hook 捕获模式")
    info("KiriKiri 捕获模式：启动游戏后推进到文本，hook 会记录新文本；之后重新运行管线即可补翻")
    if pipeline.diagnostics:
        pipeline.diagnostics.set("runtime_overlay_only", True)
        pipeline.diagnostics.set("kirikiri_runtime_capture_mode", {
            "enabled": True,
            "reason": "static_extract_empty",
            "capture": str(capture_path),
            "extract_only": bool(extract_only),
            "launch": bool(launch),
        })
        pipeline.diagnostics.set("runtime_dump_required", False)
        pipeline.diagnostics.set("static_decrypt_required", False)
        pipeline.diagnostics.warn(
            "KiriKiri 静态提取为空，已进入运行时 hook 捕获模式",
            capture=str(capture_path),
        )
        pipeline.diagnostics.suggest(
            "启动汉化版后推进到剧情文本；关闭游戏后重新点击开始翻译，管线会合并捕获文本并离线补翻。"
        )

    launcher: Path | None = None
    if injector == "frida":
        info("KiriKiri 使用 Frida 捕获模式启动")
    else:
        launcher = create_kirikiri_native_launcher(game_path, engine=engine, checkpoint=checkpoint)
        if launcher:
            if pipeline.diagnostics:
                pipeline.diagnostics.set("kirikiri_native_launcher", str(launcher))
            if pipeline.manifest:
                pipeline.manifest.record_created(launcher, kind="runtime", runtime_required=True)
        else:
            message = "KiriKiri 原生 hook 启动器创建失败，无法进入默认捕获模式"
            warning(message)
            if pipeline.diagnostics:
                pipeline.diagnostics.warn(message)
                return pipeline._fail_stage_code(
                    "runtime",
                    "runtime_prepare_failed",
                    detail="KRKR 原生 Hook 启动器创建失败",
                    rollback=True,
                    next_actions=("重新安装完整发布包，或点击改用实时翻译前先补齐运行时组件。",),
                )
            return False

    pipeline._update_progress("runtime_capture", 90)
    # This compatibility helper is now preparation-only. A static failure must
    # never spend API credit or start a game behind the user's back.
    info("已生成 KiriKiri 运行时捕获启动器；等待用户点击“改用实时翻译”")

    pipeline._write_completion_notice(game_path, engine, mode="KiriKiri 实时捕获/显示层")
    return True

def record_modified_outputs(pipeline, game_path: Path, items: list, engine):
    from core import pipeline as _pipeline_mod
    if not pipeline.manifest:
        return
    if engine is not None and not can_repack(engine):
        return
    engine_name = getattr(engine, "name", "") if engine else ""
    game_dir = game_path if game_path.is_dir() else game_path.parent
    target_dir = game_dir / "game" if engine_name == "renpy" and (game_dir / "game").is_dir() else game_dir
    modified_targets: set[Path] = set()
    if engine_name == "kirikiri":
        stale_rels = {
            str(getattr(item, "file", ""))
            for item in items
            if not _kirikiri_should_copy_loose_script(item)
        }
        pipeline.manifest.forget_modified_rels(stale_rels)
        for archive_name in _kirikiri_static_xp3_archives(items):
            archive = game_dir / archive_name
            if archive.exists() and archive.is_file():
                modified_targets.add(archive)
            sig = archive.with_name(archive.name + ".sig")
            if sig.exists() and sig.is_file():
                modified_targets.add(sig)

    for item in items:
        if not _pipeline_mod._has_effective_translation(item):
            continue
        if engine_name == "kirikiri" and not _kirikiri_should_copy_loose_script(item):
            continue
        target = target_dir / item.file
        if target.exists() and target.is_file():
            modified_targets.add(target)
        target_loc = item.meta.get("target_loc") if getattr(item, "meta", None) else ""
        if target_loc:
            loc_path = target_dir / target_loc
            if loc_path.exists() and loc_path.is_file():
                modified_targets.add(loc_path)
    if engine_name == "gamemaker":
        data_win = _pipeline_mod._find_gamemaker_data_win(game_dir)
        if data_win:
            modified_targets.add(data_win)
    if engine_name == "wolf" and hasattr(engine, "font_targets"):
        modified_targets.update(engine.font_targets(game_dir))

    # Archive engines can emit tens of thousands of TextItems for one file.
    # Hashing and saving the same target per item turns bookkeeping into hours
    # of unnecessary I/O after a successful repack.
    for target in sorted(modified_targets, key=lambda path: str(path).casefold()):
        pipeline.manifest.record_modified(target)

def verify_repack_outputs(pipeline, game_path: Path, items: list, engine) -> dict:
    """Best-effort verification that translated text reached a writable target."""
    from core import pipeline as _pipeline_mod
    translated = [it for it in items if _pipeline_mod._has_effective_translation(it)]
    engine_name = getattr(engine, "name", "") if engine else ""
    result = {
        "engine": engine_name,
        "translated_items": len(translated),
        "checked": False,
        "hits": 0,
        "files_checked": 0,
        "note": "",
    }
    if engine is not None and not can_repack(engine):
        result["note"] = "当前引擎为仅提取模式，未执行自动写回，跳过回填命中验证"
        return result
    if not translated:
        result["note"] = "没有已翻译条目可验证"
        return result

    if engine_name == "godot_pck":
        return pipeline._verify_godot_pck_outputs(game_path, translated, engine, result)

    if engine_name == "bgi":
        return pipeline._verify_bgi_outputs(game_path, translated, engine, result)

    if engine_name == "wolf":
        verification = getattr(engine, "_last_repack_verification", {}) or {}
        outputs = verification.get("outputs", [])
        applied = int(verification.get("applied_translations", 0) or 0)
        archive_roundtrip = bool(verification.get("archive_roundtrip_verified"))
        engine_diagnostics = {}
        diagnostics_snapshot = getattr(engine, "diagnostics_snapshot", None)
        if callable(diagnostics_snapshot):
            try:
                snapshot = diagnostics_snapshot()
                if isinstance(snapshot, dict):
                    engine_diagnostics = snapshot
            except Exception as exc:
                # Diagnostics must never turn a successful repack into a failure.
                engine_diagnostics = {"snapshot_error": str(exc)}
        result.update({
            "checked": bool(verification.get("bridge_verified")),
            "hits": applied if verification.get("bridge_verified") else 0,
            "files_checked": len(outputs),
            "outputs": outputs,
            "archives_repacked": int(verification.get("archives_repacked", 0) or 0),
            "bridge_verified": bool(verification.get("bridge_verified")),
            "archive_roundtrip_verified": archive_roundtrip,
            "archive_roundtrip_runtime_keys": verification.get("archive_roundtrip_runtime_keys", {}),
            "engine_diagnostics": engine_diagnostics,
            "tool_runs": verification.get("tool_runs", []),
            "note": (
                "WOLF 重封归档已二次解包、重新解析并校验运行时键"
                if archive_roundtrip
                else "WOLF 松散数据已在写回前重新解析校验"
            ),
        })
        return result

    if engine_name == "tyrano" and hasattr(engine, "verify_repack"):
        game_dir = game_path if game_path.is_dir() else game_path.parent
        return engine.verify_repack(game_dir, translated, result)

    if engine_name == "kirikiri":
        from core.kirikiri_repack_verifier import verify_kirikiri_repack_outputs
        return verify_kirikiri_repack_outputs(
            game_path,
            translated,
            result,
            runtime_resource_overlay=bool(getattr(engine, "_runtime_resource_overlay", False)),
        )

    if engine_name in ("xunity_realtime", "godot_frida", "unity_arch000_lua"):
        result["note"] = "该引擎写入封包/运行时缓存，跳过通用文本验证"
        return result

    game_dir = game_path if game_path.is_dir() else game_path.parent
    if engine_name == "renpy" and (game_dir / "game").is_dir():
        game_dir = game_dir / "game"

    files: dict[str, list] = {}
    for item in translated:
        files.setdefault(item.file, []).append(item)

    hits = 0
    checked = 0
    for rel, file_items in list(files.items())[:50]:
        target = game_dir / rel
        if not target.exists():
            continue
        try:
            if target.suffix.lower() == ".exe":
                content_bytes = target.read_bytes()
                checked += 1
                for item in file_items[:20]:
                    if item.translated.encode("utf-8", errors="ignore") in content_bytes:
                        hits += 1
                continue
            content = target.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        checked += 1
        for item in file_items[:20]:
            if item.translated in content:
                hits += 1
    result.update({
        "checked": checked > 0,
        "hits": hits,
        "files_checked": checked,
        "note": "通用文本命中验证" if checked else "未找到可直接读取的回填文件",
    })
    if checked and hits == 0 and pipeline.diagnostics:
        pipeline.diagnostics.warn("回填验证未命中译文，可能没有成功写回目标文件")
        pipeline.diagnostics.suggest("检查引擎回填器是否修改了 workspace/original，并确认 copy_back 没有被跳过。")
    return result

def verify_kirikiri_runtime_overlay_outputs(pipeline, game_path: Path, translated: list, result: dict) -> dict:
    from core.kirikiri_repack_verifier import verify_kirikiri_repack_outputs

    return verify_kirikiri_repack_outputs(
        game_path,
        translated,
        result,
        runtime_resource_overlay=True,
    )

def verify_bgi_outputs(pipeline, game_path: Path, translated: list, engine, result: dict) -> dict:
    """Verify translated BGI strings in the active ARC files."""
    from core import pipeline as _pipeline_mod
    import json as _json
    from engines.bgi import _read_bgi_archive
    from utils.bgi_dsc import DSC_MAGIC, decode_sjis_tunnel_bytes, decompress_dsc, extract_bgi_strings

    game_dir = game_path if game_path.is_dir() else game_path.parent
    by_arc: dict[str, list] = {}
    for item in translated:
        arc = ""
        if getattr(item, "meta", None):
            arc = str(item.meta.get("arc") or "")
        if not arc and "::" in item.file:
            arc = item.file.split("::", 1)[0]
        if arc:
            by_arc.setdefault(arc.replace("\\", "/"), []).append(item)

    files_checked = 0
    scripts_checked = 0
    active_refs = 0
    active_cjk_refs = 0
    active_kana_refs = 0
    sample_hits = 0
    sample_checked = 0
    missing_arcs: list[str] = []
    per_arc: list[dict[str, object]] = []
    tunnel_table = b""
    try:
        sjis_ext = game_dir / "sjis_ext.bin"
        if sjis_ext.exists():
            tunnel_table = sjis_ext.read_bytes()
    except Exception:
        tunnel_table = b""

    for arc_rel, arc_items in sorted(by_arc.items()):
        arc_path = game_dir / arc_rel
        if not arc_path.exists():
            missing_arcs.append(arc_rel)
            continue
        try:
            _kind, entries = _read_bgi_archive(arc_path)
        except Exception:
            continue
        if not entries:
            continue
        files_checked += 1
        refs_by_entry: dict[str, list[str]] = {}
        for entry_name, _offset, _size, data in entries:
            refs = extract_bgi_strings(data)
            if not refs:
                continue
            scripts_checked += 1
            script = data
            try:
                if data.startswith(DSC_MAGIC):
                    script = decompress_dsc(data)
            except Exception:
                script = data
            texts = []
            for ref in refs:
                raw_text = ref.text
                end = script.find(b"\x00", ref.text_offset)
                if 0 <= ref.text_offset < len(script) and end >= ref.text_offset:
                    try:
                        raw_text = decode_sjis_tunnel_bytes(script[ref.text_offset:end], tunnel_table)
                    except Exception:
                        raw_text = ref.text
                texts.append(raw_text)
            refs_by_entry[entry_name] = texts
            active_refs += len(texts)
            active_cjk_refs += sum(1 for text in texts if _pipeline_mod._contains_cjk(text))
            active_kana_refs += sum(1 for text in texts if _pipeline_mod._contains_japanese_kana(text))

        arc_sample_checked = 0
        arc_sample_hits = 0
        for item in arc_items[:80]:
            entry = str(item.meta.get("entry") or "") if getattr(item, "meta", None) else ""
            if not entry or entry not in refs_by_entry:
                continue
            sample_checked += 1
            arc_sample_checked += 1
            texts = refs_by_entry[entry]
            translated_text = item.translated or ""
            if translated_text and any(
                translated_text == text
                or translated_text.rstrip("。") == text.rstrip("。")
                for text in texts
            ):
                sample_hits += 1
                arc_sample_hits += 1
        per_arc.append({
            "arc": arc_rel,
            "items": len(arc_items),
            "sample_checked": arc_sample_checked,
            "sample_hits": arc_sample_hits,
        })

    result.update({
        "checked": files_checked > 0,
        "hits": sample_hits,
        "files_checked": files_checked,
        "scripts_checked": scripts_checked,
        "checked_items": sample_checked,
        "active_refs": active_refs,
        "active_cjk_refs": active_cjk_refs,
        "active_kana_refs": active_kana_refs,
        "missing_arcs": missing_arcs[:20],
        "per_arc": per_arc,
        "note": "BGI active ARC string verification",
    })
    try:
        out = _pipeline_mod._game_meta_path(game_path, "bgi_repack_verification.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        warning(f"BGI 回填验证报告写入失败: {exc}")
    if files_checked:
        info(
            "BGI 活动包验证: "
            f"{files_checked} ARC/{scripts_checked} scripts，"
            f"中文 refs {active_cjk_refs}/{active_refs}，"
            f"假名残留 refs {active_kana_refs}，"
            f"抽样命中 {sample_hits}/{sample_checked}"
        )
    if files_checked and active_cjk_refs == 0 and pipeline.diagnostics:
        pipeline.diagnostics.warn("BGI 活动 ARC 未检测到中文译文，可能没有成功写回")
    return result

def verify_godot_pck_outputs(pipeline, game_path: Path, translated: list, engine, result: dict) -> dict:
    """Verify Godot PCK repack against the actual patched payloads."""
    from core import pipeline as _pipeline_mod
    patched_map = getattr(engine, "_last_patched_files", {}) or {}
    effective_maps = getattr(engine, "_last_effective_translations", {}) or {}
    if not patched_map:
        result.update({
            "checked": False,
            "hits": 0,
            "files_checked": 0,
            "note": "Godot PCK 未记录到本次 patched payload",
        })
        return result

    deploy_mode = getattr(engine, "_last_deploy_mode", "") or "unknown"
    deployed_payloads: dict[str, bytes] | None = None
    missing_pck_payloads = 0
    if deploy_mode == "pck":
        pck_path = (
            getattr(engine, "_deploy_pck_path", None)
            or getattr(engine, "_last_output_pck", None)
            or getattr(engine, "_pck_path", None)
        )
        if pck_path and Path(pck_path).exists():
            try:
                from engines.godot_pck import read_pck_payloads
                deployed_payloads = read_pck_payloads(Path(pck_path), patched_map.keys())
            except Exception as exc:
                if pipeline.diagnostics:
                    pipeline.diagnostics.warn("Godot PCK 最终包读取校验失败", error=str(exc))

    by_norm: dict[str, list] = {}
    for item in translated:
        by_norm.setdefault(item.file.replace("\\", "/"), []).append(item)

    checked_items = 0
    hits = 0
    original_residuals = 0
    files_checked = 0
    for rel, expected_data in patched_map.items():
        file_items = by_norm.get(rel)
        if not file_items:
            continue
        if deployed_payloads is not None:
            data = deployed_payloads.get(rel)
            if data is None:
                missing_pck_payloads += 1
                data = b""
        else:
            data = expected_data
        files_checked += 1
        ext = Path(rel).suffix.lower()
        variant_values = None
        if ext in (".scn", ".res"):
            variant_values = _pipeline_mod._godot_variant_string_values(data)
        for item in file_items[:20]:
            checked_items += 1
            effective_translation = (
                effective_maps.get(rel, {}).get(item.original)
                if isinstance(effective_maps.get(rel, {}), dict)
                else None
            ) or item.translated
            translated_bytes = effective_translation.encode("utf-8", errors="ignore")
            original_bytes = item.original.encode("utf-8", errors="ignore")
            if variant_values is not None:
                if (
                    effective_translation in variant_values
                    or effective_translation in {v.rstrip(" ") for v in variant_values}
                ):
                    hits += 1
                if item.original in variant_values:
                    original_residuals += 1
            else:
                if translated_bytes and translated_bytes in data:
                    hits += 1
                if original_bytes and original_bytes in data:
                    original_residuals += 1

    game_dir = game_path if game_path.is_dir() else game_path.parent
    deployed_checked = 0
    missing_deployed_files = 0
    if deploy_mode == "loose":
        for rel in patched_map:
            target = game_dir / rel
            if target.exists() and target.is_file():
                deployed_checked += 1
            else:
                missing_deployed_files += 1
    elif deploy_mode == "pck":
        pck_path = getattr(engine, "_deploy_pck_path", None) or getattr(engine, "_pck_path", None)
        output_pck = getattr(engine, "_last_output_pck", None)
        if pck_path and Path(pck_path).exists():
            deployed_checked = 1
        elif output_pck and Path(output_pck).exists():
            deployed_checked = 1

    result.update({
        "checked": files_checked > 0,
        "hits": hits,
        "files_checked": files_checked,
        "checked_items": checked_items,
        "original_residuals": original_residuals,
        "deploy_mode": deploy_mode,
        "deployed_checked": deployed_checked,
        "missing_deployed_files": missing_deployed_files,
        "actual_pck_payload_checked": deployed_payloads is not None,
        "missing_pck_payloads": missing_pck_payloads,
        "note": "Godot PCK patched payload 验证",
    })

    if files_checked and hits == 0 and pipeline.diagnostics:
        pipeline.diagnostics.warn("Godot PCK 回填验证未命中译文，可能没有成功写入 patched payload")
    if missing_pck_payloads and pipeline.diagnostics:
        pipeline.diagnostics.warn("Godot PCK 最终包缺少预期补丁 payload", missing=missing_pck_payloads)
    if original_residuals and pipeline.diagnostics:
        pipeline.diagnostics.warn(
            "Godot PCK 回填后仍有原文残留",
            residuals=original_residuals,
            checked_items=checked_items,
        )
    if deploy_mode == "loose" and missing_deployed_files and pipeline.diagnostics:
        pipeline.diagnostics.warn("Godot PCK 松散文件部署缺少目标文件", missing=missing_deployed_files)
    return result

def validate_all(pipeline, items: list) -> list:
    """翻译后强制安全校验 —— 防线 1。"""
    from core import pipeline as _pipeline_mod
    translated_items = [it for it in items if _pipeline_mod._has_effective_translation(it)]
    total = len(translated_items)
    if total == 0:
        return items

    # 大批量时显示进度
    report_every = max(1, total // 20)  # 每 5% 报告一次
    for idx, item in enumerate(translated_items):
        safe, warns = verify_translation(validation_source_for_item(item), item.translated)
        if safe != item.translated:
            pipeline._blocked_count += 1
            if not getattr(item, "meta", None):
                item.meta = {}
            item.meta["validation_failed_translation"] = item.translated
            item.translated = safe
        for w in warns:
            warning(f"校验警告 [{item.original[:30]}...]: {w}")
        if (idx + 1) % report_every == 0:
            pct = 55 + (idx + 1) / total * 10  # 55% → 65%
            pipeline._update_progress(f"安全校验 ({idx + 1}/{total})", pct)
    return items

def sanitize_engine_translations(engine, items: list, target_lang: str) -> list:
    sanitizer = getattr(engine, "sanitize_translations", None)
    if not callable(sanitizer):
        return items
    return sanitizer(items, target_lang)

def stop_translation_proxy(engine):
    """停止翻译代理服务器（--no-launch 模式使用）。"""
    if engine and hasattr(engine, "_translation_proxy") and engine._translation_proxy:
        engine._translation_proxy.stop()
        info("翻译代理服务器已停止")

def auto_select_injector(pipeline, injector: str | None, engine, game_path: Path) -> str | None:
    if injector:
        return injector
    engine_name = getattr(engine, "name", "")
    selected: str | None = None
    if engine_name == "xunity_realtime":
        selected = "xunity"
        info("Unity 引擎检测到，自动启用 XUnity 运行时注入模式")
    elif engine_name == "bgi":
        selected = None
        info("BGI 引擎检测到，将生成原生免依赖汉化启动器")
    elif engine_name == "kirikiri":
        selected = None
        info("KiriKiri 引擎检测到，默认使用原生显示层 hook，不做静态脚本回填")
    elif engine_name == "godot_pck":
        selected = None
        info("Godot PCK 引擎检测到，使用静态 PCK 回填模式，直接启动 exe 无需 hook")
    elif engine_name in {"godot", "godot_frida"}:
        selected = "frida"
        info("Godot 引擎检测到，默认使用离线译文 + 显示层 hook，跳过静态 PCK/资源写回")
    elif engine_name == "rpgmaker" and _is_runtime_frida_mode("frida", engine, game_path):
        selected = "frida"
        info("RPG Maker 引擎检测到，自动启用 Frida 运行时 hook 模式")
    if selected and pipeline.diagnostics:
        pipeline.diagnostics.set("injector", selected)
    return selected

def create_runtime_launchers(pipeline, game_path: Path, engine, checkpoint: Path | None) -> None:
    engine_name = getattr(engine, "name", "")
    if engine_name == "bgi":
        launcher = create_bgi_hook_launcher(game_path, engine=engine, checkpoint=checkpoint)
        diag_key = "bgi_hook_launcher"
    elif engine_name == "kirikiri":
        launcher = create_kirikiri_native_launcher(game_path, engine=engine, checkpoint=checkpoint)
        diag_key = "kirikiri_native_launcher"
    elif engine_name in {"godot", "godot_frida"}:
        launcher = create_godot_display_hook_launcher(game_path, engine=engine, checkpoint=checkpoint)
        diag_key = "godot_display_hook_launcher"
    else:
        return
    if not launcher:
        return
    if pipeline.diagnostics:
        pipeline.diagnostics.set(diag_key, str(launcher))
    if pipeline.manifest:
        pipeline.manifest.record_created(launcher, kind="runtime", runtime_required=True)

def prepare_runtime_dependencies(pipeline, game_path: Path, engine) -> None:
    if getattr(engine, "name", "") in {"godot", "godot_frida"}:
        restored = pipeline._restore_godot_static_patch_artifacts_for_runtime(game_path)
        if restored and pipeline.diagnostics:
            pipeline.diagnostics.set("godot_static_patch_restored_for_runtime", restored)
    if getattr(engine, "name", "") == "kirikiri":
        if bool(getattr(engine, "_kirikiri_xp3pack_used", False)):
            pipeline._prepare_kirikiri_unencrypted_version_bridge(game_path, engine)
        elif bool(getattr(engine, "_runtime_resource_overlay", False)):
            pipeline._prepare_kirikiri_patch_bridge_tooling(game_path, engine)

def restore_godot_static_patch_artifacts_for_runtime(pipeline, game_path: Path) -> list[str]:
    """Restore .pre_tool PCK/EXE backups before using the Godot display hook."""
    game_dir = game_path if game_path.is_dir() else game_path.parent
    restored: list[str] = []
    for backup in list(game_dir.glob("*.pck.pre_tool")) + list(game_dir.glob("*.exe.pre_tool")):
        target = backup.with_name(backup.name[:-len(".pre_tool")])
        try:
            if target.exists() and backup.stat().st_size == target.stat().st_size:
                continue
            shutil.copy2(backup, target)
            restored.append(str(target))
            info(f"Godot runtime hook 已恢复原始文件: {target.name}")
        except OSError as exc:
            warning(f"Godot runtime hook 恢复原始文件失败: {backup} - {exc}")
    return restored

def disable_kirikiri_static_patch_artifacts_for_runtime(pipeline, game_path: Path, engine) -> None:
    if getattr(engine, "name", "") != "kirikiri":
        return
    game_dir = game_path if game_path.is_dir() else game_path.parent
    meta_dir = game_dir / "_translation_meta"
    targets = [
        game_dir / "patch.xp3",
        meta_dir / "kirikiri_patch.xp3",
        meta_dir / "kirikiri_patch",
        meta_dir / "kirikiri_patch_manifest.txt",
    ]
    disabled: list[str] = []
    for target in targets:
        if not target.exists():
            continue
        backup = target.with_name(target.name + ".disabled_runtime_overlay")
        try:
            if backup.exists():
                if backup.is_dir():
                    shutil.rmtree(backup)
                else:
                    backup.unlink()
            target.rename(backup)
            disabled.append(str(backup))
        except OSError as exc:
            warning(f"KiriKiri runtime overlay 无法禁用旧静态补丁: {target} - {exc}")
    if disabled:
        info(f"KiriKiri runtime overlay 已禁用旧静态补丁入口: {len(disabled)} 个")
        if pipeline.diagnostics:
            pipeline.diagnostics.set("kirikiri_static_patch_disabled_for_runtime", disabled)

def prepare_kirikiri_patch_bridge_tooling(pipeline, game_path: Path, engine) -> None:
    game_dir = game_path if game_path.is_dir() else game_path.parent
    static_diag = game_dir / "_translation_meta" / "kirikiri_static_xp3_rebuild.json"
    if static_diag.exists():
        try:
            import json as _json
            data = _json.loads(static_diag.read_text(encoding="utf-8-sig"))
            if data.get("rebuilt_archives"):
                info("KiriKiri static XP3 rewrite present; skipping patch bridge archives")
                return
        except Exception:
            pass
    patch_archives: list[str] = []
    has_archive_patch = False
    if (game_dir / "_translation_meta" / "kirikiri_patch.xp3").exists():
        patch_archives.append("_translation_meta/kirikiri_patch.xp3")
        has_archive_patch = True
    if (game_dir / "patch.xp3").exists():
        patch_archives.append("patch.xp3")
        has_archive_patch = True
    if not has_archive_patch and (game_dir / "_translation_meta" / "kirikiri_patch").is_dir():
        patch_archives.append("_translation_meta/kirikiri_patch")
    if not patch_archives:
        return
    try:
        from core.launcher import _select_launch_exe
        exe_path = _select_launch_exe(game_path, engine)
    except Exception:
        exe_path = None
    if not exe_path:
        return
    result = _prepare_kirikiri_krkrpatch_runtime(game_dir, exe_path, patch_archives)
    if pipeline.diagnostics:
        pipeline.diagnostics.set("kirikiri_krkrpatch_bridge", result)
    if pipeline.manifest and result.get("ok"):
        for key in ("loader", "dll", "config"):
            value = result.get(key)
            if value:
                pipeline.manifest.record_created(Path(str(value)), kind="runtime", runtime_required=True)


def prepare_kirikiri_unencrypted_version_bridge(pipeline, game_path: Path, engine) -> dict[str, object]:
    """Install KirikiriTools' version.dll only when Xp3Pack was actually used."""
    if getattr(engine, "name", "") != "kirikiri":
        return {"ok": False, "status": "wrong_engine"}
    game_dir = game_path if game_path.is_dir() else game_path.parent
    target = game_dir / "version.dll"
    result: dict[str, object] = {
        "ok": False,
        "status": "missing",
        "target": str(target),
        "source": "",
    }
    try:
        from core.tool_manager import find_tool
        source = find_tool("kirikiri_unencrypted_version")
    except Exception:
        source = None
    if target.exists():
        result.update({"ok": True, "status": "existing_game_bridge"})
        info("KiriKiri 已存在 version.dll，保留现有桥接文件")
    elif source and source.is_file():
        try:
            shutil.copy2(source, target)
            result.update({"ok": True, "status": "installed", "source": str(source)})
            info("KirikiriTools version.dll 已部署，用于加载 Xp3Pack 补丁")
            if pipeline.manifest:
                pipeline.manifest.record_created(target, kind="runtime", runtime_required=True)
        except OSError as exc:
            result.update({"status": "copy_failed", "detail": str(exc)})
            warning(f"KiriKiri version.dll 部署失败: {exc}")
    else:
        result.update({"status": "tool_missing", "detail": "kirikiri_unencrypted_version not found"})
        warning("Xp3Pack 补丁需要 KirikiriTools version.dll，但发布包中未找到")
    if pipeline.diagnostics:
        pipeline.diagnostics.set("kirikiri_unencrypted_version_bridge", result)
    return result

def setup_engine_repack(pipeline, engine, game_path: Path):
    """在 repack 前设置引擎的上下文（游戏目录、工作区路径等）。

    某些引擎需要在 repack 时知道游戏目录和工作区位置，
    而这些信息在 --from-json 恢复模式下不会通过 unpack 传递。
    """
    game_dir = game_path if game_path.is_dir() else game_path.parent
    engine._game_dir = game_dir  # 动态属性，所有引擎都接受
    engine._manifest = pipeline.manifest

def verify_repack_integrity(pipeline, items: list, engine) -> bool:
    """回填前完整性预检：确保翻译不会破坏游戏文件结构。"""
    from core import pipeline as _pipeline_mod
    if not items:
        return True

    translated = [it for it in items if _pipeline_mod._has_effective_translation(it)]
    if not translated:
        info("没有已翻译条目，跳过完整性检查")
        return True

    engine_name = getattr(engine, "name", "")
    issues = 0

    # 通用检查：翻译不应包含裸控制字符
    for it in translated[:1000]:  # 只抽样前 1000 条，避免全量扫描
        if any(0 < ord(c) < 32 and c not in "\n\r\t" for c in it.translated):
            warning(f"[回填预检] 翻译含控制字符: {it.file} key={it.key[:50]}")
            issues += 1

    # RPG Maker JSON 特定检查
    if engine_name == "rpgmaker":
        import json as _json
        workspace_original = pipeline.workspace.root / "original" if pipeline.workspace else None
        if workspace_original and workspace_original.exists():
            for it in translated[:200]:
                target = workspace_original / it.file
                if not target.exists() or not target.suffix == ".json":
                    continue
                try:
                    data = _json.loads(target.read_text(encoding="utf-8"))
                except _json.JSONDecodeError as e:
                    error(f"[回填预检] JSON 解析失败: {it.file} — {e}")
                    issues += 1

    if issues > 0:
        warning(f"[回填预检] 发现 {issues} 个潜在问题，将继续回填但建议复查")
    else:
        info(f"[回填预检] 完整性检查通过 ({len(translated)} 条翻译)")

    if pipeline.diagnostics:
        pipeline.diagnostics.set("repack_integrity", {
            "checked": min(len(translated), 1000),
            "issues": issues,
            "passed": issues == 0,
        })

    return True  # 不阻塞，有问题只告警

def backup_game_files(pipeline, game_path: Path, items: list | None = None, engine=None):
    """防线 4: 在回填写入前，对受影响的游戏文件建立安全备份。"""
    from core import pipeline as _pipeline_mod
    game_dir = game_path if game_path.is_dir() else game_path.parent

    # 只备份实际会被修改的文件（item 中引用的文件）
    files_to_backup: set[Path] = set()
    if items:
        engine_name = getattr(engine, "name", "")
        if engine_name == "kirikiri":
            for archive_name in _kirikiri_static_xp3_archives(items):
                archive = game_dir / archive_name
                if archive.exists():
                    files_to_backup.add(archive)
                sig = archive.with_name(archive.name + ".sig")
                if sig.exists():
                    files_to_backup.add(sig)
            # Static KRKR writes patch-layer files directly under the game
            # directory. Back up pre-existing layers so a failed verification
            # can restore them byte-for-byte instead of merely deleting new files.
            patch_targets = [
                game_dir / "patch.xp3",
                game_dir / "_translation_meta" / "kirikiri_patch.xp3",
                game_dir / "_translation_meta" / "kirikiri_patch_manifest.txt",
            ]
            patch_dir = game_dir / "_translation_meta" / "kirikiri_patch"
            if patch_dir.is_dir():
                patch_targets.extend(path for path in patch_dir.rglob("*") if path.is_file())
            files_to_backup.update(path for path in patch_targets if path.is_file())
        for item in items:
            if engine_name == "kirikiri" and not _kirikiri_should_copy_loose_script(item):
                continue
            f = game_dir / item.file
            if not f.exists() and (game_dir / "game").is_dir():
                f = game_dir / "game" / item.file
            if f.exists():
                files_to_backup.add(f)
            target_loc = item.meta.get("target_loc") if getattr(item, "meta", None) else ""
            if target_loc:
                loc_path = game_dir / target_loc
                if loc_path.exists():
                    files_to_backup.add(loc_path)
        if any(
            _pipeline_mod._has_effective_translation(item)
            and _pipeline_mod._contains_cjk(item.translated)
            for item in items
        ):
            data_win = _pipeline_mod._find_gamemaker_data_win(game_dir)
            if data_win:
                files_to_backup.add(data_win)
        if engine_name == "wolf" and hasattr(engine, "font_targets"):
            files_to_backup.update(engine.font_targets(game_dir))
    else:
        # 没有 item 信息时，只备份 game/ 下的脚本文件
        for f in game_dir.glob("game/*.rpy"):
            files_to_backup.add(f)

    backup_count = 0
    for f in files_to_backup:
        try:
            if pipeline.manifest:
                pipeline.manifest.backup_file(f)
                backup_count += 1
            elif not Path(str(f) + ".bak").exists():
                safe_replace(f)
                backup_count += 1
        except Exception:
            pass
    if backup_count > 0:
        info(f"已为 {backup_count} 个游戏文件建立安全备份")

def _has_runtime_capture_items(items: list) -> bool:
    return any(bool(getattr(item, "meta", {}).get("runtime_capture")) for item in items)


def _should_use_runtime_overlay(
    injector: str | None,
    engine,
    game_path: Path,
    items: list | None = None,
) -> bool:
    if _is_runtime_frida_mode(injector, engine, game_path):
        return True
    return _kirikiri_should_use_runtime_overlay(engine, game_path, items)


def _should_use_runtime_resource_overlay(
    injector: str | None,
    engine,
    game_path: Path,
    items: list | None = None,
) -> bool:
    if injector == "frida":
        return False
    if getattr(engine, "name", "") != "kirikiri":
        return False
    return bool(getattr(get_config(), "kirikiri_enable_static_patch", False)) and _kirikiri_should_use_runtime_overlay(engine, game_path, items)


def _mark_runtime_resource_overlay(engine, enabled: bool) -> None:
    if getattr(engine, "name", "") == "kirikiri":
        setattr(engine, "_runtime_resource_overlay", bool(enabled))


def _runtime_overlay_note(injector: str | None, engine) -> str:
    engine_name = getattr(engine, "name", "") if engine else ""
    if injector == "frida":
        if engine_name in {"godot", "godot_pck", "godot_frida"}:
            return "Godot 显示层 hook 模式：保留原始 PCK/资源，使用本地离线译文表替换运行时显示文本"
        return "Frida 运行时翻译模式：跳过静态资源回填，使用显示层 hook 翻译"
    if engine_name == "kirikiri":
        return "KiriKiri 运行时显示层 hook 模式：保留原始 XP3/脚本，使用本地译文表替换显示文本"
    return "运行时 hook 模式：跳过静态资源回填，使用显示层 hook 翻译"


def _kirikiri_should_use_runtime_overlay(engine, game_path: Path, items: list | None = None) -> bool:
    from core import pipeline as _pipeline_mod
    if getattr(engine, "name", "") != "kirikiri":
        return False
    if not bool(getattr(get_config(), "kirikiri_enable_static_patch", False)):
        return True
    if _has_runtime_capture_items(items or []):
        return True
    if _kirikiri_items_have_protected_xp3_filter(items):
        return True
    if bool(getattr(engine, "_protected_archives", [])):
        return True
    if int(getattr(engine, "_protected_script_count", 0) or 0) > 0:
        return True
    diag = _pipeline_mod._read_game_meta_json(game_path, "extract_diagnostics.json")
    if not diag:
        return False
    if int(diag.get("protected_script_count") or 0) > 0:
        return True
    if diag.get("protected_archives"):
        return True
    layer = str(diag.get("protection_layer") or "")
    if layer in {"content_filtered", "index_obfuscated", "index_and_content"}:
        return True
    if bool(diag.get("static_decrypt_required")):
        return True
    return False


def _kirikiri_can_use_native_root_patch(engine, game_path: Path, items: list | None = None) -> bool:
    checker = getattr(engine, "_should_use_native_root_patch", None)
    if not callable(checker):
        return False
    changed: dict[str, list] = {}
    for item in items or []:
        file_name = str(getattr(item, "file", "") or "")
        if file_name:
            changed.setdefault(file_name.replace("\\", "/").lstrip("/"), []).append(item)
    try:
        return bool(checker(game_path, changed or None))
    except Exception:
        return False


def _kirikiri_items_have_protected_xp3_filter(items: list | None) -> bool:
    for item in items or []:
        meta = getattr(item, "meta", None) or {}
        if isinstance(meta.get("xp3_filter"), dict):
            return True
    return False


def _stop_processes_under_dir(game_dir: Path) -> None:
    try:
        game_root = str(game_dir.resolve())
    except Exception:
        game_root = str(game_dir)
    ps = (
        "$root = [System.IO.Path]::GetFullPath($args[0]);"
        "$procs = Get-Process | Where-Object { "
        "try { $_.Path -and $_.Path.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase) } catch { $false } };"
        "$procs | Stop-Process -Force -ErrorAction SilentlyContinue;"
        "Get-CimInstance Win32_Process | Where-Object { try { "
        "$_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase) "
        "} catch { $false } } "
        "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps, game_root],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        pass
    deadline = time.monotonic() + 8
    query = (
        "$root = [System.IO.Path]::GetFullPath($args[0]);"
        "@(Get-CimInstance Win32_Process | Where-Object { try { "
        "$_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase) "
        "} catch { $false } }).Count"
    )
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", query, game_root],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if (result.stdout or "").strip() in {"", "0"}:
                break
        except Exception:
            break
        time.sleep(0.5)


def _is_runtime_frida_mode(injector: str | None, engine, game_path: Path) -> bool:
    if injector != "frida":
        return False
    engine_name = getattr(engine, "name", "") if engine else ""
    if engine_name == "gamemaker":
        return True
    if engine_name == "kirikiri":
        return True
    if engine_name in {"godot", "godot_pck", "godot_frida"}:
        return True
    if engine_name != "rpgmaker":
        return False
    game_dir = game_path if game_path.is_dir() else game_path.parent
    return (
        bool(list(game_dir.glob("Data/*.rxdata")))
        and (game_dir / "Languages").is_dir()
        and (
            (game_dir / "steamshim.exe").exists()
            or (game_dir / "SDL2.dll").exists()
            or any(
                p.name.lower().startswith("x64-vcruntime") and "ruby" in p.name.lower()
                for p in game_dir.glob("*.dll")
            )
        )
    )


def _launch_finished_game(game_path: Path, engine, injector: str | None, checkpoint: Path | None) -> bool:
    engine_name = getattr(engine, "name", "") if engine else ""
    if engine_name in {"bgi", "kirikiri"} and not injector:
        game_dir = game_path if game_path.is_dir() else game_path.parent
        launcher = game_dir / "启动汉化版.bat"
        if launcher.exists():
            label = "BGI" if engine_name == "bgi" else "KiriKiri"
            info(f"通过 {label} 原生汉化启动器启动游戏")
            try:
                launch_translated_launcher(launcher, cwd=game_dir)
                return True
            except Exception as exc:
                warning(f"{label} 原生启动器启动失败，回退普通启动: {exc}")
    if injector:
        return launch_with_injector(game_path, injector, engine=engine, checkpoint=checkpoint)
    return launch_game(game_path, engine)


def _copy_back_safe(workspace: Workspace, game_path: Path, items: list | None = None, engine=None):
    """
    防线 4: 安全复制回游戏目录。只复制实际被修改的文件。
    对于 Ren'Py 游戏，.rpy 文件需放到 game/ 子目录。
    对于 Godot PCK 游戏，repack() 已直接将 PCK 写入游戏目录，跳过此步骤。
    """
    from core import pipeline as _pipeline_mod
    engine_name = getattr(engine, "name", "") if engine else ""
    if engine_name in ("godot", "godot_frida", "godot_pck", "xunity_realtime", "rpgmaker", "gamemaker"):
        return  # Runtime/deploy engines don't need file copy-back
    if engine_name == "tyrano" and bool(getattr(engine, "_packed_archive", False)):
        return  # Embedded executable was rebuilt directly by TyranoEngine.repack().

    game_dir = game_path if game_path.is_dir() else game_path.parent

    original_src = workspace.root / "original"
    if not original_src.exists():
        return

    target_dir = game_dir
    if engine and getattr(engine, "name", "") == "renpy":
        if (game_dir / "game").is_dir():
            target_dir = game_dir / "game"
            info(f"Ren'Py: 文件将回填到 {target_dir}")

    # 只复制被翻译修改过的文件
    files_to_copy: set[Path] = set()
    if items:
        for item in items:
            if engine_name == "kirikiri" and not _kirikiri_should_copy_loose_script(item):
                continue
            if _pipeline_mod._has_effective_translation(item):
                f = original_src / item.file
                if f.exists():
                    files_to_copy.add(f)
    else:
        # 没有 item 信息时，复制 original 目录下所有文件
        for f in original_src.rglob("*"):
            if f.is_file():
                files_to_copy.add(f)

    for f in files_to_copy:
        rel = f.relative_to(original_src)
        dest = target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            content = f.read_bytes()
            dest.write_bytes(content)
        except Exception:
            shutil.copy2(f, dest)

    info(f"文件已安全复制回: {target_dir}")

    # 自动修复中文字体（Ren'Py 游戏专用）
    if engine and getattr(engine, "name", "") == "renpy":
        _auto_fix_renpy_font(target_dir)

    info("如果游戏运行异常，可在 GUI 中使用“卸载汉化”按 manifest 恢复")


def _kirikiri_should_copy_loose_script(item) -> bool:
    meta = getattr(item, "meta", None) or {}
    if bool(meta.get("from_xp3")):
        return False
    if bool(meta.get("runtime_dump")) or bool(meta.get("runtime_capture")):
        return False
    if str(getattr(item, "file", "")).startswith("__kirikiri_"):
        return False
    return True


def _kirikiri_static_xp3_archives(items: list | None) -> set[str]:
    from core import pipeline as _pipeline_mod
    archives: set[str] = set()
    for item in items or []:
        if not _pipeline_mod._has_effective_translation(item):
            continue
        xp3_filter = (getattr(item, "meta", None) or {}).get("xp3_filter")
        if not isinstance(xp3_filter, dict):
            continue
        archive = str(xp3_filter.get("archive") or "").replace("\\", "/").lstrip("/")
        if not archive or archive.startswith("../") or "/../" in archive:
            continue
        archives.add(archive)
    return archives


def _replace_fonts_if_needed(engine, game_path: Path, workspace: Path):
    """Deploy engine-owned CJK font resources after repack."""
    engine_name = getattr(engine, "name", "")
    game_dir = game_path if game_path.is_dir() else game_path.parent
    if engine_name == "wolf" and hasattr(engine, "deploy_cjk_fonts"):
        engine.deploy_cjk_fonts(game_dir, workspace)
        return
    if engine_name not in ("godot", "godot_frida", "godot_pck"):
        return

    # 查找 .pck 文件
    for sub in ["contents", "game", "data", ""]:
        search_dir = game_dir / sub if sub else game_dir
        if not search_dir.is_dir():
            continue
        pck_files = list(search_dir.glob("*.pck"))
        if pck_files:
            pck_path = pck_files[0]
            # 字体替换已在 repack() 中完成，不重复操作
            break


def _auto_fix_renpy_font(game_dir: Path):
    """自动检测并修复 Ren'Py 游戏中文字体。"""
    gui_rpy = game_dir / "gui.rpy"
    if not gui_rpy.exists():
        return

    content = gui_rpy.read_text(encoding="utf-8", errors="replace")

    # 已知的中文字体名称
    import re
    cjk_fonts = {
        "SourceHanSansCN-Regular.otf", "SourceHanSansSC-Regular.otf",
        "SourceHanSansHWSC-Regular.otf", "NotoSansCJK.ttf",
        "NotoSansCJK.ttc", "NotoSansCJKsc-Regular.otf",
        "NotoSansSC-Regular.otf", "NotoSansSC-VF.ttf",
    }

    # Ren'Py 所有可能的中文字体变量（不同游戏命名习惯不同）
    _FONT_VARS = [
        "gui.default_font", "gui.interface_font", "gui.name_font",
        "gui.text_font", "gui.name_text_font", "gui.interface_text_font",
        "gui.button_text_font", "gui.choice_button_text_font",
    ]

    # 检测当前字体是否已支持中文
    for var in _FONT_VARS:
        fm = re.search(rf'define {re.escape(var)}\s*=\s*"([^"]+)"', content)
        if fm:
            font_name = Path(fm.group(1)).name
            if font_name in cjk_fonts:
                info(f"字体已支持中文: {font_name}")
                return

    info("检测到字体不支持中文，自动修复...")

    font_dir = game_dir / "gui" / "font"
    font_dir.mkdir(parents=True, exist_ok=True)
    chosen_font = "SourceHanSansCN-Regular.otf"
    dest = font_dir / chosen_font
    try:
        from core.open_source_fonts import deploy_source_han_sans
        if not dest.exists():
            if deploy_source_han_sans(dest):
                info(f"  已部署开源中文字体: {chosen_font}")
            else:
                chosen_font = None
    except Exception as exc:
        warning(f"部署开源中文字体失败: {exc}")
        chosen_font = None

    if not chosen_font:
        warning("未找到开源中文字体，中文可能无法正常显示")
        return

    # 替换所有字体变量配置
    font_rel_path = f"gui/font/{chosen_font}"
    replaced = 0
    for var in _FONT_VARS:
        m = re.search(rf'(define {re.escape(var)}\s*=\s*)"[^"]*"', content)
        if m:
            content = content.replace(m.group(0),
                                      f'define {var} = "{font_rel_path}"')
            replaced += 1

    if replaced:
        gui_rpy.write_text(content, encoding="utf-8")
        info(f"  字体已设置为: {chosen_font} (替换了 {replaced} 处字体变量)")
