from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from core.resources import app_root


APP_NAME = "EngAixt"
APP_VERSION = "1.1.11"
UPDATE_BASE_URL = "https://engaixt.com/updates"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"
HTTP_RETRIES = 4
HTTP_RETRY_BASE_DELAY = 1.0
HTTP_READ_CHUNK_SIZE = 256 * 1024
CURL_SPEED_TIME = 30
CURL_SPEED_LIMIT = 1024
MIRROR_BASE_URLS: list[str] = []
PENDING_UPDATE_TTL_SECONDS = 30 * 60


ProgressCallback = Callable[[dict[str, Any]], None]

_predownload_lock = threading.Lock()
_predownload_state: dict[str, Any] = {}


class AppUpdateError(RuntimeError):
    pass


def current_app_info() -> dict[str, Any]:
    edition = _current_update_edition()
    version = _current_app_version()
    return {
        "app": APP_NAME,
        "version": version,
        "edition": edition,
        "can_apply_update": _is_packaged_app(),
        "install_dir": str(_install_dir()),
    }


def check_update(*, timeout: int = 45) -> dict[str, Any]:
    info = current_app_info()
    current_version = str(info.get("version") or APP_VERSION)
    url = _update_manifest_url(info["edition"])
    try:
        manifest = _read_json_url(url, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _no_update_package_info(info, url, current_version)
        raise

    latest = str(manifest.get("version") or "").strip()
    available = bool(latest and is_newer_version(latest, current_version))
    return {
        **info,
        "manifest_url": url,
        "update_available": available,
        "latest_version": latest or current_version,
        "notes": manifest.get("notes") or [],
        "published_at": manifest.get("published_at", ""),
        "package_size": manifest.get("size") or manifest.get("package_size") or 0,
        "sha256": manifest.get("sha256", ""),
        "manifest": manifest if available else {},
        "message": "Update available" if available else "Already up to date",
    }


def _no_update_package_info(info: dict[str, Any], url: str, current_version: str) -> dict[str, Any]:
    return {
        **info,
        "manifest_url": url,
        "update_available": False,
        "latest_version": current_version,
        "manifest": {},
        "message": "No online update package available",
        "manual_update_required": True,
    }


def download_update_package(
    manifest: dict[str, Any],
    *,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    package_manifest_url = str(
        manifest.get("package_manifest_url")
        or manifest.get("url")
        or manifest.get("download_manifest_url")
        or ""
    ).strip()
    if not package_manifest_url:
        raise AppUpdateError("update manifest missing package_manifest_url")

    mirror_bases = list(manifest.get("mirror_base_urls") or MIRROR_BASE_URLS)

    _emit(progress_callback, {
        "phase": "package_manifest",
        "downloaded": 0,
        "size": 0,
        "message": "Reading update package manifest",
    })
    package_manifest = _read_json_url(package_manifest_url, timeout=60, mirror_bases=mirror_bases)
    filename = str(package_manifest.get("filename") or manifest.get("filename") or "EngAixt-update.zip")
    expected_sha = str(package_manifest.get("sha256") or manifest.get("sha256") or "").lower().strip()
    expected_size = int(package_manifest.get("size") or manifest.get("size") or 0)
    parts = list(package_manifest.get("parts") or [])
    if not parts:
        raise AppUpdateError("update package manifest has no parts")

    updates_dir = _updates_dir()
    updates_dir.mkdir(parents=True, exist_ok=True)
    version = _safe_name(str(manifest.get("version") or "latest"))
    dest = updates_dir / f"{Path(filename).stem}-{version}.zip"
    if _verify_existing_update_package(dest, expected_size, expected_sha):
        _emit(progress_callback, {
            "phase": "download_done",
            "downloaded": expected_size or dest.stat().st_size,
            "size": expected_size,
            "message": "Update package already downloaded",
        })
        return dest

    tmp = _make_update_temp_path(dest)

    total_done = 0
    sha = hashlib.sha256()
    try:
        with tmp.open("wb") as out:
            for index, part in enumerate(parts, start=1):
                part_url = _resolve_package_part_url(package_manifest_url, str(part.get("path") or ""))
                part_mirror_urls = _build_mirror_urls(part_url, mirror_bases)[1:]
                part_tmp = _make_update_part_temp_path(tmp, index)
                _download_part_to_path(
                    part_url,
                    part_tmp,
                    timeout=600,
                    progress_callback=progress_callback,
                    base_downloaded=total_done,
                    total_size=expected_size,
                    index=index,
                    total=len(parts),
                    mirror_urls=part_mirror_urls,
                )

                actual_part_size, actual_part_sha = _append_part_file(part_tmp, out, sha)
                _unlink_quiet(part_tmp)

                expected_part_size = int(part.get("size") or 0)
                if expected_part_size and actual_part_size != expected_part_size:
                    raise AppUpdateError(f"update package part size mismatch: {part.get('path')}")

                expected_part_sha = str(part.get("sha256") or "").lower().strip()
                if expected_part_sha and actual_part_sha != expected_part_sha:
                    raise AppUpdateError(f"update package part sha256 mismatch: {part.get('path')}")

                total_done += actual_part_size
                _emit(progress_callback, {
                    "phase": "download",
                    "index": index,
                    "total": len(parts),
                    "downloaded": total_done,
                    "size": expected_size,
                    "message": f"Downloading update package {index}/{len(parts)}",
                })

        if expected_size and total_done != expected_size:
            raise AppUpdateError("update package size verification failed")

        actual_sha = sha.hexdigest()
        if expected_sha and actual_sha != expected_sha:
            raise AppUpdateError("update package sha256 verification failed")

        if _verify_existing_update_package(dest, expected_size, expected_sha):
            _unlink_quiet(tmp)
            _emit(progress_callback, {
                "phase": "download_done",
                "downloaded": expected_size or dest.stat().st_size,
                "size": expected_size,
                "message": "Update package already downloaded",
            })
            return dest
        try:
            tmp.replace(dest)
        except OSError as exc:
            raise AppUpdateError(
                "无法写入更新包，可能是上一轮更新或安全软件仍在占用文件。"
                "请稍等几秒后重试，或关闭正在运行的 EngAixt 后再更新。"
            ) from exc
    except Exception:
        _unlink_quiet(tmp)
        raise
    _emit(progress_callback, {
        "phase": "download_done",
        "downloaded": total_done,
        "size": expected_size,
        "message": "Update package downloaded",
    })
    return dest


def start_update_and_exit(
    zip_path: str | Path,
    manifest: dict[str, Any],
    *,
    restart_after_update: bool = False,
) -> dict[str, Any]:
    if not _is_packaged_app():
        raise AppUpdateError("source mode cannot apply in-place updates; use a packaged build")

    zip_path = Path(zip_path).resolve()
    if not zip_path.exists():
        raise AppUpdateError(f"update package not found: {zip_path}")

    install_dir = _install_dir()
    exe_path = Path(sys.executable).resolve()
    if not _is_within(exe_path, install_dir):
        raise AppUpdateError("current executable is outside the install directory")

    script_path = _write_update_script(
        zip_path,
        install_dir,
        exe_path.name,
        manifest,
        restart_after_update=restart_after_update,
    )
    launcher_path = _write_update_cmd_launcher(script_path, os.getpid())
    pending_path = _write_pending_update_marker(
        zip_path,
        install_dir,
        exe_path.name,
        manifest,
        script_path,
        launcher_path,
        restart_after_update=restart_after_update,
    )
    _append_update_launch_log(f"launch requested script={script_path} cmd={launcher_path} pid={os.getpid()}")
    command = ["cmd.exe", "/d", "/c", str(launcher_path)]
    flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    ) if os.name == "nt" else 0
    subprocess.Popen(
        command,
        cwd=str(launcher_path.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )
    return {
        "ok": True,
        "message": "Updater started; reopen EngAixt manually after it closes",
        "script": str(script_path),
        "launcher": str(launcher_path),
        "pending": str(pending_path),
        "restart_after_update": restart_after_update,
    }


def exit_if_update_pending_at_startup() -> bool:
    """Return True when this old process should exit because an update is applying.

    This closes the race where a user manually reopens EngAixt right after the
    updater closes the app but before the background PowerShell copy has
    replaced the executable.
    """
    pending = pending_update_status_for_startup()
    if not pending:
        return False
    _append_update_launch_log(
        "startup blocked by pending update "
        f"current={pending.get('current_version')} target={pending.get('target_version')}"
    )
    _launch_pending_update(pending)
    _show_pending_update_message(pending)
    return True


def pending_update_status_for_startup(*, now: float | None = None) -> dict[str, Any] | None:
    if os.environ.get("ENGAIXT_IGNORE_PENDING_UPDATE"):
        return None
    if not _is_packaged_app():
        return None

    marker = _read_pending_update_marker()
    if not marker:
        return None

    target_version = _normalize_version(str(marker.get("target_version") or marker.get("version") or ""))
    current_version = _current_app_version()
    if not target_version or not is_newer_version(target_version, current_version):
        _clear_pending_update_marker()
        return None

    now = time.time() if now is None else float(now)
    try:
        created_at = float(marker.get("created_at") or 0)
    except (TypeError, ValueError):
        created_at = 0
    if created_at and now - created_at > PENDING_UPDATE_TTL_SECONDS:
        _append_update_launch_log(
            "stale pending update marker cleared "
            f"current={current_version} target={target_version} age={now - created_at:.1f}s"
        )
        _clear_pending_update_marker()
        return None

    return {
        **marker,
        "current_version": current_version,
        "target_version": target_version,
    }


def is_newer_version(latest: str, current: str) -> bool:
    return _version_tuple(latest) > _version_tuple(current)


def _current_update_edition() -> str:
    try:
        from core.trial_quota import current_edition

        edition = str(current_edition() or "trial").strip().lower()
    except Exception:
        edition = "trial"
    if edition in {"pro", "unlimited"}:
        return "unlimited"
    return "trial"


def _update_manifest_url(edition: str) -> str:
    # All editions share the same binaries. Membership is stored separately as
    # a signed local license and survives in-place updates.
    return f"{UPDATE_BASE_URL}/trial/latest.json"


def _user_agent() -> str:
    return f"{APP_NAME}/{_current_app_version()}"


def _current_app_version() -> str:
    env_version = _normalize_version(os.environ.get("ENGAIXT_APP_VERSION", ""))
    if env_version:
        return env_version
    for path in _app_version_marker_paths():
        version = _read_app_version_marker(path)
        if version:
            return version
    return APP_VERSION


def _app_version_marker_paths() -> list[Path]:
    candidates = [
        app_root() / "app_version.json",
        app_root() / "version.json",
        _install_dir() / "app_version.json",
        _install_dir() / "version.json",
        _install_dir() / "_internal" / "app_version.json",
        _install_dir() / "_internal" / "version.json",
    ]
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _read_app_version_marker(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8-sig").strip()
        if not text:
            return ""
        if path.suffix.lower() == ".json":
            data = json.loads(text)
            if isinstance(data, dict):
                return _normalize_version(str(data.get("version") or data.get("app_version") or ""))
        return _normalize_version(text.splitlines()[0])
    except Exception:
        return ""


def _normalize_version(value: str) -> str:
    text = str(value or "").strip().lstrip("vV")
    parts = text.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return ""
    return ".".join(parts)


def _is_packaged_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def _install_dir() -> Path:
    if _is_packaged_app():
        return Path(sys.executable).resolve().parent
    return app_root()


def _updates_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(local) / APP_NAME / "updates"


def _pending_update_path() -> Path:
    return _updates_dir() / "pending-update.json"


def _make_update_temp_path(dest: Path) -> Path:
    stamp = f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}"
    return dest.with_name(f"{dest.name}.{stamp}.part")


def _make_update_part_temp_path(tmp: Path, index: int) -> Path:
    return tmp.with_name(f"{tmp.name}.part-{index:03d}")


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _verify_existing_update_package(path: Path, expected_size: int, expected_sha: str) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        if expected_size and path.stat().st_size != expected_size:
            return False
        if expected_sha and _sha256_file(path) != expected_sha:
            return False
        return True
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(HTTP_READ_CHUNK_SIZE)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def _write_pending_update_marker(
    zip_path: Path,
    install_dir: Path,
    exe_name: str,
    manifest: dict[str, Any],
    script_path: Path,
    launcher_path: Path,
    restart_after_update: bool = False,
) -> Path:
    target_version = str(manifest.get("version") or "").strip()
    marker = {
        "schema": 1,
        "app": APP_NAME,
        "source_version": _current_app_version(),
        "target_version": target_version,
        "created_at": time.time(),
        "zip_path": str(zip_path),
        "install_dir": str(install_dir),
        "exe_name": exe_name,
        "script_path": str(script_path),
        "launcher_path": str(launcher_path),
        "restart_after_update": bool(restart_after_update),
    }
    path = _pending_update_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _read_pending_update_marker() -> dict[str, Any] | None:
    path = _pending_update_path()
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        _append_update_launch_log(f"pending update marker unreadable: {exc}")
        _clear_pending_update_marker()
        return None


def _clear_pending_update_marker() -> None:
    try:
        _pending_update_path().unlink(missing_ok=True)
    except Exception as exc:
        _append_update_launch_log(f"pending update marker cleanup failed: {exc}")


def _launch_pending_update(pending: dict[str, Any]) -> None:
    try:
        script_path = Path(str(pending.get("script_path") or ""))
        launcher_path = Path(str(pending.get("launcher_path") or ""))
        if script_path.is_file():
            launcher_path = _write_update_cmd_launcher(script_path, os.getpid())
        elif not launcher_path.is_file():
            _append_update_launch_log("pending update launcher missing")
            return

        command = ["cmd.exe", "/d", "/c", str(launcher_path)]
        flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        ) if os.name == "nt" else 0
        subprocess.Popen(
            command,
            cwd=str(launcher_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
        _append_update_launch_log(f"pending update launcher started: {launcher_path}")
    except Exception as exc:
        _append_update_launch_log(f"pending update launcher start failed: {exc}")


def _show_pending_update_message(pending: dict[str, Any]) -> None:
    target = str(pending.get("target_version") or "").strip()
    message = (
        f"EngAixt 正在更新到 {target}。\n\n"
        "请等待几秒，安装完成后再次打开 EngAixt 就是新版。\n"
        "如果刚才点得太快，这是正常的防误开保护。"
    )
    try:
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "EngAixt 正在更新", 0x40)
        else:
            print(message)
    except Exception:
        print(message)


def _read_json_url(url: str, *, timeout: int, mirror_bases: list[str] | None = None) -> dict[str, Any]:
    urls = _build_mirror_urls(url, mirror_bases or MIRROR_BASE_URLS)
    last_error = "unknown error"
    for attempt in range(HTTP_RETRIES + 1):
        data = _read_bytes_url_with_fallback(urls, timeout=timeout)
        try:
            if not data.strip():
                raise ValueError("empty response")
            parsed = json.loads(data.decode("utf-8-sig"))
            if not isinstance(parsed, dict):
                raise ValueError(f"expected JSON object, got {type(parsed).__name__}")
            return parsed
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            last_error = _invalid_json_summary(data, exc)
            if attempt >= HTTP_RETRIES:
                break
            time.sleep(min(HTTP_RETRY_BASE_DELAY * (2 ** attempt), 8.0))
    raise AppUpdateError(f"update JSON is invalid: {url} ({last_error})")


def _invalid_json_summary(data: bytes, exc: BaseException) -> str:
    if not data.strip():
        return "empty response"
    if isinstance(exc, UnicodeDecodeError):
        return "response is not UTF-8 text"
    preview = data[:120].decode("utf-8", errors="replace").replace("\r", " ").replace("\n", " ").strip()
    return f"response is not a JSON object, starts with {preview!r}"


def _read_bytes_url_with_fallback(urls: list[str], *, timeout: int) -> bytes:
    """Try each URL in order; return first success."""
    last_exc: BaseException | None = None
    for url in urls:
        try:
            return _read_bytes_url(url, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except (AppUpdateError, TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            last_exc = exc
            continue
    raise last_exc or AppUpdateError("all mirror URLs failed")


def _read_bytes_url(url: str, *, timeout: int) -> bytes:
    last_exc: BaseException | None = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            return _read_bytes_url_once(url, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            last_exc = exc
            if attempt >= HTTP_RETRIES:
                break
            time.sleep(min(HTTP_RETRY_BASE_DELAY * (2 ** attempt), 8.0))
    raise AppUpdateError(f"network read failed after retries: {last_exc}")


def _read_bytes_url_once(url: str, *, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        chunks: list[bytes] = []
        while True:
            chunk = resp.read(HTTP_READ_CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def _download_part_to_path(
    url: str,
    path: Path,
    *,
    timeout: int,
    progress_callback: ProgressCallback | None,
    base_downloaded: int,
    total_size: int,
    index: int,
    total: int,
    mirror_urls: list[str] | None = None,
) -> None:
    urls = [url] + (mirror_urls or [])
    last_exc: BaseException | None = None
    for attempt_url in urls:
        try:
            _download_part_to_path_single(
                attempt_url,
                path,
                timeout=timeout,
                progress_callback=progress_callback,
                base_downloaded=base_downloaded,
                total_size=total_size,
                index=index,
                total=total,
            )
            return
        except AppUpdateError as exc:
            last_exc = exc
            continue
    raise last_exc or AppUpdateError("all mirror URLs failed for part download")


def _download_part_to_path_single(
    url: str,
    path: Path,
    *,
    timeout: int,
    progress_callback: ProgressCallback | None,
    base_downloaded: int,
    total_size: int,
    index: int,
    total: int,
) -> None:
    if path.exists():
        path.unlink()

    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl:
        _download_part_to_path_curl(
            curl,
            url,
            path,
            timeout=timeout,
            progress_callback=progress_callback,
            base_downloaded=base_downloaded,
            total_size=total_size,
            index=index,
            total=total,
        )
        return

    _download_part_to_path_urllib(
        url,
        path,
        timeout=timeout,
        progress_callback=progress_callback,
        base_downloaded=base_downloaded,
        total_size=total_size,
        index=index,
        total=total,
    )


def _download_part_to_path_curl(
    curl: str,
    url: str,
    path: Path,
    *,
    timeout: int,
    progress_callback: ProgressCallback | None,
    base_downloaded: int,
    total_size: int,
    index: int,
    total: int,
) -> None:
    command = [
        curl,
        "-L",
        "--fail",
        "--silent",
        "--show-error",
        "--retry",
        "3",
        "--connect-timeout",
        "15",
        "--speed-time",
        str(CURL_SPEED_TIME),
        "--speed-limit",
        str(CURL_SPEED_LIMIT),
        "--max-time",
        str(timeout),
        "-o",
        str(path),
        url,
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    try:
        while proc.poll() is None:
            current = path.stat().st_size if path.exists() else 0
            _emit(progress_callback, {
                "phase": "download",
                "index": index,
                "total": total,
                "downloaded": base_downloaded + current,
                "size": total_size,
                "message": f"Downloading update package {index}/{total}",
            })
            time.sleep(0.5)
        stdout, stderr = proc.communicate(timeout=5)
    except Exception:
        proc.kill()
        proc.communicate()
        path.unlink(missing_ok=True)
        raise

    if proc.returncode != 0:
        path.unlink(missing_ok=True)
        detail = (stderr or stdout or b"").decode("utf-8", errors="replace").strip()
        raise AppUpdateError(f"curl download failed: {detail or proc.returncode}")


def _download_part_to_path_urllib(
    url: str,
    path: Path,
    *,
    timeout: int,
    progress_callback: ProgressCallback | None,
    base_downloaded: int,
    total_size: int,
    index: int,
    total: int,
) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    downloaded = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp, path.open("wb") as out:
        while True:
            chunk = resp.read(HTTP_READ_CHUNK_SIZE)
            if not chunk:
                break
            out.write(chunk)
            downloaded += len(chunk)
            _emit(progress_callback, {
                "phase": "download",
                "index": index,
                "total": total,
                "downloaded": base_downloaded + downloaded,
                "size": total_size,
                "message": f"Downloading update package {index}/{total}",
            })


def _append_part_file(part_path: Path, out, whole_sha) -> tuple[int, str]:
    part_sha = hashlib.sha256()
    size = 0
    with part_path.open("rb") as part_file:
        while True:
            chunk = part_file.read(HTTP_READ_CHUNK_SIZE)
            if not chunk:
                break
            out.write(chunk)
            whole_sha.update(chunk)
            part_sha.update(chunk)
            size += len(chunk)
    return size, part_sha.hexdigest()


def _resolve_package_part_url(package_manifest_url: str, part_path: str) -> str:
    if not part_path:
        raise AppUpdateError("update package part path is empty")
    if urllib.parse.urlparse(part_path).scheme:
        return part_path
    parsed = urllib.parse.urlparse(package_manifest_url)
    origin = f"{parsed.scheme}://{parsed.netloc}/"
    if part_path.startswith("/"):
        return urllib.parse.urljoin(origin, part_path.lstrip("/"))
    if part_path.startswith("downloads/"):
        return urllib.parse.urljoin(origin, part_path)
    return urllib.parse.urljoin(package_manifest_url, part_path)


def _emit(callback: ProgressCallback | None, event: dict[str, Any]) -> None:
    if callback:
        callback(event)


def _build_mirror_urls(primary_url: str, mirror_bases: list[str]) -> list[str]:
    """Build [primary, mirror1, mirror2, ...] URL list from primary and mirror base URLs."""
    urls = [primary_url]
    if not mirror_bases:
        return urls
    parsed = urllib.parse.urlparse(primary_url)
    primary_origin = f"{parsed.scheme}://{parsed.netloc}"
    for mirror_base in mirror_bases:
        mirror_base = mirror_base.rstrip("/")
        if primary_origin and primary_url.startswith(primary_origin):
            mirror_url = mirror_base + primary_url[len(primary_origin):]
            urls.append(mirror_url)
    return urls


def get_predownloaded_package(manifest: dict[str, Any]) -> Path | None:
    """Return the pre-downloaded zip path if available and matches manifest version."""
    with _predownload_lock:
        state = _predownload_state
        if not state.get("done") or state.get("error"):
            return None
        if state.get("version") != str(manifest.get("version") or ""):
            return None
        zip_path = state.get("zip_path")
        if zip_path and Path(zip_path).exists():
            return Path(zip_path)
    return None


def start_background_predownload(manifest: dict[str, Any]) -> bool:
    """Start background download of update package. Idempotent - won't re-download same version."""
    version = str(manifest.get("version") or "")
    if not version:
        return False

    with _predownload_lock:
        state = _predownload_state
        if state.get("version") == version:
            if state.get("done") and not state.get("error"):
                return False
            if state.get("running"):
                return False

        _predownload_state.clear()
        _predownload_state["version"] = version
        _predownload_state["manifest"] = manifest
        _predownload_state["running"] = True
        _predownload_state["done"] = False

    def _run():
        try:
            zip_path = download_update_package(manifest, progress_callback=None)
            with _predownload_lock:
                _predownload_state["zip_path"] = str(zip_path)
                _predownload_state["done"] = True
                _predownload_state["running"] = False
        except Exception as exc:
            with _predownload_lock:
                _predownload_state["error"] = str(exc)
                _predownload_state["done"] = True
                _predownload_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return True


def _safe_name(value: str) -> str:
    clean = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value.strip())
    return clean or "latest"


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in str(value or "").strip().lstrip("vV").replace("-", ".").split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        if digits == "":
            parts.append(0)
        else:
            parts.append(int(digits))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _ps_b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _ps_encoded_command(value: str) -> str:
    return base64.b64encode(value.encode("utf-16le")).decode("ascii")


def _append_update_launch_log(message: str) -> None:
    try:
        script_dir = _updates_dir()
        script_dir.mkdir(parents=True, exist_ok=True)
        line = time.strftime("[%Y-%m-%d %H:%M:%S] ", time.localtime()) + message + "\n"
        (script_dir / "launch-update.log").open("a", encoding="utf-8").write(line)
    except Exception:
        pass


def _write_update_cmd_launcher(script_path: Path, pid_to_wait: int) -> Path:
    script_dir = _updates_dir()
    script_dir.mkdir(parents=True, exist_ok=True)
    launcher_path = script_dir / "launch-engaixt-update.cmd"
    log_path = script_dir / "launch-update.log"
    encoded = _ps_encoded_command(f"""
$ErrorActionPreference = "Continue"

function FromB64([string]$Value) {{
    return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($Value))
}}

$scriptPath = FromB64 "{_ps_b64(str(script_path))}"
$logPath = FromB64 "{_ps_b64(str(log_path))}"
$pidToWait = {int(pid_to_wait)}

function Log([string]$Message) {{
    try {{
        $logDir = Split-Path -Parent $logPath
        if (-not (Test-Path -LiteralPath $logDir)) {{
            New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        }}
        $line = "[{{0}}] {{1}}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
        Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    }} catch {{
    }}
}}

try {{
    Log "cmd entered pid=$pidToWait"
    $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $scriptPath, "-PidToWait", [string]$pidToWait)
    $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $args -WindowStyle Hidden -PassThru
    Log "powershell start returned pid=$($proc.Id)"
}} catch {{
    Log ("powershell start failed: " + $_.Exception.Message)
}}
""")
    text = "\r\n".join(
        [
            "@echo off",
            "setlocal",
            f"start \"EngAixt Update Launcher\" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}",
            "exit /b 0",
            "",
        ]
    )
    launcher_path.write_text(text, encoding="ascii")
    return launcher_path


def _write_update_script(
    zip_path: Path,
    install_dir: Path,
    exe_name: str,
    manifest: dict[str, Any],
    *,
    restart_after_update: bool = False,
) -> Path:
    script_dir = _updates_dir()
    script_dir.mkdir(parents=True, exist_ok=True)
    script_path = script_dir / "apply-engaixt-update.ps1"
    version = str(manifest.get("version") or "latest")
    log_path = script_dir / "apply-update.log"
    pending_path = _pending_update_path()
    version_marker = json.dumps({"schema": 1, "app": APP_NAME, "version": version}, ensure_ascii=True)
    restart_literal = "$true" if restart_after_update else "$false"
    text = f"""param([int]$PidToWait)
$ErrorActionPreference = "Stop"

function FromB64([string]$Value) {{
    return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($Value))
}}

$zipPath = FromB64 "{_ps_b64(str(zip_path))}"
$installDir = FromB64 "{_ps_b64(str(install_dir))}"
$exeName = FromB64 "{_ps_b64(exe_name)}"
$version = FromB64 "{_ps_b64(version)}"
$logPath = FromB64 "{_ps_b64(str(log_path))}"
$pendingPath = FromB64 "{_ps_b64(str(pending_path))}"
$versionMarkerJson = FromB64 "{_ps_b64(version_marker)}"
$restartAfterUpdate = {restart_literal}
$waitDeadlineSeconds = 20

function Log([string]$Message) {{
    $logDir = Split-Path -Parent $logPath
    if (-not (Test-Path -LiteralPath $logDir)) {{
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    }}
    $line = "[{{0}}] {{1}}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}}

function Test-IsWithin([string]$Path, [string]$Root) {{
    try {{
        $fullPath = [System.IO.Path]::GetFullPath($Path)
        $fullRoot = [System.IO.Path]::GetFullPath($Root)
        if (-not $fullRoot.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {{
            $fullRoot += [System.IO.Path]::DirectorySeparatorChar
        }}
        return $fullPath.StartsWith($fullRoot, [System.StringComparison]::OrdinalIgnoreCase)
    }} catch {{
        return $false
    }}
}}

function Stop-InstallProcesses {{
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {{
        -not [string]::IsNullOrWhiteSpace($_.ExecutablePath) -and
        (Test-IsWithin $_.ExecutablePath $installDir) -and
        ($_.Name -eq $exeName -or $_.Name -eq "game-translator.exe" -or $_.Name -eq "EngAixt.exe")
    }}
    foreach ($proc in $procs) {{
        if ($proc.ProcessId -eq $PID) {{ continue }}
        Log "stopping install process pid=$($proc.ProcessId) path=$($proc.ExecutablePath)"
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }}
}}

function Clear-PendingUpdate {{
    try {{
        if (Test-Path -LiteralPath $pendingPath) {{
            Remove-Item -LiteralPath $pendingPath -Force
            Log "removed pending update marker"
        }}
    }} catch {{
        Log ("pending update marker cleanup failed: " + $_.Exception.Message)
    }}
}}

function Copy-UpdateFiles([string]$Source, [string]$Destination) {{
    $lastError = $null
    for ($attempt = 1; $attempt -le 8; $attempt++) {{
        try {{
            Get-ChildItem -LiteralPath $Source -Force | Copy-Item -Destination $Destination -Recurse -Force
            return
        }} catch {{
            $lastError = $_.Exception.Message
            Log "copy attempt $attempt failed: $lastError"
            Stop-InstallProcesses
            Start-Sleep -Milliseconds (500 * $attempt)
        }}
    }}
    throw "copy update files failed after retries: $lastError"
}}

function Assert-UpdatedExe([string]$SourceExe, [string]$DestExe) {{
    if (-not (Test-Path -LiteralPath $DestExe)) {{
        throw "updated executable missing: $DestExe"
    }}
    if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) {{
        $sourceHash = (Get-FileHash -LiteralPath $SourceExe -Algorithm SHA256).Hash
        $destHash = (Get-FileHash -LiteralPath $DestExe -Algorithm SHA256).Hash
        Log "exe hash source=$sourceHash dest=$destHash"
        if ($sourceHash -ne $destHash) {{
            throw "updated executable hash mismatch"
        }}
    }}
}}

try {{
    Log "updater entered pid=$PID waitPid=$PidToWait installDir=$installDir zip=$zipPath"
    $deadline = (Get-Date).AddSeconds($waitDeadlineSeconds)
    while ($PidToWait -gt 0 -and (Get-Process -Id $PidToWait -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {{
        Start-Sleep -Milliseconds 500
    }}
    if ($PidToWait -gt 0 -and (Get-Process -Id $PidToWait -ErrorAction SilentlyContinue)) {{
        Log "wait pid still alive after $waitDeadlineSeconds seconds; forcing install process shutdown"
        Stop-Process -Id $PidToWait -Force -ErrorAction SilentlyContinue
    }}
    Stop-InstallProcesses
    Start-Sleep -Milliseconds 500

    $workRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("engaixt-update-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $workRoot | Out-Null
    Log "extracting $zipPath"
    Expand-Archive -LiteralPath $zipPath -DestinationPath $workRoot -Force

    $source = $workRoot
    $nested = Get-ChildItem -LiteralPath $workRoot -Directory | Where-Object {{ Test-Path -LiteralPath (Join-Path $_.FullName $exeName) }} | Select-Object -First 1
    if ($nested) {{ $source = $nested.FullName }}
    if (-not (Test-Path -LiteralPath (Join-Path $source $exeName))) {{
        throw "updated package does not contain $exeName"
    }}

    $backup = Join-Path ([System.IO.Path]::GetTempPath()) ("engaixt-backup-" + (Get-Date -Format "yyyyMMddHHmmss"))
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    Log "backup to $backup"
    Get-ChildItem -LiteralPath $installDir -Force | Copy-Item -Destination $backup -Recurse -Force

    $editionMarkers = @(
        "edition.json",
        "_internal\\edition.json",
        "edition.txt",
        "_internal\\edition.txt"
    )
    $preservedEditionMarkers = @{{}}
    foreach ($relativeMarker in $editionMarkers) {{
        $markerPath = Join-Path $installDir $relativeMarker
        if (Test-Path -LiteralPath $markerPath) {{
            $preservedEditionMarkers[$relativeMarker] = [System.Convert]::ToBase64String([System.IO.File]::ReadAllBytes($markerPath))
            Log "preserved edition marker $relativeMarker"
        }}
    }}

    Log "copying update files"
    Copy-UpdateFiles $source $installDir

    foreach ($relativeMarker in $preservedEditionMarkers.Keys) {{
        $markerPath = Join-Path $installDir $relativeMarker
        $markerDir = Split-Path -Parent $markerPath
        if (-not (Test-Path -LiteralPath $markerDir)) {{
            New-Item -ItemType Directory -Force -Path $markerDir | Out-Null
        }}
        [System.IO.File]::WriteAllBytes($markerPath, [System.Convert]::FromBase64String($preservedEditionMarkers[$relativeMarker]))
        Log "restored edition marker $relativeMarker"
    }}

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $versionMarkerPaths = @(
        (Join-Path $installDir "app_version.json"),
        (Join-Path $installDir "_internal\\app_version.json")
    )
    foreach ($markerPath in $versionMarkerPaths) {{
        $markerDir = Split-Path -Parent $markerPath
        if (Test-Path -LiteralPath $markerDir) {{
            [System.IO.File]::WriteAllText($markerPath, $versionMarkerJson, $utf8NoBom)
            Log "wrote version marker $markerPath"
        }}
    }}

    $exePath = Join-Path $installDir $exeName
    Assert-UpdatedExe (Join-Path $source $exeName) $exePath
    Clear-PendingUpdate
    if ($restartAfterUpdate) {{
        Log "restart $exePath"
        Start-Process -FilePath $exePath -WorkingDirectory $installDir
    }} else {{
        Log "manual restart requested"
    }}
    Log "update $version done"
}} catch {{
    Log ("update failed: " + $_.Exception.Message)
    Clear-PendingUpdate
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(("EngAixt update failed: " + $_.Exception.Message), "EngAixt Update", "OK", "Error") | Out-Null
}}
"""
    script_path.write_text(text, encoding="utf-8")
    return script_path
