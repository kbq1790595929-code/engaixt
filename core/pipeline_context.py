from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from config import DIAGNOSTIC_SNAPSHOT_FIELDS, get_config, is_sensitive_field
from core.diagnostics import Diagnostics
from core.pipeline_stages import stage_plan
from core.resources import app_root
from core.workspace import Workspace


def begin_run_context(pipeline, input_path: str, path: Path, mode: dict):
    config = get_config()
    pipeline.workspace = Workspace(
        base_dir=Path(config.workspace_dir) if config.workspace_dir else None
    )
    pipeline.diagnostics = Diagnostics(pipeline.workspace.root)
    pipeline.diagnostics.set("input_path", str(input_path))
    pipeline.diagnostics.set("resolved_input_path", str(path.resolve()))
    pipeline.diagnostics.set("game_exe", _game_exe_snapshot(path))
    pipeline.diagnostics.set("mode", mode)
    pipeline.diagnostics.set("stage_plan", stage_plan())
    pipeline.diagnostics.set("app_runtime", _app_runtime_snapshot())
    pipeline.diagnostics.set("config_snapshot", _config_snapshot(config))
    provider = str(getattr(config, "active_translator", "") or "")
    try:
        from translators.factory import translator_model, translator_prompt_version

        model = translator_model(provider, config)
        prompt_version = translator_prompt_version(provider)
    except Exception:
        model = "unknown"
        prompt_version = "legacy_v1"
    try:
        from core.usage_statistics import start_usage_run
        from core.game_identity import resolve_game_identity

        identity = resolve_game_identity(path)
        pipeline.diagnostics.set("game_identity", identity.to_dict())
        if mode.get("extract_only"):
            run_mode = "extract"
        elif mode.get("patch_only"):
            run_mode = "patch"
        elif mode.get("polish"):
            run_mode = "polish"
        elif mode.get("checkpoint"):
            run_mode = "checkpoint"
        else:
            run_mode = "translate"
        pipeline.usage_run_id = start_usage_run(
            game_title=identity.title,
            title_source=identity.source,
            game_path=str(path.resolve()),
            mode=run_mode,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
        )
        pipeline.diagnostics.set("usage_run_id", pipeline.usage_run_id)
    except Exception as exc:
        pipeline.usage_run_id = ""
        pipeline.diagnostics.warn("使用统计初始化失败，翻译流程继续", error=str(exc))
    pipeline._blocked_count = 0
    return config


def _game_exe_snapshot(path: Path) -> dict[str, Any]:
    """游戏主 exe 的路径/mtime/大小快照。

    "以前可以现在不行"的排查第一步是确认游戏本体是否变过，
    这里记录的是事实证据，找不到 exe 时如实记空。
    """
    try:
        from core.exe_selector import find_main_exe

        game_dir = path if path.is_dir() else path.parent
        exe = find_main_exe(game_dir)
        if not exe:
            return {}
        exe = Path(exe)
        stat = _safe_stat(exe)
        return {
            "path": str(exe),
            "mtime": stat.get("mtime", 0.0),
            "size": stat.get("size", 0),
        }
    except Exception as exc:
        return {"error": str(exc)}


def _app_runtime_snapshot() -> dict[str, Any]:
    executable = Path(sys.executable).resolve()
    stat = _safe_stat(executable)
    info = _current_app_info_safe()
    root = app_root()
    return {
        **info,
        "packaged": bool(getattr(sys, "frozen", False)),
        "sys_executable": str(executable),
        "executable_mtime": stat.get("mtime", 0.0),
        "executable_size": stat.get("size", 0),
        "executable_sha256": _file_sha256(executable),
        "app_root": str(root),
        "cwd": str(Path.cwd()),
        "pid": os.getpid(),
        "argv0": sys.argv[0] if sys.argv else "",
        "git": _git_snapshot(root),
    }


def _current_app_info_safe() -> dict[str, Any]:
    try:
        from core.app_update import current_app_info

        return dict(current_app_info())
    except Exception as exc:
        return {
            "app": "EngAixt",
            "version": "unknown",
            "can_apply_update": False,
            "install_dir": "",
            "error": str(exc),
        }


def _config_snapshot(config: Any) -> dict[str, Any]:
    names = set(DIAGNOSTIC_SNAPSHOT_FIELDS)
    if is_dataclass(config):
        names.update(field.name for field in fields(config))

    snapshot: dict[str, Any] = {}
    for name in sorted(names):
        if is_sensitive_field(name) or not hasattr(config, name):
            continue
        value = getattr(config, name)
        if _is_snapshot_value(value):
            snapshot[name] = value

    try:
        from translators.factory import translator_model, translator_prompt_version

        active = str(getattr(config, "active_translator", "") or "")
        snapshot["active_translator_model"] = translator_model(active, config)
        snapshot["active_translator_prompt_version"] = translator_prompt_version(active)
    except Exception as exc:
        snapshot["active_translator_model_error"] = str(exc)
    return snapshot




def _is_snapshot_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_snapshot_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_snapshot_value(val) for key, val in value.items())
    return False


def _safe_stat(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
        return {"mtime": stat.st_mtime, "size": stat.st_size}
    except OSError:
        return {"mtime": 0.0, "size": 0}


def _file_sha256(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def _git_snapshot(root: Path) -> dict[str, Any]:
    if bool(getattr(sys, "frozen", False)):
        return {}
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if head.returncode != 0:
            return {}
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return {
            "head": head.stdout.strip(),
            "dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
        }
    except (OSError, subprocess.SubprocessError):
        return {}
