"""detect 阶段补完：引擎选择、Luna 特征库 fallback、自动运行时探针、
提取质量检查与提取/无文本诊断记录。

模块级函数收 pipeline 作第一参数（Pipeline 类保留委托薄壳），
与 core/pipeline_stage_runtime.py 的拆分模式一致。
"""
from __future__ import annotations

import threading
from pathlib import Path

from core.engine_capabilities import engine_support_summary
from utils.logger import info, warning


def select_engine(pipeline, path: Path):
    from core import pipeline as _pipeline_mod
    candidates = _pipeline_mod.detect_engine_candidates(path)
    diag_candidates = []
    for engine, score, evidence in candidates:
        if engine is None:
            continue
        diag_candidates.append({
            "name": getattr(engine, "name", ""),
            "label": getattr(engine, "label", ""),
            "score": score,
            "evidence": evidence,
            "support": engine_support_summary(engine),
        })
    if pipeline.diagnostics:
        pipeline.diagnostics.set("engine_candidates", diag_candidates)
    if not candidates:
        # fallback: LunaTranslator 引擎特征库
        engine = pipeline._try_luna_hints(path)
        if engine:
            return engine
        return None
    engine = candidates[0][0]
    info(f"检测到引擎: {engine.label}")
    support = getattr(engine, "support_level", "stable")
    if support not in ("stable", "beta"):
        warning(f"该引擎支持状态: {support}，可能无法自动完成全部流程")
    return engine

def try_luna_hints(path: Path):
    """LunaTranslator 引擎特征库 fallback。"""
    try:
        from core.detector import detect_with_luna_hints
        name = detect_with_luna_hints(path)
        if name:
            from engines.base import EngineBase
            class _LunaEngine(EngineBase):
                name = "luna_hint"
                label = f"Luna特征匹配 ({name})"
                support_level = "experimental"
                supports_extract = False
                supports_repack = False
                detect_priority = 1
                limitations = [
                    f"通过 LunaTranslator {name} 特征匹配，未实现自动提取。",
                    "建议运行探针定位文本来源后手动配置引擎。",
                ]
                def detect(self, p): return True
                def unpack(self, p, ws): return []
                def repack(self, items, ws): pass
            info(f"Luna 特征匹配: {name}")
            return _LunaEngine()
    except ImportError:
        pass
    return None

def auto_probe(path: Path) -> dict | None:
    """引擎检测失败时自动运行探针，分析文本来源。

    启动游戏 → 注入 Frida → 捕获文本 30 秒 → 终止游戏 → 分析结果。
    返回 {"modules": [...], "hotspots": [...], "hints": [...]} 或 None。
    """
    import json, subprocess as _sp, tempfile, time as _t
    from pathlib import Path as _P

    # 找可执行文件
    p = path if path.is_dir() else path.parent
    exes = sorted(
        [f for f in p.glob("*.exe") if "unins" not in f.stem.lower() and "crash" not in f.stem.lower()],
        key=lambda x: x.stat().st_size, reverse=True,
    )
    if not exes:
        return None
    exe = exes[0]

    try:
        info(f"[自动探针] 启动: {exe.name}")
        proc = _sp.Popen([str(exe)], cwd=str(exe.parent))
        _t.sleep(2)

        # 注入 Frida
        import frida
        device = frida.get_local_device()
        session = device.attach(proc.pid)

        script_path = _P(__file__).parent.parent / "frida" / "runtime_text_probe_v2.js"
        script = session.create_script(script_path.read_text(encoding="utf-8"))

        events = []
        module_stats = {}
        ready = threading.Event()

        def on_msg(msg, _data):
            if msg["type"] != "send":
                return
            pl = msg.get("payload", {})
            if pl.get("type") == "text":
                events.append({"text": pl["text"], "caller": pl.get("caller", {}), "src": pl["src"]})
                c = pl.get("caller", {})
                if c.get("module"):
                    module_stats[c["module"]] = module_stats.get(c["module"], 0) + 1
            elif pl.get("type") == "ready":
                ready.set()

        script.on("message", on_msg)
        script.load()

        if not ready.wait(timeout=10):
            session.detach()
            proc.terminate()
            return None

        info(f"[自动探针] 扫描 30 秒...")
        _t.sleep(30)

        session.detach()
        try:
            proc.terminate()
        except Exception:
            pass

        if not events:
            return None

        # 分析结果
        hotspots = {}
        for e in events:
            c = e.get("caller", {})
            key = (c.get("module", ""), c.get("offset", "0x0"))
            if key[0]:
                hotspots[key] = hotspots.get(key, 0) + 1

        top = sorted(hotspots.items(), key=lambda x: x[1], reverse=True)[:10]
        top_modules = sorted(module_stats.items(), key=lambda x: x[1], reverse=True)[:5]

        info(f"[自动探针] {len(events)} 条文本, {len(module_stats)} 个模块")

        return {
            "modules": [{"module": m, "count": c} for m, c in top_modules],
            "hotspots": [{"module": m, "offset": o, "count": c} for (m, o), c in top],
            "exe": str(exe),
            "event_count": len(events),
        }

    except ImportError:
        info("[自动探针] Frida 未安装，跳过")
        return None
    except Exception as e:
        info(f"[自动探针] 失败: {e}")
        return None

