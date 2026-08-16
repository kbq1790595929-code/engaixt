from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


MODEL_NAME = "Hy-MT2-1.8B-Q4_K_M"
MODEL_FILENAME = f"{MODEL_NAME}.gguf"
MODEL_SIZE = 1_133_080_448
MODEL_SHA256 = "dc5f44fcf1fa496ee7ad725982c0c8c553a4de00259b53af84c4b89fb0c06699"
MODEL_URL = (
    "https://www.modelscope.cn/models/Tencent-Hunyuan/Hy-MT2-1.8B-GGUF/"
    f"resolve/master/{MODEL_FILENAME}"
)
MODEL_LICENSE_URL = (
    "https://www.modelscope.cn/models/Tencent-Hunyuan/Hy-MT2-1.8B-GGUF/"
    "resolve/master/LICENSE.txt"
)
LLAMA_RELEASE = "b10085"
LLAMA_LICENSE_URL = f"https://raw.githubusercontent.com/ggml-org/llama.cpp/{LLAMA_RELEASE}/LICENSE"

ProgressCallback = Callable[[dict], None]


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


def model_path() -> Path:
    return component_dir() / "model" / MODEL_FILENAME


def runtime_dir() -> Path:
    return component_dir() / "runtime"


def runtime_executable() -> Path:
    return runtime_dir() / "llama-server.exe"


def manifest_path() -> Path:
    return component_dir() / "component.json"


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


def component_status() -> dict:
    hardware = detected_hardware()
    manifest = _load_manifest()
    model = model_path()
    executable = runtime_executable()
    installed_runner = str(manifest.get("runner") or "")
    model_ready = (
        model.is_file()
        and model.stat().st_size == MODEL_SIZE
        and str(manifest.get("model_sha256") or "").lower() == MODEL_SHA256
    )
    runtime_ready = executable.is_file() and executable.stat().st_size > 0
    hardware_matches = installed_runner == hardware["runner"]
    ready = model_ready and runtime_ready and hardware_matches
    installed_bytes = _directory_size(component_dir()) if component_dir().exists() else 0
    if ready:
        message = f"已就绪：{hardware['name']} / {installed_runner}"
    elif model_ready and runtime_ready and not hardware_matches:
        message = f"检测到设备变化，需要安装 {hardware['runner']} 运行器"
    elif model_ready:
        message = "模型已下载，运行器缺失"
    else:
        message = "未安装（首次下载约 1.2-1.8 GB）"
    return {
        "ready": ready,
        "model_ready": model_ready,
        "runtime_ready": runtime_ready,
        "hardware_matches": hardware_matches,
        "hardware": hardware,
        "runner": installed_runner,
        "expected_runner": hardware["runner"],
        "model": MODEL_NAME,
        "model_size": MODEL_SIZE,
        "installed_bytes": installed_bytes,
        "component_dir": str(component_dir()),
        "message": message,
        "version": str(manifest.get("runtime_version") or ""),
    }


