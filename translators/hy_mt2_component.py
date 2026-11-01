from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from translators.hy_mt2_models import (
    DEFAULT_MODEL_NAME,
    HyMt2ModelSpec,
    model_choices,
    model_spec,
    resolve_model_name,
)
from utils.logger import info

# Backward-compatible aliases for callers and older plugin code. New code must
# resolve the selected model through model_spec() instead of using these values.
MODEL_NAME = DEFAULT_MODEL_NAME
MODEL_FILENAME = model_spec().filename
MODEL_SIZE = model_spec().size
MODEL_SHA256 = model_spec().sha256
MODEL_URL = model_spec().source_url
MODEL_LICENSE_URL = model_spec().license_url
LLAMA_RELEASE = "b10085"
LLAMA_LICENSE_URL = f"https://raw.githubusercontent.com/ggml-org/llama.cpp/{LLAMA_RELEASE}/LICENSE"

ProgressCallback = Callable[[dict], None]
_LOCAL_MODEL_DISCOVERY_TTL_SECONDS = 300.0
_local_model_discovery_cache: dict[str, tuple[float, Path | None]] = {}
_COMPONENT_STATUS_TTL_SECONDS = 1.5
_component_status_cache: dict[tuple, tuple[float, dict]] = {}
_component_status_lock = threading.RLock()


class ComponentInstallCancelled(RuntimeError):
    """Raised when the user cancels a local-model install."""


def _raise_if_cancelled(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ComponentInstallCancelled("本地 GGUF 组件下载已取消")


def invalidate_component_status_cache() -> None:
    with _component_status_lock:
        _component_status_cache.clear()


@dataclass(frozen=True)
class RuntimeAsset:
    filename: str
    size: int
    sha256: str

    @property
    def url(self) -> str:
        return (
            f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_RELEASE}/"
            f"{self.filename}"
        )


RUNTIME_ASSETS: dict[str, tuple[RuntimeAsset, ...]] = {
    "vulkan": (
        RuntimeAsset(
            f"llama-{LLAMA_RELEASE}-bin-win-vulkan-x64.zip",
            33_283_041,
            "624c59402d227b207a06bf9b41a845d9514dcfb794904798bdebc3ec02858001",
        ),
    ),
    "cuda-12.4": (
        RuntimeAsset(
            f"llama-{LLAMA_RELEASE}-bin-win-cuda-12.4-x64.zip",
            249_139_859,
            "c4745837fd17687e8ce9bc3a2d70afdb7f9689c7768bf43f2650592c90fa5733",
        ),
        RuntimeAsset(
            "cudart-llama-bin-win-cuda-12.4-x64.zip",
            391_443_627,
            "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6",
        ),
    ),
    "cuda-13.3": (
        RuntimeAsset(
            f"llama-{LLAMA_RELEASE}-bin-win-cuda-13.3-x64.zip",
            145_604_885,
            "e2c85ea958b264c65b3c6f09b54aaf4a8a67a122f09a4614fdc237f71db1e5f5",
        ),
        RuntimeAsset(
            "cudart-llama-bin-win-cuda-13.3-x64.zip",
            390_970_417,
            "1462a050eb4c684921ba51dcc4cc488a036674c3e73e9945ee705b854808d03e",
        ),
    ),
}


def component_dir() -> Path:
    return Path.home() / "Downloads" / ".game_translator" / "models" / "hy_mt2"


def selected_model_name() -> str:
    try:
        from config import get_config

        return resolve_model_name(getattr(get_config(), "hy_mt2_model", ""))
    except Exception:
        return DEFAULT_MODEL_NAME


def configured_custom_model_path() -> Path | None:
    """Return the configured custom path, even if the file is currently missing."""
    try:
        from config import get_config

        raw = str(getattr(get_config(), "hy_mt2_custom_model_path", "") or "").strip()
    except Exception:
        raw = ""
    return Path(raw).expanduser() if raw else None