def validate_extraction_quality(pipeline, items: list) -> None:
    """提取后质量检查：检测可疑模式并告警。"""
    if not items or len(items) < 100:
        return

    import re
    # 统计可疑模式
    code_like = 0
    path_like = 0
    single_char = 0
    all_upper = 0
    total = len(items)

    for it in items:
        s = it.original.strip()
        if len(s) == 1:
            single_char += 1
        elif re.match(r"^[A-Z][A-Z0-9_]{2,}$", s):
            all_upper += 1
        elif re.match(r"^[A-Za-z0-9_\-/\\\.@:]+$", s) and " " not in s:
            path_like += 1
        elif s.startswith(("var ", "let ", "const ", "function ", "if(", "for(", "while(")):
            code_like += 1

    suspicious = single_char + all_upper + path_like + code_like
    if suspicious > 0:
        pct = suspicious / total * 100
        detail_parts = []
        if single_char:
            detail_parts.append(f"单字符:{single_char}")
        if all_upper:
            detail_parts.append(f"全大写ID:{all_upper}")
        if path_like:
            detail_parts.append(f"路径:{path_like}")
        if code_like:
            detail_parts.append(f"代码:{code_like}")
        detail = " ".join(detail_parts)
        info(f"[提取质量] 可疑条目: {suspicious}/{total} ({pct:.1f}%) — {detail}")
        if pct > 10:
            warning(f"[提取质量] 可疑条目占比过高 ({pct:.1f}%)，建议检查提取结果")
        if pipeline.diagnostics:
            pipeline.diagnostics.set("extraction_quality", {
                "total": total,
                "suspicious": suspicious,
                "suspicious_pct": round(pct, 1),
                "detail": {"single_char": single_char, "all_upper": all_upper,
                            "path_like": path_like, "code_like": code_like},
            })

def record_unsupported_engine(pipeline, engine):
    limitations = getattr(engine, "limitations", [])
    msg = f"{getattr(engine, 'label', '当前引擎')} 暂不支持自动提取"
    if pipeline.diagnostics:
        pipeline.diagnostics.warn(
            msg,
            engine=getattr(engine, "name", ""),
            limitations=limitations,
        )
        for item in limitations:
            pipeline.diagnostics.suggest(item)
    else:
        warning(msg)

def fallback_generic(pipeline, path: Path):
    from engines import registry

    generic = registry.detect_generic()
    if not generic:
        return None
    info("尝试降级为通用明文扫描...")
    if pipeline.diagnostics:
        pipeline.diagnostics.step("fallback_generic", status="started")
    return generic

def record_extraction(pipeline, engine, items: list):
    per_file: dict[str, int] = {}
    samples = []
    for item in items[:20]:
        samples.append({
            "file": item.file,
            "key": item.key,
            "text": item.original[:120],
        })
    for item in items:
        per_file[item.file] = per_file.get(item.file, 0) + 1
    top_files = sorted(per_file.items(), key=lambda kv: kv[1], reverse=True)[:20]
    info(f"提取统计: {len(items)} 条，{len(per_file)} 个文件")
    pipeline._meta("text_count", len(items))
    pipeline._meta("file_count", len(per_file))
    if pipeline.diagnostics:
        pipeline.diagnostics.set("extraction", {
            "engine": getattr(engine, "name", ""),
            "total_items": len(items),
            "files": len(per_file),
            "top_files": [{"file": f, "count": c} for f, c in top_files],
            "samples": samples,
        })

def record_no_items(pipeline, engine, path: Path, injector: str | None):
    engine_name = getattr(engine, "name", "")
    suggestions = [
        "先使用“仅提取”检查是否能生成 translation_checkpoint.json。",
        "确认游戏资源不是加密封包，必要时先用专用工具解包。",
        "查看 workspaces/latest_diagnostics.json 中的 engine_candidates 和 preflight。",
    ]
    if engine_name in ("unity", "xunity_realtime") or injector == "xunity":
        suggestions.append("Unity 游戏可尝试 XUnity 运行时注入；部分动态文本只有运行时才能捕获。")
    if engine_name in ("renpy",):
        suggestions.append("Ren'Py 游戏建议安装 unrpa 和 unrpyc，提高 .rpa/.rpyc 提取成功率。")
    if engine_name in ("godot_frida", "godot_pck", "godot"):
        suggestions.append("Godot 加密 PCK 需要 Frida/gdpack/gdsdecomp 可用，普通 PCK 需能解析 GDPC。")
    if pipeline.diagnostics:
        pipeline.diagnostics.warn("未提取到可翻译文本", engine=engine_name, path=path)
        for s in suggestions:
            pipeline.diagnostics.suggest(s)