def install_component(progress_callback: ProgressCallback | None = None) -> dict:
    root = component_dir()
    current = component_status()
    if (
        current.get("ready")
        and current.get("version") == LLAMA_RELEASE
        and _valid_file(model_path(), MODEL_SIZE, MODEL_SHA256)
    ):
        _emit(progress_callback, "done", "Hy-MT2 离线组件已是最新状态", 1, 1)
        return current
    downloads = root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    hardware = detected_hardware()
    runner = hardware["runner"]
    assets = RUNTIME_ASSETS[runner]
    total_bytes = MODEL_SIZE + sum(asset.size for asset in assets)
    completed_bytes = 0

    _emit(progress_callback, "prepare", "准备 Hy-MT2 离线组件", 0, total_bytes)
    model_download = downloads / MODEL_FILENAME
    existing_model = model_path()
    if _valid_file(existing_model, MODEL_SIZE, MODEL_SHA256):
        completed_bytes += MODEL_SIZE
        _emit(progress_callback, "model_ready", "Hy-MT2 模型已存在，跳过下载", completed_bytes, total_bytes)
    else:
        _download(
            MODEL_URL,
            model_download,
            MODEL_SIZE,
            MODEL_SHA256,
            progress_callback,
            "下载 Hy-MT2 模型",
            completed_bytes,
            total_bytes,
        )
        completed_bytes += MODEL_SIZE

    runtime_archives: list[tuple[RuntimeAsset, Path]] = []
    for asset in assets:
        archive = downloads / asset.filename
        _download(
            asset.url,
            archive,
            asset.size,
            asset.sha256,
            progress_callback,
            f"下载 {runner} 运行器",
            completed_bytes,
            total_bytes,
        )
        completed_bytes += asset.size
        runtime_archives.append((asset, archive))

    staging = root / f"runtime.staging.{os.getpid()}"
    backup = root / "runtime.backup"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for _asset, archive in runtime_archives:
            _safe_extract_zip(archive, staging)
        if not (staging / "llama-server.exe").is_file():
            raise RuntimeError("llama.cpp 运行包缺少 llama-server.exe")
        if runner == "vulkan" and not (staging / "ggml-vulkan.dll").is_file():
            raise RuntimeError("Vulkan 运行包缺少 ggml-vulkan.dll")
        if runner.startswith("cuda") and not (staging / "ggml-cuda.dll").is_file():
            raise RuntimeError("CUDA 运行包缺少 ggml-cuda.dll")

        model_path().parent.mkdir(parents=True, exist_ok=True)
        if not _valid_file(existing_model, MODEL_SIZE, MODEL_SHA256):
            model_download.replace(existing_model)

        shutil.rmtree(backup, ignore_errors=True)
        if runtime_dir().exists():
            runtime_dir().replace(backup)
        staging.replace(runtime_dir())
        shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if backup.exists() and not runtime_dir().exists():
            backup.replace(runtime_dir())
        raise

    _download_license(MODEL_LICENSE_URL, root / "licenses" / "Hy-MT2-LICENSE.txt")
    _download_license(LLAMA_LICENSE_URL, root / "licenses" / "llama.cpp-LICENSE.txt")
    manifest = {
        "model": MODEL_NAME,
        "model_sha256": MODEL_SHA256,
        "model_size": MODEL_SIZE,
        "runner": runner,
        "runtime_version": LLAMA_RELEASE,
        "runtime_assets": [asset.filename for asset in assets],
        "hardware": hardware,
        "installed_at": int(time.time()),
        "source": "Tencent-Hunyuan/Hy-MT2 + ggml-org/llama.cpp",
    }
    manifest_path().write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for _asset, archive in runtime_archives:
        archive.unlink(missing_ok=True)
    try:
        downloads.rmdir()
    except OSError:
        pass
    _emit(progress_callback, "done", "Hy-MT2 离线组件安装完成", total_bytes, total_bytes)
    return component_status()


def remove_component() -> dict:
    try:
        from translators.hy_mt2_runtime import shutdown_runtime

        shutdown_runtime()
    except Exception:
        pass
    shutil.rmtree(component_dir(), ignore_errors=True)
    return component_status()


def _download(
    url: str,
    destination: Path,
    expected_size: int,
    expected_sha256: str,
    callback: ProgressCallback | None,
    label: str,
    completed_before: int,
    total_bytes: int,
) -> None:
    if _valid_file(destination, expected_size, expected_sha256):
        _emit(callback, "asset_ready", f"{label}：已存在", completed_before + expected_size, total_bytes)
        return
    part = destination.with_suffix(destination.suffix + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    offset = part.stat().st_size if part.exists() else 0
    if offset > expected_size:
        part.unlink()
        offset = 0
    headers = {"User-Agent": "EngAixt-HyMT2/1.0"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        if offset and getattr(response, "status", 200) != 206:
            offset = 0
            part.unlink(missing_ok=True)
        mode = "ab" if offset else "wb"
        downloaded = offset
        last_emit = 0.0
        with open(part, mode) as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                if now - last_emit >= 0.4:
                    _emit(callback, "download", label, completed_before + downloaded, total_bytes)
                    last_emit = now
    if not _valid_file(part, expected_size, expected_sha256):
        raise RuntimeError(f"{label} 校验失败，请重试")
    part.replace(destination)
    _emit(callback, "download_done", f"{label}完成", completed_before + expected_size, total_bytes)


def _valid_file(path: Path, expected_size: int, expected_sha256: str) -> bool:
    if not path.is_file() or path.stat().st_size != expected_size:
        return False
    return _sha256(path) == expected_sha256.lower()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            target = (destination / member.filename).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise RuntimeError(f"运行包包含不安全路径: {member.filename}") from exc
        package.extractall(destination)


def _download_license(url: str, destination: Path) -> None:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": "EngAixt-HyMT2/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            destination.write_bytes(response.read())
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


def _directory_size(path: Path) -> int:
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                total += child.stat().st_size
    except OSError:
        pass
    return total
