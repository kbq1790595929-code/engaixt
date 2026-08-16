from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

from config import get_config
from core.tool_manager import get_tools_dir, tool_status


def run_preflight(game_path: Path, engine: object | None = None,
                  injector: str | None = None) -> dict:
    """Collect dependency and environment checks before translation starts."""
    cfg = get_config()
    engine_name = getattr(engine, "name", "") if engine else ""
    checks: list[dict] = []
    suggestions: list[str] = []

    _check_path(game_path, checks, suggestions)
    _check_translator(cfg.active_translator, checks, suggestions)

    if engine_name in ("unity", "xunity_realtime") or injector == "xunity":
        _check_python_module("UnityPy", checks, suggestions, "pip install UnityPy")
        _check_python_module("openai", checks, suggestions, "pip install openai")
        _check_managed_tool("assetripper", checks, suggestions, required=False)

    if engine_name == "renpy":
        _check_python_module("unrpa", checks, suggestions, "pip install unrpa")
        _check_managed_tool("unrpyc", checks, suggestions, required=False)

    if injector == "frida" and engine_name in ("bgi", "gamemaker", "rpgmaker", "kirikiri"):
        _check_python_module("frida", checks, suggestions, "pip install frida-tools")

    if engine_name in ("godot_frida",) or (injector == "frida" and engine_name in ("", "godot", "godot_pck", "godot_frida")):
        _check_python_module("frida", checks, suggestions, "pip install frida-tools")
        _check_python_module("Crypto", checks, suggestions, "pip install pycryptodome")
        _check_managed_tool("gdpack", checks, suggestions, required=False)
        _check_managed_tool("gdsdecomp", checks, suggestions, required=False)
        _check_managed_tool("gdre_tools", checks, suggestions, required=False)

    if engine_name in ("godot", "godot_pck"):
        _check_managed_tool("gdre_tools", checks, suggestions, required=False)
        _check_tool_file("Godot_v4.4.1-stable_win64.exe.zip", checks, suggestions, required=False)

    if engine_name == "godot_frida":
        _check_tool_file("Godot_v4.4.1-stable_win64.exe.zip", checks, suggestions, required=False)

    if engine_name == "rpgmaker":
        _check_managed_tool("rpgmdec", checks, suggestions, required=False)
        _check_managed_tool("rpgmad", checks, suggestions, required=False)

    if engine_name == "kirikiri":
        _check_managed_tool("garbro_console", checks, suggestions, required=False)
        _check_managed_tool("garbro_gui", checks, suggestions, required=False)

    if engine_name == "wolf":
        try:
            from engines.wolf.toolchain import bundled_uberwolf_path
            bundled = bundled_uberwolf_path()
        except Exception:
            bundled = Path()
        if bundled.is_file():
            checks.append({
                "name": "tool:uberwolf",
                "status": "ok",
                "required": False,
                "detail": f"内置 UberWolfCli: {bundled}",
            })
        else:
            _check_managed_tool("uberwolf", checks, suggestions, required=False)

    if engine_name == "unreal":
        _check_managed_tool("unrealpak", checks, suggestions, required=False)
        _check_managed_tool("fmodel", checks, suggestions, required=False)

    ok = all(c["status"] != "missing" or not c.get("required", True) for c in checks)
    return {"ok": ok, "checks": checks, "suggestions": suggestions}


def _check_path(path: Path, checks: list[dict], suggestions: list[str]):
    path_str = str(path.resolve())
    exists = path.exists()
    checks.append({
        "name": "input_path",
        "status": "ok" if exists else "missing",
        "required": True,
        "detail": path_str,
    })
    if any(ord(c) > 127 for c in path_str):
        checks.append({
            "name": "non_ascii_path",
            "status": "warning",
            "required": False,
            "detail": "路径包含非 ASCII 字符，少数外部工具可能失败",
        })
        suggestions.append("如果外部工具异常，尝试把游戏移动到纯英文路径后重试。")


def _check_translator(name: str, checks: list[dict], suggestions: list[str]):
    cfg = get_config()
    try:
        from translators.factory import translator_api_key

        key = translator_api_key(name, cfg)
    except Exception:
        key = ""

    checks.append({
        "name": f"{name}_api_key",
        "status": "ok" if key else "missing",
        "required": False,
        "detail": "已配置" if key else "未配置；只能提取/回填已有 JSON，不能调用 AI 翻译",
    })
    if not key:
        suggestions.append("在设置里配置翻译 API Key，或使用“仅提取/从 JSON 回填”的离线流程。")


def _check_python_module(module_name: str, checks: list[dict], suggestions: list[str],
                         install_hint: str, required: bool = True):
    ok = importlib.util.find_spec(module_name) is not None
    checks.append({
        "name": f"python_module:{module_name}",
        "status": "ok" if ok else "missing",
        "required": required,
        "detail": "已安装" if ok else install_hint,
    })
    if not ok:
        suggestions.append(f"缺少 Python 依赖 {module_name}：{install_hint}")


def _check_managed_tool(name: str, checks: list[dict], suggestions: list[str],
                        required: bool = True):
    status = tool_status(name)
    ok = bool(status.get("found"))
    checks.append({
        "name": f"tool:{name}",
        "status": "ok" if ok else "missing",
        "required": required,
        "detail": status.get("path") or status.get("homepage") or str(get_tools_dir()),
    })
    if not ok and required:
        suggestions.append(f"缺少外部工具 {name}，请放到 {get_tools_dir()} 或启用自动下载。")
    elif not ok:
        notes = status.get("notes") or ""
        suggestions.append(f"可选工具 {name} 未找到；安装后可提高对应引擎成功率。{notes}")


def _check_tool_file(filename: str, checks: list[dict], suggestions: list[str],
                     required: bool = True):
    cfg = get_config()
    tools_dir = Path(cfg.tools_dir) if cfg.tools_dir else Path.home() / "Downloads" / ".game_translator" / "tools"
    path = tools_dir / filename
    found = path.exists() or shutil.which(Path(filename).stem) is not None
    checks.append({
        "name": f"tool:{filename}",
        "status": "ok" if found else "missing",
        "required": required,
        "detail": str(path),
    })
    if not found and required:
        suggestions.append(f"缺少外部工具 {filename}，请放到 {tools_dir} 或启用自动下载。")