def custom_model_path() -> Path | None:
    """Return the user-selected GGUF only when it still exists and is valid."""
    path = configured_custom_model_path()
    if path is None:
        return None
    try:
        if path.is_file() and path.suffix.casefold() == ".gguf":
            return path.resolve()
    except OSError:
        pass
    return None


def selected_model_id() -> str:
    """Stable cache/runtime identity for the active local model."""
    custom = configured_custom_model_path()
    if custom is not None:
        return "custom:" + str(custom)
    return selected_model_name()


def model_path(model_name: str | None = None) -> Path:
    custom = configured_custom_model_path()
    if custom is not None and model_name in (None, selected_model_name(), selected_model_id()):
        return custom
    selected = selected_model_name() if model_name is None else model_name
    spec = model_spec(selected)
    return _registered_model_path(spec, _load_manifest())


def _managed_model_path(spec: HyMt2ModelSpec) -> Path:
    return component_dir() / "model" / spec.filename


def runtime_dir() -> Path:
    return component_dir() / "runtime"


def runtime_executable() -> Path:
    return runtime_dir() / "llama-server.exe"


def manifest_path() -> Path:
    return component_dir() / "component.json"


def _runtime_has_backend(runner: str) -> bool:
    """Check the already unpacked runtime before scheduling a download."""
    if not runtime_executable().is_file():
        return False
    runtime = runtime_dir()
    if runner == "vulkan":
        return (runtime / "ggml-vulkan.dll").is_file()
    if runner.startswith("cuda"):
        return (runtime / "ggml-cuda.dll").is_file() and any(
            (runtime / name).is_file()
            for name in ("cudart64_12.dll", "cudart64_13.dll")
        )
    return runner == "cpu"


def _detect_installed_runner(manifest: dict) -> str:
    """Recover the runner from disk for installs made before runner metadata."""
    declared = str(manifest.get("runner") or "")
    if declared in RUNTIME_ASSETS and _runtime_has_backend(declared):
        return declared
    runtime = runtime_dir()
    if (runtime / "ggml-cuda.dll").is_file():
        if (runtime / "cudart64_13.dll").is_file():
            return "cuda-13.3"
        if (runtime / "cudart64_12.dll").is_file():
            return "cuda-12.4"
    if (runtime / "ggml-vulkan.dll").is_file():
        return "vulkan"
    return ""


