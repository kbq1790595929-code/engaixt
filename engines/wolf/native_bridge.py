from __future__ import annotations

import json
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NativeArchiveInfo:
    crypt_version: int
    protection: str


def bridge_path() -> Path:
    return Path(__file__).resolve().parents[2] / "assets" / "wolf_runtime" / "engaixt_wolf_native.exe"


def is_available() -> bool:
    return bridge_path().is_file()


def inspect(archive: Path) -> tuple[NativeArchiveInfo, dict]:
    try:
        result = _run([str(bridge_path()), "inspect", str(archive)], timeout=30)
    except OSError as exc:
        result = {
            "command": _redact_command([str(bridge_path()), "inspect", str(archive)]),
            "exit_code": -1,
            "duration_ms": 0,
            "stdout": "",
            "stderr": str(exc),
        }
    if result["exit_code"] != 0:
        fallback = _inspect_header(archive)
        if fallback is not None:
            result["native_exit_code"] = result["exit_code"]
            result["exit_code"] = 0
            result["inspection_backend"] = "python_header"
            return fallback, result
        raise RuntimeError(f"WOLF native archive inspection failed: {result['stderr'][-500:]}")
    try:
        payload = json.loads(result["stdout"].strip())
        info = NativeArchiveInfo(
            crypt_version=int(payload["crypt_version"]),
            protection=str(payload["protection"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("WOLF native archive inspection returned invalid data") from exc
    return info, result


def _inspect_header(archive: Path) -> NativeArchiveInfo | None:
    """Read non-secret WOLF archive metadata without requiring the native tool.

    This only classifies the public archive header. Unpacking and repacking
    protected archives still require the verified native bridge and profile.
    """
    try:
        with archive.open("rb") as stream:
            header = stream.read(48)
    except OSError:
        return None
    if len(header) < 48 or header[:2] != b"DX":
        return None
    crypt_version = struct.unpack_from("<H", header, 46)[0]
    return NativeArchiveInfo(
        crypt_version=crypt_version,
        protection="pro" if crypt_version >= 1000 else "standard",
    )


def unpack(archive: Path, key_file: Path, crypt_version: int) -> dict:
    return _run(
        [
            str(bridge_path()), "unpack", "--input", str(archive),
            "--key-file", str(key_file), "--crypt-version", str(crypt_version),
        ],
        timeout=1800,
    )


def pack(data_directory: Path, key_file: Path, crypt_version: int) -> dict:
    return _run(
        [
            str(bridge_path()), "pack", "--input", str(data_directory),
            "--key-file", str(key_file), "--crypt-version", str(crypt_version),
        ],
        timeout=1800,
    )


def _run(command: list[str], timeout: int) -> dict:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=str(bridge_path().parent),
        capture_output=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    return {
        "command": _redact_command(command),
        "exit_code": int(completed.returncode),
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "stdout": _decode(completed.stdout),
        "stderr": _decode(completed.stderr),
    }


def _redact_command(command: list[str]) -> list[str]:
    redacted = [str(part) for part in command]
    try:
        key_index = redacted.index("--key-file") + 1
    except ValueError:
        return redacted
    if key_index < len(redacted):
        redacted[key_index] = "<ephemeral>"
    return redacted


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "cp932", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
