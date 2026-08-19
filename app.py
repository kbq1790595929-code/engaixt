import sys, json, threading, subprocess, os, webbrowser

from pathlib import Path



sys.path.insert(0, str(Path(__file__).parent))



from config import get_config, save_config
from core.clipboard_service import get_clipboard_text, set_clipboard_text
from core.resources import resource_path, app_root
from core.task_manager import TaskManager


_TRANSLATED_GAMES_CACHE = {"at": 0.0, "items": [], "manifest_dir": ""}
_COVER_CACHE: dict[str, object] = {}
_LIBRARY_PRUNE_STATE = {"running": False, "last": 0.0}
_LIBRARY_PRUNE_LOCK = threading.Lock()
_REALTIME_ONLY_ENGINE_NAMES = {"kirikiri", "unity", "xunity_realtime", "unity_arch000_lua"}
_UNITY_REALTIME_ENGINE_NAMES = {"unity", "xunity_realtime", "unity_arch000_lua"}
_SUPPORT_LINKS = {
    "official": "https://engaixt.com/",
    "feedback": "mailto:contact@example.com?subject=EngAixt%20%E9%97%AE%E9%A2%98%E5%8F%8D%E9%A6%88",
    "upgrade": "https://ifdian.net/a/engaixt",
    "renew": "https://ifdian.net/a/engaixt",
}


def _detect_engine_policy(path: str | Path) -> dict:
    """Return the detected engine info used by GUI action routing."""
    try:
        from core.detector import _ensure_engines_loaded, detect_engine
        from core.path_resolver import resolve_game_path

        _ensure_engines_loaded()
        resolved = Path(resolve_game_path(str(path)))
        engine = detect_engine(resolved)
        return {
            "name": str(getattr(engine, "name", "") or ""),
            "label": str(getattr(engine, "label", "") or getattr(engine, "name", "") or ""),
            "engine": engine,
            "path": resolved,
        }
    except Exception as exc:
        return {"name": "error", "label": "检测失败", "engine": None, "path": Path(str(path)), "error": str(exc)}


def _is_realtime_only_engine_name(name: str) -> bool:
    return str(name or "").lower() in _REALTIME_ONLY_ENGINE_NAMES


def _is_unity_realtime_engine_name(name: str) -> bool:
    return str(name or "").lower() in _UNITY_REALTIME_ENGINE_NAMES


def _invalidate_translated_games_cache():
    _TRANSLATED_GAMES_CACHE["at"] = 0.0
    _TRANSLATED_GAMES_CACHE["items"] = []
    _TRANSLATED_GAMES_CACHE["manifest_dir"] = ""


def _schedule_library_prune():
    """Run expensive deleted-game cleanup away from the GUI bridge call."""
    import time

    now = time.monotonic()
    with _LIBRARY_PRUNE_LOCK:
        if _LIBRARY_PRUNE_STATE.get("running"):
            return
        if now - float(_LIBRARY_PRUNE_STATE.get("last") or 0.0) < 30:
            return
        _LIBRARY_PRUNE_STATE["running"] = True
        _LIBRARY_PRUNE_STATE["last"] = now

    def _run():
        try:
            from core.manifest import prune_missing_games
            prune_missing_games(remove_workspaces=True)
            _invalidate_translated_games_cache()
        finally:
            with _LIBRARY_PRUNE_LOCK:
                _LIBRARY_PRUNE_STATE["running"] = False

    threading.Thread(target=_run, daemon=True).start()


def _find_translated_launcher(game_dir: Path, manifest_data: dict | None = None) -> Path | None:
    """Return the translated launcher for a game when the pipeline created one."""
    direct = game_dir / "启动汉化版.bat"
    if direct.is_file():
        return direct

    data = manifest_data or {}
    candidates: list[Path] = []
    for item in data.get("created_files", []) if isinstance(data, dict) else []:
        if not item.get("runtime_required") and item.get("kind") != "runtime":
            continue
        path_text = str(item.get("path") or "").strip()
        rel_text = str(item.get("rel") or "").strip()
        for text in (path_text, rel_text):
            if not text:
                continue
            p = Path(text)
            if not p.is_absolute():
                p = game_dir / p
            name = p.name.casefold()
            if (
                p.suffix.lower() in {".bat", ".cmd", ".lnk", ".exe"}
                and ("启动汉化版" in name or "汉化" in name)
            ):
                candidates.append(p)
                continue
            if (
                p.suffix.lower() in {".bat", ".cmd", ".lnk", ".exe"}
                and ("启动汉化版" in name or "汉化" in name or "translated" in name)
            ):
                candidates.append(p)

    def score(path: Path) -> tuple[int, str]:
        name = path.name.casefold()
        if name == "启动汉化版.bat":
            return (0, name)
        if "launcher" in name or "汉化" in name:
            return (1, name)
        return (2, name)

    for path in sorted(candidates, key=score):
        try:
            resolved = path.resolve()
            resolved.relative_to(game_dir.resolve())
            if path.is_file():
                return path
        except (OSError, ValueError):
            continue
    return None


def _manifest_for_game_dir(game_dir: Path, manifest_dir: Path) -> dict:
    if not manifest_dir.exists():
        return {}
    expected = str(game_dir.resolve()).casefold()
    for manifest_file in manifest_dir.glob("*.json"):
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            recorded = str(data.get("game_dir") or "").strip()
            if recorded and str(Path(recorded).resolve()).casefold() == expected:
                return data
        except Exception:
            continue
    return {}


def _tool_progress_percent(event: dict, start: float, end: float) -> float:
    total = max(1, int(event.get("total") or 1))
    index = max(0, int(event.get("index") or 0))
    phase_weight = {
        "update_begin": 0.0,
        "tool_check": 0.1,
        "resolve_release": 0.22,
        "download_start": 0.38,
        "download_done": 0.55,
        "extract_start": 0.68,
        "replace_start": 0.82,
        "tool_done": 0.95,
        "update_done": 1.0,
        "ensure_start": 0.2,
        "ensure_done": 0.95,
        "ensure_failed": 0.95,
        "update_skipped": 1.0,
    }.get(str(event.get("phase") or ""), 0.5)
    raw = (max(index - 1, 0) + phase_weight) / total
    raw = max(0.0, min(1.0, raw))
    return round(start + (end - start) * raw, 1)


def _app_update_progress_percent(event: dict) -> float:
    phase = str(event.get("phase") or "")
    if phase == "package_manifest":
        return 8.0
    if phase == "download":
        size = max(1, int(event.get("size") or 1))
        done = max(0, int(event.get("downloaded") or 0))
        return round(20 + min(1.0, done / size) * 60, 1)
    if phase == "download_done":
        return 85.0
    return 10.0


def _app_update_progress_message(event: dict) -> str:
    phase = str(event.get("phase") or "")
    if phase == "package_manifest":
        return "读取更新包清单"
    if phase == "download":
        index = int(event.get("index") or 0)
        total = int(event.get("total") or 0)
        if index and total:
            return f"下载更新包 {index}/{total}"
        return "下载更新包"
    if phase == "download_done":
        return "更新包下载完成"
    return str(event.get("message") or "下载更新包")


def _app_update_progress_detail(event: dict) -> dict:
    detail = {}
    for key in ("phase", "downloaded", "size", "index", "total"):
        value = event.get(key)
        if value is not None:
            detail[key] = value
    return detail