def detect_graphics_adapters() -> list[dict[str, str]]:
    if os.name != "nt":
        return []
    command = (
        "Get-CimInstance Win32_VideoController | "
        "Select-Object Name,PNPDeviceID,DriverVersion | ConvertTo-Json -Compress"
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=creationflags,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        payload = json.loads(result.stdout.strip().lstrip("\ufeff"))
        rows = payload if isinstance(payload, list) else [payload]
        return [
            {
                "name": str(row.get("Name") or ""),
                "pnp_device_id": str(row.get("PNPDeviceID") or ""),
                "driver_version": str(row.get("DriverVersion") or ""),
            }
            for row in rows
            if isinstance(row, dict)
        ]
    except Exception:
        return []


def classify_graphics_adapters(adapters: list[dict[str, str]]) -> dict[str, str]:
    for adapter in adapters:
        pnp = str(adapter.get("pnp_device_id") or "").upper()
        name = str(adapter.get("name") or "")
        if "VEN_10DE" in pnp:
            runner = "cuda-13.3" if re.search(r"RTX\s*50\d{2}", name, re.I) else "cuda-12.4"
            return {"vendor": "nvidia", "runner": runner, "name": name or "NVIDIA GPU"}
    for adapter in adapters:
        pnp = str(adapter.get("pnp_device_id") or "").upper()
        name = str(adapter.get("name") or "")
        if "VEN_1002" in pnp:
            if "DEV_15BF" in pnp:
                name = "AMD Radeon 780M"
            elif "NVIDIA" in name.upper() or "INTEL" in name.upper():
                name = "AMD Radeon Graphics"
            return {"vendor": "amd", "runner": "vulkan", "name": name or "AMD GPU"}
        if "VEN_8086" in pnp:
            if "NVIDIA" in name.upper() or "AMD" in name.upper():
                name = "Intel Graphics"
            return {"vendor": "intel", "runner": "vulkan", "name": name or "Intel GPU"}
    return {"vendor": "cpu", "runner": "vulkan", "name": "CPU fallback"}


def detected_hardware() -> dict[str, str]:
    return classify_graphics_adapters(detect_graphics_adapters())


def _component_status_cache_key(model_name: str | None) -> tuple:
    configured = configured_custom_model_path()
    return (
        model_name,
        selected_model_name(),
        str(configured) if configured is not None else "",
        str(component_dir()),
        # Tests and embedders may replace hardware detection at runtime.
        id(detect_graphics_adapters),
    )


def component_status(model_name: str | None = None) -> dict:
    """Return component status quickly and avoid overlapping expensive scans."""
    with _component_status_lock:
        now = time.monotonic()
        key = _component_status_cache_key(model_name)
        cached = _component_status_cache.get(key)
        if cached and now - cached[0] < _COMPONENT_STATUS_TTL_SECONDS:
            return cached[1]
        status = _component_status_uncached(model_name)
        _component_status_cache[key] = (now, status)
        return status


def _component_status_uncached(model_name: str | None = None) -> dict:
    """Return selected-model status without hashing multi-gigabyte files.

    Files are hashed during download/repair. The persisted verified manifest
    keeps routine settings refreshes instant even when the 7B model is present.
    """
    selected = model_spec(selected_model_name() if model_name is None else model_name)
    hardware = detected_hardware()
    manifest = _load_manifest()
    executable = runtime_executable()
    installed_runner = _detect_installed_runner(manifest)
    configured_custom = configured_custom_model_path()
    custom = custom_model_path()
    if configured_custom is not None and model_name in (None, selected_model_name(), selected_model_id()):
        runtime_ready = _runtime_has_backend(installed_runner)
        hardware_matches = bool(installed_runner) and installed_runner == hardware["runner"]
        ready = custom is not None and runtime_ready and hardware_matches
        size = custom.stat().st_size if custom is not None else 0
        label = f"自定义 GGUF：{configured_custom.name}"
        message = (
            f"{label} 已就绪：{hardware['name']} / {installed_runner}"
            if ready
            else f"已选择本地模型：{configured_custom}；文件不存在或需要可用的 llama.cpp 运行器"
        )
        return {
            "ready": ready,
            "model_ready": custom is not None,
            "runtime_ready": runtime_ready,
            "hardware_matches": hardware_matches,
            "hardware": hardware,
            "runner": installed_runner,
            "expected_runner": hardware["runner"],
            "model": selected_model_id(),
            "model_label": label,
            "model_size": size,
            # A custom file is already explicit. Do not scan the filesystem
            # again for every built-in model while rendering this status.
            "models": [_model_status(spec, manifest, discover=False) for spec in model_choices()],
            "local_model_found": custom is not None,
            "local_model_path": str(configured_custom),
            "custom_model": True,
            "installed_bytes": size + _runtime_declared_bytes(manifest),
            "component_dir": str(component_dir()),
            "message": message,
            "version": str(manifest.get("runtime_version") or ""),
        }
    model_rows = [_model_status(spec, manifest) for spec in model_choices()]
    if not any(row["name"] == selected.name for row in model_rows):
        # Keep status queries for legacy installations readable even though
        # legacy base models are no longer offered as download choices.
        model_rows.append(_model_status(selected, manifest))
    selected_row = next(row for row in model_rows if row["name"] == selected.name)
    model_ready = bool(selected_row["ready"])
    runtime_ready = _runtime_has_backend(installed_runner)
    hardware_matches = bool(installed_runner) and installed_runner == hardware["runner"]
    ready = model_ready and runtime_ready and hardware_matches
    installed_bytes = _installed_component_bytes(model_rows, manifest)
    installed_models = [row["label"] for row in model_rows if row["ready"]]
    local_model_found = bool(selected_row.get("candidate_path"))
    if ready:
        message = f"{selected.label} 已就绪：{hardware['name']} / {installed_runner}"
    elif local_model_found:
        message = (
            f"已发现本地模型：{selected_row['candidate_path']}；"
            "点击“下载/修复当前模型”会校验并接入，不重复下载模型"
        )
    elif model_ready and runtime_ready and not hardware_matches:
        message = f"模型已下载，需要安装 {hardware['runner']} 运行器"
    elif model_ready:
        message = "模型已下载，运行器缺失"
    else:
        message = f"未安装（下载约 {_format_gib(selected.size)}）"
    if installed_models:
        message += "；已安装：" + "、".join(installed_models)
    return {
        "ready": ready,
        "model_ready": model_ready,
        "runtime_ready": runtime_ready,
        "hardware_matches": hardware_matches,
        "hardware": hardware,
        "runner": installed_runner,
        "expected_runner": hardware["runner"],
        "model": selected.name,
        "model_label": selected.label,
        "model_size": selected.size,
        "models": model_rows,
        "local_model_found": local_model_found,
        "local_model_path": str(selected_row.get("candidate_path") or ""),
        "installed_bytes": installed_bytes,
        "component_dir": str(component_dir()),
        "message": message,
        "version": str(manifest.get("runtime_version") or ""),
    }


def install_component(
    progress_callback: ProgressCallback | None = None,
    model_name: str | None = None,
    cancel_event=None,
) -> dict:
    _raise_if_cancelled(cancel_event)
    invalidate_component_status_cache()
    custom_selected = configured_custom_model_path() is not None
    if custom_selected and custom_model_path() is None:
        raise RuntimeError("已选择的自定义 GGUF 不存在或扩展名无效，请重新选择模型文件")
    selected = model_spec(selected_model_name() if model_name is None else model_name)
    root = component_dir()
    current = component_status(selected.name)
    info(
        f"[本地 GGUF] 已选择模型: {selected.name}; 文件={selected.filename}; "
        f"大小={_format_gib(selected.size)}"
    )
    if (
        current.get("ready")
        and current.get("version") == LLAMA_RELEASE
    ):
        info("[本地 GGUF] 模型与本地运行器已就绪，跳过重复下载")
        _emit(progress_callback, "done", f"{selected.label} 已是最新状态", 1, 1)
        return current
    downloads = root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    hardware = detected_hardware()
    runner = hardware["runner"]
    assets = RUNTIME_ASSETS[runner]
    info(
        f"[本地 GGUF] 自动检测硬件: {hardware['name']}; "
        f"选择运行器={runner}; 运行器包={len(assets)}"
    )
    model_ready = bool(current.get("model_ready"))
    candidate_value = str(current.get("local_model_path") or "")
    local_candidate = Path(candidate_value) if candidate_value else None
    runtime_ready = bool(
        current.get("runtime_ready")
        and current.get("hardware_matches")
        and current.get("runner") == runner
    )
    if runtime_ready:
        info(f"[本地 GGUF] 复用现有 {runner} 运行器，不重复下载")
    total_bytes = (0 if model_ready or local_candidate else selected.size) + (
        0 if runtime_ready else sum(asset.size for asset in assets)
    )
    total_bytes = max(1, total_bytes)
    completed_bytes = 0

    _emit(progress_callback, "prepare", f"准备 {selected.label}", 0, total_bytes)
    _raise_if_cancelled(cancel_event)
    model_download = downloads / selected.filename
    managed_model = _managed_model_path(selected)
    verified_model = custom_model_path() if custom_selected else (_registered_model_path(selected, manifest) if model_ready else None)
    downloaded_model = False
    if model_ready:
        info(f"[本地 GGUF] 已验证的模型文件存在，跳过下载: {verified_model}")
        _emit(progress_callback, "model_ready", f"{selected.label} 已存在，跳过下载", 0, total_bytes)
    elif local_candidate:
        _raise_if_cancelled(cancel_event)
        _emit(progress_callback, "verify_local_model", f"正在校验本地模型: {local_candidate.name}", 0, total_bytes)
        if _valid_file(local_candidate, selected.size, selected.sha256, cancel_event):
            model_ready = True
            verified_model = local_candidate.resolve()
            info(f"[本地 GGUF] 已接入校验通过的本地模型: {verified_model}")
            _emit(progress_callback, "model_ready", f"本地模型校验通过，已接入: {selected.label}", 0, total_bytes)
        else:
            info(f"[本地 GGUF] 本地模型校验失败，改为下载: {local_candidate}")
            total_bytes += selected.size
    if not model_ready:
        info(f"[本地 GGUF] 开始下载模型: {selected.source_url}")
        _download(
            selected.source_url,
            model_download,
            selected.size,
            selected.sha256,
            progress_callback,
            f"下载 {selected.label}",
            completed_bytes,
            total_bytes,
            cancel_event,
        )
        completed_bytes += selected.size
        downloaded_model = True

    runtime_archives: list[tuple[RuntimeAsset, Path]] = []
    if not runtime_ready:
        for asset in assets:
            _raise_if_cancelled(cancel_event)
            archive = downloads / asset.filename
            info(f"[本地 GGUF] 下载运行器资源: {asset.filename}")
            _download(
                asset.url,
                archive,
                asset.size,
                asset.sha256,
                progress_callback,
                f"下载 {runner} 运行器",
                completed_bytes,
                total_bytes,
                cancel_event,
            )
            completed_bytes += asset.size
            runtime_archives.append((asset, archive))

    staging = root / f"runtime.staging.{os.getpid()}"
    backup = root / "runtime.backup"
    try:
        _raise_if_cancelled(cancel_event)
        if downloaded_model:
            managed_model.parent.mkdir(parents=True, exist_ok=True)
            model_download.replace(managed_model)
            verified_model = managed_model
            info(f"[本地 GGUF] 模型校验完成并已安装: {managed_model}")
            _emit(
                progress_callback,
                "model_installed",
                f"模型校验并安装完成: {selected.filename}",
                completed_bytes,
                total_bytes,
            )
        if runtime_archives:
            info(f"[本地 GGUF] 正在解压并切换 {runner} 运行器")
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            for _asset, archive in runtime_archives:
                _safe_extract_zip(archive, staging, cancel_event)
            if not (staging / "llama-server.exe").is_file():
                raise RuntimeError("llama.cpp 运行包缺少 llama-server.exe")
            if runner == "vulkan" and not (staging / "ggml-vulkan.dll").is_file():
                raise RuntimeError("Vulkan 运行包缺少 ggml-vulkan.dll")
            if runner.startswith("cuda") and not (staging / "ggml-cuda.dll").is_file():
                raise RuntimeError("CUDA 运行包缺少 ggml-cuda.dll")
            shutil.rmtree(backup, ignore_errors=True)
            if runtime_dir().exists():
                runtime_dir().replace(backup)
            staging.replace(runtime_dir())
            shutil.rmtree(backup, ignore_errors=True)
            info(f"[本地 GGUF] 运行器已就绪: {runtime_executable()}")
            _emit(
                progress_callback,
                "runtime_ready",
                f"{runner} 运行器已配置完成",
                completed_bytes,
                total_bytes,
            )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if backup.exists() and not runtime_dir().exists():
            backup.replace(runtime_dir())
        raise

    _raise_if_cancelled(cancel_event)
    _download_license(selected.license_url, root / "licenses" / f"{selected.name}-LICENSE.txt", cancel_event)
    _download_license(LLAMA_LICENSE_URL, root / "licenses" / "llama.cpp-LICENSE.txt", cancel_event)
    manifest = _load_manifest()
    models = _manifest_models(manifest)
    if not custom_selected:
        model_entry = {
            "sha256": selected.sha256,
            "size": selected.size,
            "installed_at": int(time.time()),
        }
        if verified_model and verified_model.resolve() != managed_model.resolve():
            model_entry["path"] = str(verified_model)
        models[selected.name] = model_entry
    manifest.update({
        "models": models,
        "runner": runner,
        "runtime_version": LLAMA_RELEASE,
        "runtime_assets": [asset.filename for asset in assets],
        "hardware": hardware,
        "source": "EngAixt GGUF + ggml-org/llama.cpp",
    })
    # Preserve the legacy fields so versions before multi-model support can
    # still recognize an existing 1.8B installation after an app downgrade.
    if selected.name == DEFAULT_MODEL_NAME and not custom_selected:
        manifest.update({
            "model": selected.name,
            "model_sha256": selected.sha256,
            "model_size": selected.size,
        })
    _raise_if_cancelled(cancel_event)
    manifest_path().write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    invalidate_component_status_cache()
    info("[本地 GGUF] 已写入本地组件清单，准备自动启动验证")
    _emit(
        progress_callback,
        "manifest_ready",
        "本地模型配置已保存，准备自动启动验证",
        completed_bytes,
        total_bytes,
    )
    for _asset, archive in runtime_archives:
        archive.unlink(missing_ok=True)
    try:
        downloads.rmdir()
    except OSError:
        pass
    _emit(progress_callback, "done", f"{selected.label} 安装完成", total_bytes, total_bytes)
    return component_status(selected.name)


def remove_component(model_name: str | None = None) -> dict:
    invalidate_component_status_cache()
    selected = model_spec(selected_model_name() if model_name is None else model_name)
    try:
        from translators.hy_mt2_runtime import shutdown_runtime

        shutdown_runtime()
    except Exception:
        pass
    # An adopted external model is only registered here, never owned by EngAixt.
    _managed_model_path(selected).unlink(missing_ok=True)
    manifest = _load_manifest()
    models = _manifest_models(manifest)
    models.pop(selected.name, None)
    if models:
        manifest["models"] = models
        if manifest.get("model") == selected.name:
            manifest.pop("model", None)
            manifest.pop("model_sha256", None)
            manifest.pop("model_size", None)
        manifest_path().write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        shutil.rmtree(component_dir(), ignore_errors=True)
    invalidate_component_status_cache()
    return component_status(selected.name)


def _response_resumed_from(response: Any, offset: int) -> bool:
    """判断响应是否真的从请求的偏移量开始返回。

    标准做法是 HTTP 206，但部分 CDN / 对象存储（例如 ModelScope）会返回 200
    同时给出正确的 Content-Range，并只发送请求的那一段。只看 206 会把这种
    情况误判为"服务器不支持续传"，于是丢弃已下载的分片从头再来。

    反之，如果服务器忽略了 Range 并回传整个文件，Content-Range 会缺失或与
    请求的偏移量不符 —— 这时必须重下，否则会把两份数据拼成损坏的文件。
    """
    content_range = str(response.headers.get("Content-Range") or "").strip()
    if content_range:
        return content_range.startswith(f"bytes {offset}-")
    return getattr(response, "status", 200) == 206


def _download(
    url: str,
    destination: Path,
    expected_size: int,
    expected_sha256: str,
    callback: ProgressCallback | None,
    label: str,
    completed_before: int,
    total_bytes: int,
    cancel_event=None,
) -> None:
    _raise_if_cancelled(cancel_event)
    if _valid_file(destination, expected_size, expected_sha256, cancel_event):
        info(f"[本地 GGUF] 已复用校验通过的下载文件: {destination.name}")
        _emit(callback, "asset_ready", f"{label}：已存在", completed_before + expected_size, total_bytes)
        return
    part = destination.with_suffix(destination.suffix + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    offset = part.stat().st_size if part.exists() else 0
    if offset > expected_size:
        part.unlink()
        offset = 0
    if offset:
        info(f"[本地 GGUF] 续传 {label}: 已有 {offset / 1024 / 1024:.0f} MB")
    headers = {"User-Agent": "EngAixt-HyMT2/1.0"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    try:
        # A short socket timeout makes a cancellation responsive even when the
        # CDN has stopped sending bytes. The loop keeps waiting for ordinary
        # transient read timeouts while the user has not cancelled.
        with urllib.request.urlopen(request, timeout=8) as response:
            if offset and not _response_resumed_from(response, offset):
                # 服务器没有从请求的偏移量开始返回，已有分片不可信，必须重下
                offset = 0
                part.unlink(missing_ok=True)
            mode = "ab" if offset else "wb"
            downloaded = offset
            last_emit = 0.0
            with open(part, mode) as output:
                while True:
                    _raise_if_cancelled(cancel_event)
                    try:
                        chunk = response.read(1024 * 1024)
                    except (TimeoutError, socket.timeout):
                        _raise_if_cancelled(cancel_event)
                        continue
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if now - last_emit >= 0.4:
                        _emit(callback, "download", label, completed_before + downloaded, total_bytes)
                        last_emit = now
    except ComponentInstallCancelled:
        # A cancelled install should not resume an abandoned multi-GB partial
        # file on the next click, and must never publish it as an asset.
        part.unlink(missing_ok=True)
        raise
    try:
        _raise_if_cancelled(cancel_event)
        if not _valid_file(part, expected_size, expected_sha256, cancel_event):
            raise RuntimeError(f"{label} 校验失败，请重试")
    except ComponentInstallCancelled:
        part.unlink(missing_ok=True)
        raise
    part.replace(destination)
    info(f"[本地 GGUF] 下载和 SHA-256 校验通过: {destination.name}")
    _emit(callback, "download_done", f"{label}完成", completed_before + expected_size, total_bytes)


def _valid_file(path: Path, expected_size: int, expected_sha256: str, cancel_event=None) -> bool:
    if not path.is_file() or path.stat().st_size != expected_size:
        return False
    return _sha256(path, cancel_event) == expected_sha256.lower()


def _sha256(path: Path, cancel_event=None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            _raise_if_cancelled(cancel_event)
            chunk = stream.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().lower()


def _safe_extract_zip(archive: Path, destination: Path, cancel_event=None) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            _raise_if_cancelled(cancel_event)
            target = (destination / member.filename).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise RuntimeError(f"运行包包含不安全路径: {member.filename}") from exc
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(member) as source, open(target, "wb") as output:
                while True:
                    _raise_if_cancelled(cancel_event)
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)


def _download_license(url: str, destination: Path, cancel_event=None) -> None:
    try:
        _raise_if_cancelled(cancel_event)
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": "EngAixt-HyMT2/1.0"})
        with urllib.request.urlopen(request, timeout=8) as response:
            destination.write_bytes(response.read())
        _raise_if_cancelled(cancel_event)
    except ComponentInstallCancelled:
        destination.unlink(missing_ok=True)
        raise
    except Exception:
        pass


def _emit(callback: ProgressCallback | None, phase: str, message: str, current: int, total: int) -> None:
    if not callback:
        return
    percent = round(current / max(1, total) * 100, 2)
    try:
        callback({
            "phase": phase,
            "message": message,
            "current_bytes": int(current),
            "total_bytes": int(total),
            "percent": percent,
        })
    except Exception:
        pass


def _load_manifest() -> dict:
    try:
        return json.loads(manifest_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _manifest_models(manifest: dict) -> dict[str, dict]:
    rows = manifest.get("models")
    if isinstance(rows, dict):
        return {str(name): value for name, value in rows.items() if isinstance(value, dict)}
    # Installations made by earlier versions carried only the selected 1.8B
    # model in top-level fields. Treat it as an installed verified model.
    if (
        str(manifest.get("model") or "") == DEFAULT_MODEL_NAME
        and str(manifest.get("model_sha256") or "").lower() == model_spec().sha256
    ):
        return {
            DEFAULT_MODEL_NAME: {
                "sha256": model_spec().sha256,
                "size": model_spec().size,
            }
        }
    return {}


def _registered_model_path(spec: HyMt2ModelSpec, manifest: dict) -> Path:
    entry = _manifest_models(manifest).get(spec.name, {})
    registered = str(entry.get("path") or "").strip()
    if registered:
        candidate = Path(registered)
        if _matches_model_size(candidate, spec):
            return candidate
    return _managed_model_path(spec)


def _model_status(spec: HyMt2ModelSpec, manifest: dict, *, discover: bool = True) -> dict:
    entry = _manifest_models(manifest).get(spec.name, {})
    path = _registered_model_path(spec, manifest)
    ready = (
        _matches_model_size(path, spec)
        and str(entry.get("sha256") or "").lower() == spec.sha256
    )
    candidate = None if ready or not discover else _discover_local_model(spec)
    return {
        "name": spec.name,
        "label": spec.label,
        "description": spec.description,
        "size": spec.size,
        "ready": ready,
        "path": str(path) if ready else "",
        "candidate_path": str(candidate) if candidate else "",
    }


def _matches_model_size(path: Path, spec: HyMt2ModelSpec) -> bool:
    try:
        return path.is_file() and path.stat().st_size == spec.size
    except OSError:
        return False


def _discover_local_model(spec: HyMt2ModelSpec) -> Path | None:
    now = time.monotonic()
    cached = _local_model_discovery_cache.get(spec.name)
    if cached and now - cached[0] < _LOCAL_MODEL_DISCOVERY_TTL_SECONDS:
        return cached[1]

    found = None
    for root in _local_model_search_roots():
        for directory, child_dirs, filenames in os.walk(root, onerror=lambda _error: None):
            relative_depth = len(Path(directory).relative_to(root).parts)
            if relative_depth >= 3:
                child_dirs.clear()
            else:
                child_dirs[:] = [
                    name
                    for name in child_dirs
                    if name.lower() not in {"$recycle.bin", "system volume information", "node_modules"}
                ]
            if spec.filename not in filenames:
                continue
            candidate = Path(directory) / spec.filename
            if _matches_model_size(candidate, spec):
                found = candidate.resolve()
                break
        if found:
            break
    _local_model_discovery_cache[spec.name] = (now, found)
    return found


def _local_model_search_roots() -> tuple[Path, ...]:
    home = Path.home()
    roots = [
        component_dir() / "model",
        home / "Downloads" / "models",
        home / "Downloads" / "AI",
        home / "Documents" / "models",
        home / "Documents" / "AI",
        home / "Desktop" / "models",
    ]
    # Never walk every mounted drive from a settings refresh. Network drives
    # and large game/data volumes can make that synchronous scan appear stuck
    # forever. Users can select any GGUF directly; these roots are only a
    # small compatibility fallback for older managed-model installs.
    configured_root = os.environ.get("ENGAIXT_MODEL_SEARCH_ROOT", "").strip()
    if configured_root:
        roots.append(Path(configured_root).expanduser())

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        normalized = str(resolved).casefold()
        if normalized in seen or not resolved.is_dir():
            continue
        seen.add(normalized)
        unique.append(resolved)
    return tuple(unique)


def _format_gib(size: int) -> str:
    return f"{int(size) / 1024 / 1024 / 1024:.1f} GB"


def _runtime_declared_bytes(manifest: dict) -> int:
    """Return the declared runtime download size without recursively walking it."""
    names = {str(name) for name in (manifest.get("runtime_assets") or [])}
    return sum(
        asset.size
        for assets in RUNTIME_ASSETS.values()
        for asset in assets
        if asset.filename in names
    )


def _installed_component_bytes(model_rows: list[dict], manifest: dict) -> int:
    model_bytes = sum(int(row.get("size") or 0) for row in model_rows if row.get("ready"))
    return model_bytes + _runtime_declared_bytes(manifest)
