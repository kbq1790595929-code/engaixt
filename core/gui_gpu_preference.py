from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path


DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE = 2
_USER_GPU_PREFERENCES = r"Software\Microsoft\DirectX\UserGpuPreferences"


def configure_high_performance_gui_gpu() -> dict[str, str | bool]:
    """Ask Windows and WebView2 to use the high-performance GPU for this GUI."""
    result: dict[str, str | bool] = {
        "supported": os.name == "nt",
        "process_preference": "unavailable",
        "host_registry_preference": False,
        "webview_registry_preference": False,
        "webview_d3d11": False,
        "webview_hardware_override": False,
        "executable": str(Path(sys.executable).resolve()),
    }
    if os.name != "nt":
        return result

    result["webview_d3d11"] = _configure_webview2_d3d11()
    result["webview_hardware_override"] = "--ignore-gpu-blocklist" in str(
        os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS") or ""
    ).split()
    try:
        dxgi = ctypes.WinDLL("dxgi", use_last_error=True)
        set_preference = dxgi.SetProcessDefaultGpuPreference
        set_preference.argtypes = [ctypes.c_int]
        set_preference.restype = ctypes.c_long
        result["process_preference"] = set_preference(DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE) == 0
    except Exception:
        pass

    result["host_registry_preference"] = set_high_performance_gpu_preference(
        Path(sys.executable).resolve()
    )
    webview_executable = _find_webview2_executable()
    if webview_executable:
        result["webview_registry_preference"] = set_high_performance_gpu_preference(
            webview_executable
        )
    return result


def set_high_performance_gpu_preference(executable: Path) -> bool:
    """Persist Windows Graphics Settings' high-performance preference for an EXE."""
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _USER_GPU_PREFERENCES) as key:
            winreg.SetValueEx(key, str(executable), 0, winreg.REG_SZ, "GpuPreference=2;")
        return True
    except Exception:
        return False


def _find_webview2_executable() -> Path | None:
    configured_folder = os.environ.get("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER")
    configured_path = Path(configured_folder or "") / "msedgewebview2.exe"
    if configured_folder and configured_path.is_file():
        return configured_path.resolve()

    program_files_x86 = Path(
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    )
    runtime_root = program_files_x86 / "Microsoft" / "EdgeWebView" / "Application"
    candidates = list(runtime_root.glob("*/msedgewebview2.exe"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: _version_key(path.parent.name)).resolve()


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) if part.isdigit() else -1 for part in value.split("."))


def _configure_webview2_d3d11() -> bool:
    d3d11_flag = "--use-angle=d3d11"
    hardware_override_flag = "--ignore-gpu-blocklist"
    existing = str(os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS") or "").strip()
    arguments = existing.split()
    d3d11_added = not any(argument.startswith("--use-angle=") for argument in arguments)
    if d3d11_added:
        arguments.append(d3d11_flag)
    # Chromium otherwise falls back to WARP on this legacy multi-GPU setup.
    if hardware_override_flag not in arguments:
        arguments.append(hardware_override_flag)
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = " ".join(arguments)
    return d3d11_added