class Api:

    """docstring"""



    def __init__(self):

        self._window = None
        # 统一任务管理器：所有长任务线程都经它派发，共享状态/取消/计数/结果台账。
        # 计数归零回调保持旧 _end_active_task 的行为：推送 refreshLicenseStatus()。
        self._tasks = TaskManager(on_active_drained=self._on_active_drained)
        self._background_tool_update_started = False
        self._background_tool_update_lock = threading.Lock()



    def set_window(self, window):

        self._window = window



    def _js(self, code: str):

        if self._window:

            self._window.evaluate_js(code)

    def _on_active_drained(self):
        # 活动任务计数归零时刷新授权状态（与旧 _end_active_task 归零分支逐字一致）。
        self._js("if (typeof refreshLicenseStatus === 'function') refreshLicenseStatus();")

    # 兼容垫片：begin/end 供尚未收编成 TaskManager.submit 的调用方或外部隐性依赖使用。
    def _begin_active_task(self):
        self._tasks.begin_external()

    def _end_active_task(self):
        self._tasks.end_external()

    def _is_active_task_running(self) -> bool:
        return self._tasks.active_count() > 0

    # ── 任务台账 ──

    def list_tasks(self):
        """列出本次会话所有长任务（供 GUI 查询状态/错误/耗时）。"""
        return self._tasks.list()

    def cancel_task(self, task_id: str):
        """协作式取消：设置取消事件，任务自行检查后退出。"""
        return {"ok": self._tasks.cancel(task_id)}



    # ── 配置 ──



    def get_config(self):

        c = get_config()

        return {

            "openai_api_key": c.openai_api_key,

            "openai_model": c.openai_model,

            "deepseek_api_key": c.deepseek_api_key,

            "deepseek_model": c.deepseek_model,

            "qwen_api_key": c.qwen_api_key,

            "qwen_model": c.qwen_model,

            "zhipu_api_key": c.zhipu_api_key,

            "zhipu_model": c.zhipu_model,

            "moonshot_api_key": c.moonshot_api_key,

            "moonshot_model": c.moonshot_model,

            "doubao_api_key": c.doubao_api_key,

            "doubao_model": c.doubao_model,

            "anthropic_api_key": c.anthropic_api_key,

            "anthropic_model": c.anthropic_model,

            "hy_mt2_model": c.hy_mt2_model,

            "hy_mt2_context_size": c.hy_mt2_context_size,

            "hy_mt2_batch_size": c.hy_mt2_batch_size,

            "hy_mt2_idle_timeout_seconds": c.hy_mt2_idle_timeout_seconds,

            "active_translator": c.active_translator,

            "ui_language": c.ui_language,

            "target_lang": c.target_lang,

            "max_batch_size": c.max_batch_size,

            "max_concurrency": c.max_concurrency,

            "translation_coverage": c.translation_coverage,
            "minimum_translation_coverage": c.minimum_translation_coverage,
            "translation_cache_enabled": c.translation_cache_enabled,
            "translation_cache_auto_cleanup": c.translation_cache_auto_cleanup,
            "translation_cache_max_size_gb": c.translation_cache_max_size_gb,

            "keep_workspace": c.keep_workspace,

            "frida_timeout": c.frida_timeout,

            "tools_dir": c.tools_dir,

            "auto_download_tools": c.auto_download_tools,
            "auto_update_tools": c.auto_update_tools,
            "tool_update_interval_hours": c.tool_update_interval_hours,

            "cjk_font_path": c.cjk_font_path,

            "auto_launch": c.auto_launch,
            "ui_accent": c.ui_accent,
            "bg_color": c.bg_color,
            "bg_image": c.bg_image,
            "kirikiri_runtime_completion_mode": c.kirikiri_runtime_completion_mode,
            "kirikiri_runtime_merge_captures": c.kirikiri_runtime_merge_captures,
            "kirikiri_no_window_timeout_seconds": c.kirikiri_no_window_timeout_seconds,
            "kirikiri_overlay_font_family": c.kirikiri_overlay_font_family,
            "kirikiri_overlay_font_size": c.kirikiri_overlay_font_size,
            "kirikiri_overlay_original_font_size": c.kirikiri_overlay_original_font_size,
            "kirikiri_overlay_text_color": c.kirikiri_overlay_text_color,
            "kirikiri_overlay_speaker_color": c.kirikiri_overlay_speaker_color,
            "kirikiri_overlay_original_color": c.kirikiri_overlay_original_color,
            "kirikiri_overlay_bg_color": c.kirikiri_overlay_bg_color,
            "kirikiri_overlay_pending_color": c.kirikiri_overlay_pending_color,
            "kirikiri_overlay_waiting_color": c.kirikiri_overlay_waiting_color,
            "kirikiri_overlay_opacity": c.kirikiri_overlay_opacity,
            "kirikiri_overlay_height": c.kirikiri_overlay_height,
            "kirikiri_overlay_max_width": c.kirikiri_overlay_max_width,
            "kirikiri_overlay_text_only": c.kirikiri_overlay_text_only,
            "kirikiri_overlay_show_speaker": c.kirikiri_overlay_show_speaker,
            "kirikiri_overlay_show_original": c.kirikiri_overlay_show_original,
            "kirikiri_overlay_live_translate": c.kirikiri_overlay_live_translate,
            "kirikiri_overlay_follow_window": c.kirikiri_overlay_follow_window,
            "kirikiri_overlay_locked": c.kirikiri_overlay_locked,
            "kirikiri_overlay_remember_position": c.kirikiri_overlay_remember_position,

            "engine_whitelist": c.engine_whitelist,

        }



    def get_license_status(self):

        try:
            from core.trial_quota import trial_status

            return trial_status().to_dict()
        except Exception as exc:
            return {
                "edition": "unknown",
                "error": str(exc),
            }

    def activate_monthly_member(self, code: str):
        try:
            from core.monthly_license import MonthlyLicenseError, decode_monthly_license_code
            from core.trial_quota import activate_monthly_license

            license_info = decode_monthly_license_code(str(code or ""))
            status = activate_monthly_license(license_info.raw_code)
            if status.edition == "unlimited":
                message = f"会员激活成功，有效期至 {status.license_expires_at}"
            else:
                message = f"会员码已保存，将于 {license_info.valid_from.isoformat()} 自动生效"
            return {
                "ok": True,
                "message": message,
                "status": status.to_dict(),
            }
        except MonthlyLicenseError as exc:
            return {
                "ok": False,
                "message": str(exc),
            }
        except Exception as exc:
            return {
                "ok": False,
                "message": f"会员激活失败: {exc}",
            }

    def get_app_update_info(self):
        try:
            from core.app_update import check_update

            return check_update(timeout=12)
        except Exception as exc:
            try:
                from core.app_update import current_app_info

                info = current_app_info()
            except Exception:
                info = {
                    "app": "EngAixt",
                    "version": "unknown",
                    "edition": "unknown",
                    "can_apply_update": False,
                }
            info.update({
                "update_available": False,
                "latest_version": info.get("version", "unknown"),
                "error": str(exc),
            })
            return info

    def open_support_link(self, key: str):
        key = str(key or "").strip().lower()
        url = _SUPPORT_LINKS.get(key)
        if not url:
            raise ValueError("unknown support link")
        webbrowser.open(url, new=2)
        return {"ok": True, "url": url}



    def save_config(self, data: dict):

        c = get_config()

        for k, v in data.items():

            if hasattr(c, k):
                if k.endswith("_api_key") and isinstance(v, str) and not v.strip():
                    continue

                current = getattr(c, k)

                if isinstance(current, bool):

                    setattr(c, k, bool(v))

                elif isinstance(current, int):

                    setattr(c, k, int(str(v)))

                elif isinstance(current, float):

                    setattr(c, k, float(str(v)))

                elif isinstance(current, list):

                    if isinstance(v, list):
                        setattr(c, k, v)
                    else:
                        setattr(c, k, [part.strip() for part in str(v).split(",") if part.strip()])

                else:

                    setattr(c, k, v)

        save_config()

        return True

    def get_clipboard_text(self):
        return get_clipboard_text()

    def set_clipboard_text(self, text: str):
        return set_clipboard_text(text)



    # ── 文件选择 ──



    def select_directory(self):

        import tkinter as tk

        from tkinter import filedialog

        root = tk.Tk()

        root.withdraw()

        path = filedialog.askdirectory(title="选择游戏目录")

        root.destroy()

        return path.replace("/", "\\") if path else ""



    def select_file(self):

        import tkinter as tk

        from tkinter import filedialog

        root = tk.Tk()

        root.withdraw()

        path = filedialog.askopenfilename(

            title="选择游戏文件",

            filetypes=[
                ("游戏入口", "*.exe;*.url;*.lnk;appmanifest_*.acf"),
                ("压缩包", "*.zip;*.rar;*.7z"),
                ("所有文件", "*.*"),
            ]

        )

        root.destroy()

        return path.replace("/", "\\") if path else ""



    def resolve_path(self, path: str):
        """解析快捷方式/Steam 入口；手动选择的 .exe 保留为启动入口。"""
        from core.path_resolver import resolve_game_path

        return resolve_game_path(path)

    def select_json_file(self):

        import tkinter as tk

        from tkinter import filedialog

        root = tk.Tk()

        root.withdraw()

        path = filedialog.askopenfilename(

            title="选择翻译检查点 JSON",

            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")]

        )

        root.destroy()

        return path.replace("/", "\\") if path else ""



    # ── 翻译管线 ──



    def run_translation(self, path: str, injector: str = "", coverage: int = 100):

        """docstring"""

        policy = _detect_engine_policy(path)
        if _is_realtime_only_engine_name(policy.get("name", "")):
            label = policy.get("label") or policy.get("name") or "当前引擎"
            msg = f"{label} 只能使用“实时翻译”，已阻止进入“开始翻译”流程。"
            self._js("on_status('空闲')")
            self._js("on_log(30, " + json.dumps(msg, ensure_ascii=False) + ")")
            return False

        self._do_run(path, injector=injector, coverage=coverage)

        return True

    def run_realtime(self, path: str, src_lang: str = "ja", tgt_lang: str = "zh-CN",
                     preload: str = ""):
        """实时翻译模式（LunaTranslator 风格）— 后台线程启动。"""
        import os, json, tempfile
        from pathlib import Path as _Path

        p = self.resolve_path(path)
        policy = _detect_engine_policy(p)
        engine_name = str(policy.get("name") or "").lower()
        engine = policy.get("engine")
        policy_path = _Path(policy.get("path") or p)

        if engine_name == "kirikiri":
            def _run_kirikiri(task):
                try:
                    from core.launcher import create_kirikiri_native_launcher, launch_kirikiri_native_runtime

                    self._js("on_status('实时翻译中')")
                    self._js("on_log(20, " + json.dumps("KRKR 使用原生 hook + 第二窗口实时翻译") + ")")
                    launcher = create_kirikiri_native_launcher(policy_path, engine=engine)
                    if launcher:
                        self._js("on_log(20, " + json.dumps("已生成 KRKR 便携启动器: " + str(launcher), ensure_ascii=False) + ")")
                    ok = launch_kirikiri_native_runtime(policy_path, engine=engine, wait=True)
                    if ok:
                        self._js("on_log(20, " + json.dumps("KRKR 实时翻译已结束", ensure_ascii=False) + ")")
                        self._js("on_status('空闲')")
                    else:
                        self._js("on_log(50, " + json.dumps("KRKR 实时翻译启动失败，请查看 _translation_meta 日志", ensure_ascii=False) + ")")
                        self._js("on_status('失败')")
                except Exception as e:
                    self._js("on_log(50, " + json.dumps("KRKR 实时翻译错误: " + str(e), ensure_ascii=False) + ")")
                    self._js("on_status('错误')")

            self._tasks.submit("实时翻译(kirikiri)", _run_kirikiri, kind="realtime", counted=True)
            return True

        if engine_name in {"godot", "godot_pck", "godot_frida"}:
            def _run_godot(task):
                try:
                    from core.launcher import create_godot_display_hook_launcher, launch_with_injector

                    self._js("on_status('运行中')")
                    self._js("on_log(20, " + json.dumps("Godot 使用离线译文 + 显示层 hook，不调用实时翻译 API", ensure_ascii=False) + ")")
                    launcher = create_godot_display_hook_launcher(policy_path, engine=engine)
                    if launcher:
                        self._js("on_log(20, " + json.dumps("已生成 Godot 汉化启动器: " + str(launcher), ensure_ascii=False) + ")")
                    ok = launch_with_injector(policy_path, "frida", engine=engine)
                    if ok:
                        self._js("on_log(20, " + json.dumps("Godot 显示层 hook 已结束", ensure_ascii=False) + ")")
                        self._js("on_status('空闲')")
                    else:
                        self._js("on_log(50, " + json.dumps("Godot 显示层 hook 启动失败，请查看 _translation_meta 日志", ensure_ascii=False) + ")")
                        self._js("on_status('失败')")
                except Exception as e:
                    self._js("on_log(50, " + json.dumps("Godot 显示层 hook 错误: " + str(e), ensure_ascii=False) + ")")
                    self._js("on_status('错误')")

            self._tasks.submit("实时翻译(godot)", _run_godot, kind="realtime", counted=True)
            return True

        if _is_unity_realtime_engine_name(engine_name):
            def _run_unity(task):
                try:
                    from engines.xunity import XUnityRealtimeEngine
                    from core.launcher import launch_with_injector

                    game_dir = policy_path if policy_path.is_dir() else policy_path.parent
                    meta_dir = game_dir / "_translation_meta"
                    meta_dir.mkdir(parents=True, exist_ok=True)
                    runtime_engine = XUnityRealtimeEngine()
                    runtime_engine._game_dir = game_dir
                    if src_lang and src_lang != "auto":
                        runtime_engine._source_lang = src_lang
                    self._js("on_status('实时翻译中')")
                    self._js("on_log(20, " + json.dumps("Unity 使用 XUnity 运行时注入实时翻译") + ")")
                    runtime_engine.repack([], meta_dir)
                    ok = launch_with_injector(policy_path, "xunity", engine=runtime_engine)
                    if ok:
                        self._js("on_log(20, " + json.dumps("Unity 实时翻译已结束", ensure_ascii=False) + ")")
                        self._js("on_status('空闲')")
                    else:
                        self._js("on_log(50, " + json.dumps("Unity 实时翻译启动失败，请查看运行日志", ensure_ascii=False) + ")")
                        self._js("on_status('失败')")
                except Exception as e:
                    self._js("on_log(50, " + json.dumps("Unity 实时翻译错误: " + str(e), ensure_ascii=False) + ")")
                    self._js("on_status('错误')")

            self._tasks.submit("实时翻译(unity)", _run_unity, kind="realtime", counted=True)
            return True

        exe = _Path(p) if _Path(p).suffix.lower() == ".exe" else None
        if not exe:
            self._js(f"on_log(50, {json.dumps('实时翻译需要选择游戏 .exe 文件')})")
            self._js("on_status('错误')")
            return False

        preload_map = {}
        if preload:
            try:
                preload_map = json.loads(_Path(preload).read_text(encoding="utf-8"))
            except Exception:
                pass

        def _run(task):
            from frida import get_local_device
            import subprocess as _sp
            import threading as _th
            import time as _t
            import hashlib
            import asyncio
            from pathlib import Path as _P
            from engines.base import TextItem
            from core.launcher import _overlay_window_command
            from core.translation_overlay_window import OverlayShmWriter

            overlay_proc = None
            shm_writer = None
            try:
                self._js("on_status('实时翻译中')")
                self._js("on_log(20, " + json.dumps("启动实时翻译: " + str(exe)) + ")")

                # 启动游戏
                proc = _sp.Popen([str(exe)], cwd=str(exe.parent))
                _th.Thread(target=proc.wait, daemon=True).start()
                _t.sleep(1.5)

                # 启动 overlay 子进程
                shm_name = f"gametrans_rt_{proc.pid}"
                root_path = app_root()
                overlay_cmd, overlay_cwd, overlay_env = _overlay_window_command(
                    exe.parent.name,
                    exe.name,
                    str(exe),
                    shm_name,
                )
                creationflags = 0
                if sys.platform == "win32":
                    creationflags = getattr(_sp, "CREATE_NO_WINDOW", 0)

                # 创建日志文件路径
                log_dir = app_root() / "logs"
                log_dir.mkdir(exist_ok=True)
                overlay_log = log_dir / f"overlay_{proc.pid}.log"

                try:
                    with open(overlay_log, "w", encoding="utf-8") as log_f:
                        overlay_proc = _sp.Popen(
                            overlay_cmd, cwd=overlay_cwd, env=overlay_env,
                            creationflags=creationflags,
                            stdout=log_f, stderr=_sp.STDOUT
                        )
                    self._js("on_log(20, " + json.dumps(f"字幕窗口已启动 (日志: {overlay_log.name})") + ")")
                except Exception as e:
                    self._js("on_log(40, " + json.dumps(f"字幕窗口启动失败: {e}") + ")")

                # 创建共享内存写入器
                shm_writer = OverlayShmWriter(shm_name)

                # 注入 Frida
                device = get_local_device()
                session = device.attach(proc.pid)

                script_src = resource_path("frida", "realtime_hook.js").read_text(encoding="utf-8")
                script_src = f"var GM_SRC_LANG = '{src_lang}';\n" + script_src
                script = session.create_script(script_src)

                from core.realtime_translator import RealtimeTranslator
                from core.pipeline import _get_translator

                cfg = get_config()
                translator = _get_translator(cfg.active_translator)
                if not translator:
                    raise RuntimeError("未配置翻译器")

                def translate_fn(text: str) -> str:
                    async def run_once() -> str:
                        item = TextItem(
                            file="__realtime__.jsonl",
                            key=hashlib.sha1(text.encode("utf-8")).hexdigest()[:16],
                            original=text,
                            translated="",
                            context="realtime",
                            meta={"runtime_capture": True},
                        )
                        result = await translator.translate_batch([item], src_lang, tgt_lang)
                        if result and result[0].translated:
                            return str(result[0].translated).strip()
                        return ""
                    return asyncio.run(run_once())

                pipeline = RealtimeTranslator(translate_fn, source_lang=src_lang, target_lang=tgt_lang)

                def push_result(original: str, translated: str):
                    try:
                        shm_writer.write(original, translated)
                    except Exception:
                        pass

                def on_message(msg, _data):
                    if msg["type"] != "send":
                        return
                    pl = msg.get("payload", {})
                    if pl.get("type") == "new_text":
                        text = pl.get("text", "")
                        if text:
                            priority = 1 if len(text) < 20 else 0
                            pipeline.submit(text, on_result=push_result, priority=priority)
                    elif pl.get("type") == "log":
                        self._js("on_log(20, " + json.dumps("[实时] " + pl.get("msg", "")) + ")")

                script.on("message", on_message)
                script.load()
                self._js(f"on_log(20, {json.dumps('实时翻译已启动（字幕窗口模式），Ctrl+C 停止')})")

                try:
                    while True:
                        if task.cancel_event.is_set():
                            break
                        if proc.poll() is not None:
                            break
                        if overlay_proc and overlay_proc.poll() is not None:
                            break
                        _t.sleep(0.5)
                except KeyboardInterrupt:
                    pass
                finally:
                    session.detach()
                    if shm_writer:
                        shm_writer.close()
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    if overlay_proc and overlay_proc.poll() is None:
                        try:
                            overlay_proc.terminate()
                        except Exception:
                            pass
                    self._js("on_status('空闲')")

            except Exception as e:
                self._js(f"on_status('错误')")
                self._js("on_log(50, " + json.dumps("实时翻译错误: " + str(e)) + ")")
                if shm_writer:
                    try:
                        shm_writer.close()
                    except Exception:
                        pass
                if overlay_proc and overlay_proc.poll() is None:
                    try:
                        overlay_proc.terminate()
                    except Exception:
                        pass

        self._tasks.submit("实时翻译", _run, kind="realtime", counted=True)
        return True
    def extract_only(self, path: str):

        """docstring"""

        self._do_run(path, extract_only=True)

        return True



    def patch_from_json(self, path: str, json_path: str = ""):

        """docstring"""

        self._do_run(path, resume_json=json_path, patch_only=True)

        return True

    def polish_translation(self, path: str, budget_cny: float = 1.0):

        """Polish the current game's existing translation checkpoint."""

        self._do_polish(path, budget_cny=budget_cny)

        return True



    def clear_cache(self):

        """docstring"""

        from core.manifest import cleanup_temporary_files

        def _run(task):

            self._js("on_status('清理中')")

            self._js("on_log(20, '清理临时文件...')")

            try:

                cleanup_temporary_files()

                self._js("on_log(20, '清理完成')")

                self._js("on_status('空闲')")

            except Exception as e:

                self._js(f"on_log(50, '清理失败: {e}')")

                self._js("on_status('错误')")

        self._tasks.submit("清理缓存", _run, kind="cache", counted=False)

        return True

    # ── 已翻译的游戏列表 ──



    def list_translated_games(self):

        """Return translated games whose original game directories still exist."""

        import json
        import time

        from core.manifest import MANIFEST_DIR

        now = time.monotonic()
        manifest_dir_key = str(MANIFEST_DIR.resolve()).casefold()
        if (
            float(_TRANSLATED_GAMES_CACHE["at"]) > 0
            and _TRANSLATED_GAMES_CACHE.get("manifest_dir") == manifest_dir_key
            and now - float(_TRANSLATED_GAMES_CACHE["at"]) < 10
        ):
            return list(_TRANSLATED_GAMES_CACHE["items"])

        games = []
        missing_seen = False

        if MANIFEST_DIR.exists():

            def _mtime(path: Path) -> float:
                try:
                    return path.stat().st_mtime
                except OSError:
                    return 0.0

            for manifest_file in sorted(

                MANIFEST_DIR.iterdir(),

                key=_mtime,

                reverse=True,

            ):

                if manifest_file.suffix != ".json":

                    continue

                try:

                    data = json.loads(manifest_file.read_text(encoding="utf-8"))

                    game_dir = str(data.get("game_dir") or "").strip()

                    if not game_dir:
                        continue

                    game_path = Path(game_dir)

                    if not game_path.is_dir():
                        missing_seen = True
                        continue

                    games.append({

                        "game_id": data.get("game_id", manifest_file.stem),

                        "game_dir": str(game_path),

                        "engine": data.get("engine", "未知"),

                        "created_at": data.get("created_at", ""),

                        "updated_at": data.get("updated_at", ""),

                    })

                except Exception:

                    pass

        _TRANSLATED_GAMES_CACHE["at"] = now
        _TRANSLATED_GAMES_CACHE["items"] = list(games)
        _TRANSLATED_GAMES_CACHE["manifest_dir"] = manifest_dir_key
        if missing_seen:
            _schedule_library_prune()
        return games



    def open_game_dir(self, path: str):
        """启动游戏主程序；找不到则打开目录。后台线程避免阻塞 UI。"""
        import json, os
        from pathlib import Path
        from core.exe_selector import find_main_exe
        from core.launcher import launch_translated_launcher
        from core.path_resolver import resolve_game_path
        from core.manifest import MANIFEST_DIR

        def _launch(task):
            p = Path(resolve_game_path(path))
            if not p.exists():
                return
            game_dir = p if p.is_dir() else p.parent
            manifest_data = _manifest_for_game_dir(game_dir, MANIFEST_DIR)
            launcher = _find_translated_launcher(game_dir, manifest_data)
            if launcher:
                try:
                    launch_translated_launcher(launcher, cwd=game_dir)
                    engine_name = str(manifest_data.get("engine") or "").strip()
                    label = f"{engine_name} 汉化启动器" if engine_name else "汉化启动器"
                    self._js(f"on_log(20, {json.dumps('已通过' + label + '启动', ensure_ascii=False)})")
                    return
                except Exception as e:
                    self._js("on_log(50, " + json.dumps(
                        "汉化启动器启动失败，回退原始启动: " + str(e),
                        ensure_ascii=False,
                    ) + ")")
            if p.is_file():
                os.startfile(str(p))
                return
            exe = find_main_exe(p, recursive=False) or find_main_exe(p, recursive=True)
            if exe:
                os.startfile(str(exe))
            else:
                os.startfile(str(p))

        self._tasks.submit("启动游戏", _launch, kind="misc", counted=False)

    def reveal_game_dir(self, path: str):
        """Open the game folder without launching the game."""
        import os
        from pathlib import Path

        p = Path(path)
        if not p.exists():
            return False
        target = p if p.is_dir() else p.parent
        try:
            os.startfile(str(target))
            return True
        except Exception as e:
            self._js(f"on_log(50, {json.dumps('无法打开目录: ' + str(e))})")
            return False

    def remove_game_record(self, game_id: str):
        """Remove a game from the translated-games library only."""
        from core.manifest import MANIFEST_DIR, MANIFEST_NAME

        if not game_id:
            return False
        removed = False
        manifest_path = MANIFEST_DIR / f"{game_id}.json"
        game_dir = ""
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                game_dir = data.get("game_dir", "")
            except Exception:
                game_dir = ""
            try:
                manifest_path.unlink()
                removed = True
            except Exception as e:
                self._js(f"on_log(50, {json.dumps('移除记录失败: ' + str(e))})")
                return False

        if game_dir:
            try:
                local = Path(game_dir) / MANIFEST_NAME
                if local.exists():
                    local.unlink()
                    removed = True
            except Exception:
                pass
        if removed:
            _invalidate_translated_games_cache()
            self._js("on_log(20, '已从游戏库移除记录')")
        return removed



    # ── 引擎检测 ──



    def get_engine_info(self, path: str):

        """docstring"""

        try:

            from core.detector import _ensure_engines_loaded, detect_engine_candidates
            from core.engine_capabilities import engine_support_summary
            from core.path_resolver import resolve_game_path

            _ensure_engines_loaded()
            path = resolve_game_path(path)

            candidates = detect_engine_candidates(Path(path))

            best = candidates[0][0] if candidates else None

            if not candidates and not best:

                return {"label": "未识别", "name": "unknown",

                        "support_level": "unknown", "limitations": [], "candidates": []}

            best_support = engine_support_summary(best)
            return {

                "label": best.label if best else "通用引擎",

                "name": best.name if best else "generic",

                "support_level": getattr(best, "support_level", "unknown"),

                "limitations": getattr(best, "limitations", []),

                "capabilities": best_support.get("capabilities", {}),

                "candidates": [

                    {"label": c[0].label, "support_level": getattr(c[0], "support_level", ""),

                     "capabilities": engine_support_summary(c[0]).get("capabilities", {}),

                     "score": c[1], "reasons": c[2]}

                    for c in candidates

                ] if candidates else [],

            }

        except Exception as e:

            return {"label": "检测失败", "name": "error",

                    "support_level": "error", "limitations": [str(e)], "candidates": []}



    # ── 封面查找 ──



    def find_cover(self, game_dir: str):

        """查找游戏封面：散装图片 → 引擎图标文件 → exe 内嵌图标。



        返回 {"uri": dataURI, "kind": "cover"|"icon"}，找不到返回 None。

        kind=cover 整卡铺满；kind=icon 是方形小图标，前端居中展示。

        """

        try:

            root = Path(game_dir)

            if not root.exists() or not root.is_dir():

                return None

            cache_key = str(root.resolve()).casefold()
            if cache_key in _COVER_CACHE:
                return _COVER_CACHE[cache_key]

            uri = self._find_loose_cover(root)

            if uri:

                result = {"uri": uri, "kind": "cover"}
                _COVER_CACHE[cache_key] = result
                return result

            uri = self._find_icon_file(root)

            if uri:

                result = {"uri": uri, "kind": "icon"}
                _COVER_CACHE[cache_key] = result
                return result

            uri = self._find_exe_icon(root)

            if uri:

                result = {"uri": uri, "kind": "icon"}
                _COVER_CACHE[cache_key] = result
                return result

        except Exception:

            pass

        try:
            _COVER_CACHE[str(Path(game_dir).resolve()).casefold()] = None
        except Exception:
            pass

        return None



    @staticmethod

    def _data_uri(p: Path) -> str:

        import base64, mimetypes

        MAX_COVER_BYTES = 200 * 1024  # 200KB 上限，防止大图导致 bridge 崩溃

        if p.stat().st_size > MAX_COVER_BYTES:

            return ""

        data = p.read_bytes()

        mime, _ = mimetypes.guess_type(p.name)

        if not mime:

            mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"

        b64 = base64.b64encode(data).decode("ascii")

        return f"data:{mime};base64,{b64}"



    def _find_loose_cover(self, root: Path) -> str:

        """docstring"""

        images = {}

        for p in sorted(root.iterdir()):

            if (p.is_file() and not p.name.startswith(".")

                    and not p.stem.lower().startswith("icon")

                    and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")

                    and p.stat().st_size < 2 * 1024 * 1024):

                images.setdefault(p.name.lower(), p)

        if not images:

            return ""

        for stem in ("cover", "package", "title", "splash", "banner"):

            for ext in (".jpg", ".jpeg", ".png", ".webp"):

                if stem + ext in images:

                    return self._data_uri(images[stem + ext])

        return self._data_uri(images[sorted(images)[0]])



    def _find_icon_file(self, root: Path) -> str:

        """docstring"""

        for rel in ("www/icon/icon.png", "icon/icon.png", "icon.png",

                    "game/gui/window_icon.png", "resources/icon.png", "data/icon.png"):

            p = root / rel

            if p.is_file() and p.stat().st_size < 2 * 1024 * 1024:

                return self._data_uri(p)

        return ""



    def _find_exe_icon(self, root: Path) -> str:
        """从 exe 提取 Windows 应用图标（标准 RT_GROUP_ICON）。"""
        import base64, struct
        skip_kw = ["unins", "crash", "unitycrash", "config", "setting",
                   "patcher", "update", "launcher_patch", "notification"]
        exes = [f for f in root.iterdir()
                if f.is_file() and f.suffix.lower() == ".exe"
                and not any(k in f.stem.lower() for k in skip_kw)]
        exes.sort(key=lambda x: x.stat().st_size, reverse=True)

        for exe in exes[:3]:
            try:
                result = self._extract_pe_icon(exe)
                if result:
                    data, mime = result
                    b64 = base64.b64encode(data).decode("ascii")
                    return f"data:{mime};base64,{b64}"
            except Exception:
                continue
        return ""

    @staticmethod
    def _extract_pe_icon(exe_path: Path):
        """纯 Python PE 图标提取。

        A: RT_GROUP_ICON → RT_ICON (标准 Windows 应用图标)
        B: 直接遍历 RT_ICON 条目 (无 group 的 exe)
        C: 资源区扫描 PNG 签名 (兜底)
        返回 (bytes, mime) 或 None。
        """
        import struct
        with open(exe_path, "rb") as f:
            rd = lambda off, n: (f.seek(off), f.read(n))[1] if f.seek(off) is None and len(f.read(n)) == n else None
            # PE 头
            head = f.read(64)
            if len(head) < 64 or head[:2] != b"MZ": return None
            e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
            f.seek(e_lfanew); pe_hdr = f.read(24)
            if len(pe_hdr) < 24 or pe_hdr[:4] != b"PE\0\0": return None
            num_sections = struct.unpack_from("<H", pe_hdr, 6)[0]
            opt_size = struct.unpack_from("<H", pe_hdr, 20)[0]
            f.seek(e_lfanew + 24); opt = f.read(opt_size)
            if len(opt) < 2: return None
            magic = struct.unpack_from("<H", opt, 0)[0]
            dd_off = 96 if magic == 0x10B else 112
            if dd_off + 24 > len(opt): return None
            res_rva = struct.unpack_from("<I", opt, dd_off + 16)[0]
            if not res_rva: return None

            # 区段表
            secs = []
            for i in range(num_sections):
                f.seek(e_lfanew + 24 + opt_size + i * 40); s = f.read(40)
                if len(s) < 40: return None
                vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", s, 8)
                secs.append((va, vsize, rawsize, rawptr))

            def off_of(rva_):
                for va_, vs_, rs_, rp_ in secs:
                    if va_ <= rva_ < va_ + max(vs_, rs_):
                        return rp_ + (rva_ - va_)
                return None

            res_base = off_of(res_rva)
            if res_base is None: return None

            def read_dir(dir_off):
                f.seek(res_base + dir_off); hdr = f.read(16)
                if len(hdr) < 16: return []
                n_named, n_id = struct.unpack_from("<HH", hdr, 12)
                entries = []
                for k in range(n_named + n_id):
                    f.seek(res_base + dir_off + 16 + k * 8); e = f.read(8)
                    if len(e) < 8: break
                    entries.append(struct.unpack_from("<II", e, 0))
                return entries

            def first_leaf(off):
                depth = 0
                while off & 0x80000000:
                    es = read_dir(off & 0x7FFFFFFF)
                    if not es: return None
                    off = es[0][1]; depth += 1
                    if depth > 4: return None
                f.seek(res_base + off); de = f.read(16)
                return struct.unpack_from("<II", de, 0) if len(de) >= 16 else None

            RT_ICON, RT_GROUP_ICON = 3, 14
            root_dir = read_dir(0)
            tm = {n: o for n, o in root_dir if not (o & 0x80000000)}
            grp_off = tm.get(RT_GROUP_ICON)
            ico_off = tm.get(RT_ICON)

            # A: 标准 GRPICONDIR
            if grp_off is not None and ico_off is not None and (grp_off & 0x80000000):
                try:
                    gl = first_leaf(grp_off)
                    if gl:
                        go = off_of(gl[0])
                        if go is not None:
                            f.seek(go); grp = f.read(min(gl[1], 6 + 14 * 64))
                            if len(grp) >= 6:
                                cnt = struct.unpack_from("<H", grp, 4)[0]
                                best = None
                                for k in range(cnt):
                                    ba = 6 + k * 14
                                    if ba + 14 > len(grp): break
                                    w, h, _, _, _, bc, _, iid = struct.unpack_from("<BBBBHHIH", grp, ba)
                                    w, h = (w or 256), (h or 256)
                                    if best is None or (w, bc) > (best[0], best[1]):
                                        best = (w, bc, iid, h)
                                if best:
                                    target = None
                                    for nid, eo in read_dir(ico_off & 0x7FFFFFFF):
                                        if nid == best[2]: target = eo; break
                                    if target:
                                        lf = first_leaf(target)
                                        if lf and lf[1] <= 4 * 1024 * 1024:
                                            di = off_of(lf[0])
                                            if di is not None:
                                                f.seek(di); img = f.read(lf[1])
                                                if len(img) == lf[1]:
                                                    if img[:8] == b"\x89PNG\r\n\x1a\n":
                                                        return img, "image/png"
                                                    ico_bytes = (struct.pack("<HHH", 0, 1, 1) +
                                                        struct.pack("<BBBBHHII", best[0] % 256, best[3] % 256, 0, 0, 1, best[1], len(img), 22) + img)
                                                    return ico_bytes, "image/x-icon"
                except Exception:
                    pass

            # B: 直接遍历 RT_ICON
            if ico_off is not None and (ico_off & 0x80000000):
                try:
                    best_img = None; best_sz = -1
                    for _, eo in read_dir(ico_off & 0x7FFFFFFF):
                        lf = first_leaf(eo)
                        if not lf or lf[1] > 4 * 1024 * 1024: continue
                        di = off_of(lf[0])
                        if di is None: continue
                        f.seek(di); img = f.read(lf[1])
                        if len(img) != lf[1] or len(img) <= best_sz: continue
                        if img[:8] == b"\x89PNG\r\n\x1a\n":
                            best_img = (img, "image/png"); best_sz = len(img)
                        elif img[:2] == b"BM" and len(img) < 500 * 1024:
                            best_img = (img, "image/bmp"); best_sz = len(img)
                    if best_img: return best_img
                except Exception:
                    pass

            # C: 资源区扫描 PNG
            try:
                for va, vs, rs, rp in secs:
                    if va <= res_rva < va + max(vs, rs):
                        f.seek(rp); chunk = f.read(min(rs, 3 * 1024 * 1024))
                        pos = 0; best, best_sz = None, 0
                        while True:
                            idx = chunk.find(b"\x89PNG\r\n\x1a\n", pos)
                            if idx < 0: break
                            eoi = chunk.find(b"IEND", idx)
                            if eoi < 0 or eoi - idx > 200 * 1024: pos = idx + 8; continue
                            sz = eoi + 8 - idx
                            w, h = struct.unpack(">II", chunk[idx+16:idx+24])
                            if max(w, h) <= 512 and min(w, h) >= 8 and sz > best_sz:
                                best = chunk[idx:eoi+8]; best_sz = sz
                            pos = idx + 8
                        if best and best_sz > 200: return best, "image/png"
                        break
            except Exception:
                pass
            return None

    # ── 安装/更新工具 ──

    def get_hy_mt2_status(self):
        from translators.hy_mt2_component import component_status
        from translators.hy_mt2_runtime import runtime_status

        return {**component_status(), **runtime_status()}

    def install_hy_mt2_component(self):
        self._js("on_status('下载离线模型')")
        try:
            from translators.hy_mt2_component import component_status

            model_label = str(component_status().get("model_label") or "腾讯 Hy-MT2 离线模型")
        except Exception:
            model_label = "腾讯 Hy-MT2 离线模型"
        self._js("on_log(20, " + json.dumps(f"开始下载 {model_label}...", ensure_ascii=False) + ")")

        last_logged_percent = {"value": -10.0}

        def progress(event: dict):
            message = str(event.get("message") or "下载 Hy-MT2 离线组件")
            phase = str(event.get("phase") or "")
            percent = float(event.get("percent") or 0)
            self._js(f"on_progress({json.dumps(message, ensure_ascii=False)}, {percent:.2f})")
            self._js("onHyMt2InstallProgress(" + json.dumps(event, ensure_ascii=False) + ")")
            report_progress = phase != "download" or percent >= last_logged_percent["value"] + 10
            if report_progress:
                last_logged_percent["value"] = percent
                suffix = f" ({percent:.0f}%)" if phase == "download" else ""
                self._js("on_log(20, " + json.dumps(message + suffix, ensure_ascii=False) + ")")

        def _run(task):
            try:
                from translators.hy_mt2_component import install_component
                from translators.hy_mt2_runtime import verify_selected_model

                status = install_component(progress)
                verification_message = "自动启动本地后端并执行健康检查"
                progress({
                    "phase": "verify_backend",
                    "message": verification_message,
                    "current_bytes": 1,
                    "total_bytes": 1,
                    "percent": 100,
                })
                self._js("on_log(20, " + json.dumps(
                    f"{status.get('model_label') or 'Hy-MT2 离线模型'} 已下载，正在自动配置并验证本地后端...",
                    ensure_ascii=False,
                ) + ")")
                verification = verify_selected_model()
                status["deployment_verification"] = verification
                self._js("on_log(20, " + json.dumps(
                    f"{status.get('model_label') or 'Hy-MT2 离线模型'} 已完成一键部署："
                    f"{verification.get('backend') or 'unknown'} 后端已验证",
                    ensure_ascii=False,
                ) + ")")
                self._js("on_progress('离线组件安装完成', 100)")
                self._js("on_status('空闲')")
                self._js("onHyMt2Status(" + json.dumps(status, ensure_ascii=False) + ")")
            except Exception as exc:
                self._js("on_log(50, " + json.dumps("Hy-MT2 组件安装失败: " + str(exc), ensure_ascii=False) + ")")
                self._js("on_status('错误')")
                self._js("onHyMt2InstallFailed(" + json.dumps(str(exc), ensure_ascii=False) + ")")

        self._tasks.submit("下载 Hy-MT2 离线组件", _run, kind="models", counted=True)
        return True

    def remove_hy_mt2_component(self):
        def _run(task):
            try:
                from translators.hy_mt2_component import remove_component

                status = remove_component()
                self._js("on_log(20, " + json.dumps(
                    f"{status.get('model_label') or 'Hy-MT2 离线模型'} 已删除", ensure_ascii=False
                ) + ")")
                self._js("onHyMt2Status(" + json.dumps(status, ensure_ascii=False) + ")")
            except Exception as exc:
                self._js("on_log(50, " + json.dumps("删除 Hy-MT2 组件失败: " + str(exc), ensure_ascii=False) + ")")
                self._js("onHyMt2InstallFailed(" + json.dumps(str(exc), ensure_ascii=False) + ")")

        self._tasks.submit("删除 Hy-MT2 离线组件", _run, kind="models", counted=True)
        return True

    def start_background_tool_update_check(self):
        """Start one idle, throttled tool-update pass for the GUI session."""
        with self._background_tool_update_lock:
            if self._background_tool_update_started:
                return False
            self._background_tool_update_started = True

        def _run(task):
            import time
            time.sleep(8)
            # Keep the update check out of translation/extraction/repack tasks.
            while self._is_active_task_running():
                time.sleep(30)
            try:
                from core.tool_manager import maybe_auto_update_tools
                results = maybe_auto_update_tools()
                if not results:
                    return
                counts = {"updated": 0, "current": 0, "skipped": 0, "failed": 0}
                for item in results.values():
                    status = str(item.get("status", ""))
                    if status == "updated":
                        counts["updated"] += 1
                    elif status in {"current", "checked"}:
                        counts["current"] += 1
                    elif status == "failed":
                        counts["failed"] += 1
                    else:
                        counts["skipped"] += 1
                if counts["updated"] or counts["failed"]:
                    self._js("on_log(20, " + json.dumps(
                        f"后台工具更新检查完成：更新 {counts['updated']}，失败 {counts['failed']}",
                        ensure_ascii=False,
                    ) + ")")
            except Exception as e:
                self._js("on_log(40, " + json.dumps("后台工具更新检查跳过: " + str(e), ensure_ascii=False) + ")")

        self._tasks.submit("后台工具更新检查", _run, kind="tools", counted=False)
        return True

    def install_tools(self):
        self._js("on_status('安装中')")
        self._js("on_progress('准备工具更新', 0)")
        self._js("on_log(20, '开始检查更新并补齐推荐工具...')")

        def progress_cb(event: dict):
            pct = _tool_progress_percent(event, 0, 70)
            msg = str(event.get("message") or "检查工具更新")
            self._js(f"on_progress({json.dumps(msg, ensure_ascii=False)}, {pct})")

        def ensure_progress_cb(event: dict):
            pct = _tool_progress_percent(event, 70, 95)
            msg = str(event.get("message") or "检查工具依赖")
            self._js(f"on_progress({json.dumps(msg, ensure_ascii=False)}, {pct})")

        def _run(task):
            try:
                from core.tool_manager import ensure_recommended_tools, update_recommended_tools
                updates = update_recommended_tools(force=True, progress_callback=progress_cb)
                updated = [n for n, r in updates.items() if r.get("status") == "updated"]
                failed_updates = [n for n, r in updates.items() if r.get("status") == "failed"]
                if updated:
                    self._js("on_log(20, " + json.dumps("已更新: " + ", ".join(updated), ensure_ascii=False) + ")")
                if failed_updates:
                    self._js("on_log(40, " + json.dumps("更新失败: " + ", ".join(failed_updates), ensure_ascii=False) + ")")
                results = ensure_recommended_tools(progress_callback=ensure_progress_cb, auto_update=False)
                ok = [n for n, p in results.items() if p]
                missing = [n for n, p in results.items() if not p]
                if ok:
                    self._js("on_log(20, " + json.dumps("已就绪: " + ", ".join(ok), ensure_ascii=False) + ")")
                if missing:
                    self._js("on_log(40, " + json.dumps("缺失: " + ", ".join(missing), ensure_ascii=False) + ")")
                else:
                    self._js("on_log(20, '所有工具已就绪')")
                self._js("on_progress('工具准备完成', 100)")
                self._js("on_status('空闲')")
            except Exception as e:
                self._js("on_log(50, " + json.dumps("安装/更新工具失败: " + str(e), ensure_ascii=False) + ")")
                self._js("on_status('错误')")
        self._tasks.submit("安装工具", _run, kind="tools", counted=True)
        return True

    def update_tools(self):
        self._js("on_status('更新中')")
        self._js("on_progress('准备工具更新', 0)")
        self._js("on_log(20, '开始检查已安装托管工具更新...')")

        def progress_cb(event: dict):
            pct = _tool_progress_percent(event, 0, 95)
            msg = str(event.get("message") or "检查工具更新")
            self._js(f"on_progress({json.dumps(msg, ensure_ascii=False)}, {pct})")

        def _run(task):
            try:
                from core.tool_manager import update_recommended_tools
                results = update_recommended_tools(force=True, progress_callback=progress_cb)
                counts = {"updated": 0, "current": 0, "skipped": 0, "failed": 0}
                for item in results.values():
                    status = str(item.get("status", ""))
                    if status == "updated":
                        counts["updated"] += 1
                    elif status in {"current", "checked"}:
                        counts["current"] += 1
                    elif status == "failed":
                        counts["failed"] += 1
                    else:
                        counts["skipped"] += 1
                msg = (
                    f"工具更新检查完成：更新 {counts['updated']}，已是最新 {counts['current']}，"
                    f"跳过 {counts['skipped']}，失败 {counts['failed']}"
                )
                self._js("on_log(20, " + json.dumps(msg, ensure_ascii=False) + ")")
                self._js("on_progress('工具更新检查完成', 100)")
                self._js("on_status('空闲')")
            except Exception as e:
                self._js("on_log(50, " + json.dumps("工具更新失败: " + str(e), ensure_ascii=False) + ")")
                self._js("on_status('错误')")

        self._tasks.submit("更新工具", _run, kind="tools", counted=True)
        return True

    # ── 诊断报告 ──

    def update_app(self):
        self._js("on_status('软件更新中')")
        self._js("on_progress('准备软件更新', 0)")
        self._js("on_log(20, '开始软件更新...')")

        def progress_cb(event: dict):
            pct = _app_update_progress_percent(event)
            msg = _app_update_progress_message(event)
            detail = _app_update_progress_detail(event)
            self._js(
                f"on_progress({json.dumps(msg, ensure_ascii=False)}, {pct}, "
                f"{json.dumps(detail, ensure_ascii=False)})"
            )

        def _run(task):
            try:
                from core.app_update import check_update, download_update_package, start_update_and_exit

                status = check_update()
                self._js("renderAppUpdateInfo(" + json.dumps(status, ensure_ascii=False) + ")")
                latest = str(status.get("latest_version") or "")
                current = str(status.get("version") or "")
                if not status.get("update_available"):
                    self._js("on_log(20, " + json.dumps(f"已是最新版本：{current}", ensure_ascii=False) + ")")
                    self._js("on_progress('已是最新版本', 100)")
                    self._js("on_status('空闲')")
                    return
                if not status.get("can_apply_update"):
                    self._js("on_log(40, '源码运行模式不能直接覆盖更新，请在打包版中测试软件更新')")
                    self._js("on_status('空闲')")
                    return
                self._js("on_log(20, " + json.dumps(f"发现新版本 {latest}，开始下载更新包", ensure_ascii=False) + ")")
                package = download_update_package(status.get("manifest") or {}, progress_callback=progress_cb)
                self._js("on_progress('启动更新器', 92)")
                result = start_update_and_exit(package, status.get("manifest") or {})
                self._js("on_log(20, " + json.dumps(str(result.get("message") or "更新器已启动"), ensure_ascii=False) + ")")
                self._js("on_progress('更新器已启动，软件即将关闭并安装；稍后手动打开就是新版', 100)")
                self._js("on_status('安装更新中')")
                # 成功路径故意不收尾（旧 exiting=True 语义）：进程即将退出，
                # 保持任务 running、不减计数、不触发授权刷新。
                task.detach()

                def _close_for_update():
                    try:
                        if self._window:
                            self._window.destroy()
                    except Exception:
                        pass
                    finally:
                        threading.Timer(0.6, lambda: os._exit(0)).start()

                def _force_exit_if_needed():
                    os._exit(0)

                threading.Timer(1.0, _close_for_update).start()
                threading.Timer(4.0, _force_exit_if_needed).start()
            except Exception as e:
                self._js("on_log(50, " + json.dumps("软件更新失败: " + str(e), ensure_ascii=False) + ")")
                self._js("on_status('错误')")

        self._tasks.submit("软件更新", _run, kind="app_update", counted=True)
        return True

    def start_background_app_update_predownload(self, manifest):
        try:
            from core.app_update import start_background_predownload
            if isinstance(manifest, str):
                manifest = json.loads(manifest)
            if not isinstance(manifest, dict) or not manifest.get("version"):
                return False
            return start_background_predownload(manifest)
        except Exception:
            return False

    def open_diagnostics(self):
        import os
        diag = Path.home() / "Downloads" / ".game_translator" / "workspaces" / "latest_diagnostics.json"
        if diag.exists():
            try:
                os.startfile(str(diag))
                self._js("on_log(20, '已打开诊断报告')")
            except Exception as e:
                self._js(f"on_log(50, '无法打开诊断报告: {e}')")
        else:
            self._js("on_log(40, '还没有诊断报告，先运行一次翻译或仅提取')")
        return True

    # ── CJK 字体选择 ──

    def select_cjk_font(self):
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw()
        path = filedialog.askopenfilename(
            title="选择 CJK 中文字体 (TTF/OTF)",
            filetypes=[("TrueType 字体", "*.ttf"), ("OpenType 字体", "*.otf"), ("所有文件", "*.*")]
        )
        root.destroy()
        if path:
            path = path.replace("/", "\\")
            from config import get_config, save_config
            c = get_config(); c.cjk_font_path = path; save_config()
        return path

    # ── 背景图片 ──

    def read_bg_image(self, path: str):
        """读取图片文件返回 base64 data URL，供 CSS 背景使用。"""
        import base64
        from pathlib import Path
        p = Path(path)
        if not p.exists():
            return ""
        ext = p.suffix.lower()
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp", "bmp": "bmp"}
        mt = mime.get(ext.lstrip("."), "jpeg")
        data = base64.b64encode(p.read_bytes()).decode()
        return f"data:image/{mt};base64,{data}"

    # ── 卸载汉化 ──

    def uninstall_translation(self, path: str):
        from core.manifest import uninstall_game_translation
        from pathlib import Path
        def _run(task):
            self._js("on_status('卸载中')")
            self._js("on_log(20, '开始卸载汉化...')")
            try:
                result = uninstall_game_translation(Path(path))
                restored = result.get("restored", 0)
                deleted = result.get("deleted", 0)
                self._js(f"on_log(20, '卸载完成: 恢复 {restored} 个文件, 删除 {deleted} 个文件')")
                self._js("on_status('空闲')")
            except Exception as e:
                self._js(f"on_log(50, '卸载失败: {e}')")
                self._js("on_status('错误')")
        self._tasks.submit("卸载汉化", _run, kind="uninstall", counted=False)
        return True

    def _do_run(self, path: str, injector: str = "xunity",

                 coverage: int = 100, extract_only: bool = False,

                 resume_json: str = "", patch_only: bool = False):

        """Run pipeline in background."""
        # 安全解析 .lnk 快捷方式 → 真实路径
        path = self.resolve_path(path)
        def _run(task):

            try:

                from config import get_config

                cfg = get_config()

                cfg.translation_coverage = coverage



                from core.pipeline import Pipeline

                self._js("on_status('运行中')")

                mode = "提取" if extract_only else "回填" if patch_only else "翻译"

                self._js(f"on_log(20, {json.dumps(f'开始{mode}: {path}')})")



                def progress_cb(step: str, pct: float):

                    self._js(f"on_progress({json.dumps(step)}, {pct})")

                    self._js(f"on_log(20, {json.dumps(f'[{step}] {pct:.0f}%')})")



                pipeline = Pipeline(

                    progress_callback=progress_cb,

                    item_progress_callback=lambda cur, tot: progress_cb(

                        f"翻译中 ({cur}/{tot})", 40 + cur / max(tot, 1) * 15

                    ),

                    meta_callback=lambda key, val: self._js(

                        f"on_meta({json.dumps(key)}, {json.dumps(val)})"

                    ),

                )



                if extract_only:

                    success = pipeline.run_with_checkpoint(

                        path, launch=False, extract_only=True)

                elif patch_only:

                    checkpoint = resume_json or str(

                        Path.home() / "Downloads" / ".game_translator" / "workspaces" / "translation_checkpoint.json"

                    )

                    success = pipeline.run_with_checkpoint(

                        path, launch=False, patch_only=True, resume_json=checkpoint)

                else:

                    success = pipeline.run(path, launch=cfg.auto_launch, injector=injector)



                if success:

                    self._js("on_status('完成')")

                    self._js(f"on_log(20, {json.dumps(f'{mode}完成')})")

                    self._js("on_progress('完成', 100)")

                else:

                    self._js("on_status('失败')")

                    self._js(f"on_log(50, {json.dumps(f'{mode}失败，请查看日志')})")



            except Exception as e:

                self._js(f"on_status('错误')")

                self._js(f"on_log(50, {json.dumps(f'错误: {e}')})")



        name = "提取" if extract_only else "回填" if patch_only else "翻译"
        self._tasks.submit(name, _run, kind="translate", counted=True)

    def _do_polish(self, path: str, budget_cny: float = 1.0):

        path = self.resolve_path(path)

        def _run(task):

            try:

                from core.pipeline import Pipeline

                self._js("on_status('运行中')")
                self._js(f"on_log(20, {json.dumps(f'开始文本润色: {path}')})")

                def progress_cb(step: str, pct: float):

                    self._js(f"on_progress({json.dumps(step)}, {pct})")

                    self._js(f"on_log(20, {json.dumps(f'[{step}] {pct:.0f}%')})")

                pipeline = Pipeline(

                    progress_callback=progress_cb,

                    item_progress_callback=lambda cur, tot: progress_cb(

                        f"润色中 ({cur}/{tot})", 45 + cur / max(tot, 1) * 25

                    ),

                    meta_callback=lambda key, val: self._js(

                        f"on_meta({json.dumps(key)}, {json.dumps(val)})"

                    ),

                )

                success = pipeline.polish_checkpoint(

                    path,

                    budget_cny=budget_cny,

                    launch=False,

                )

                if success:

                    self._js("on_status('完成')")

                    self._js("on_log(20, '文本润色完成')")

                    self._js("on_progress('完成', 100)")

                else:

                    self._js("on_status('失败')")

                    self._js("on_log(50, '文本润色失败，请查看日志')")

            except Exception as e:

                self._js("on_status('错误')")

                self._js(f"on_log(50, {json.dumps(f'错误: {e}')})")

        self._tasks.submit("润色", _run, kind="translate", counted=True)



if __name__ == "__main__":
    from core.gui_gpu_preference import configure_high_performance_gui_gpu

    gpu_preference = configure_high_performance_gui_gpu()
    print(
        "[GUI GPU] high-performance preference: "
        f"host_registry={gpu_preference['host_registry_preference']}, "
        f"webview_registry={gpu_preference['webview_registry_preference']}, "
        f"d3d11={gpu_preference['webview_d3d11']}, "
        f"hardware_override={gpu_preference['webview_hardware_override']}, "
        f"exe={gpu_preference['executable']}"
    )
    import webview

    try:
        from core.app_update import exit_if_update_pending_at_startup

        if exit_if_update_pending_at_startup():
            sys.exit(0)
    except Exception:
        pass

    api = Api()

    window = webview.create_window(

        "EngAixt",

        str(resource_path("web", "index.html")),

        js_api=api,

        width=1100,

        height=740,

        resizable=True,

        min_size=(840, 600),

    )

    api.set_window(window)

    webview.start()
