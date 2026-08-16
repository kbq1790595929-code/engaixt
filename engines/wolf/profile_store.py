from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import secrets
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_CACHE_VERSION = 1
_VALIDATION_LEVEL = "clone_runtime_v1"
_ENTROPY = b"EngAixt.WolfPro.Profile.v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


@dataclass(frozen=True)
class ProtectedArchiveProfile:
    key_id: str
    crypt_version: int
    validation: str
    key_hex: str


def profile_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "EngAixt" / "wolf_pro_profiles.json"


def archive_key_id(archive: Path) -> str:
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def lookup_verified_profile(
    archive: Path,
    crypt_version: int,
    *,
    path: Path | None = None,
) -> ProtectedArchiveProfile | None:
    return lookup_verified_profile_by_id(
        archive_key_id(archive),
        crypt_version,
        path=path,
    )


def lookup_verified_profile_by_id(
    key_id: str,
    crypt_version: int,
    *,
    path: Path | None = None,
) -> ProtectedArchiveProfile | None:
    entry = _load(path or profile_path()).get(key_id)
    if not isinstance(entry, dict):
        return None
    if int(entry.get("crypt_version", 0) or 0) != crypt_version:
        return None
    validation = str(entry.get("validation") or "")
    if validation != _VALIDATION_LEVEL:
        return None
    try:
        protected = base64.b64decode(str(entry.get("protected_key") or ""), validate=True)
        key_hex = _unprotect(protected).decode("ascii")
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not _valid_key(key_hex):
        return None
    return ProtectedArchiveProfile(
        key_id=key_id,
        crypt_version=crypt_version,
        validation=validation,
        key_hex=key_hex.lower(),
    )


def store_verified_profile(
    archive: Path,
    crypt_version: int,
    key_hex: str,
    *,
    path: Path | None = None,
) -> str:
    normalized = key_hex.strip().lower()
    if crypt_version <= 0 or not _valid_key(normalized):
        raise ValueError("Invalid WOLF protected archive profile")
    destination = path or profile_path()
    entries = _load(destination)
    key_id = archive_key_id(archive)
    protected = _protect(normalized.encode("ascii"))
    entries[key_id] = {
        "crypt_version": crypt_version,
        "validation": _VALIDATION_LEVEL,
        "protected_key": base64.b64encode(protected).decode("ascii"),
        "validated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save(destination, entries)
    return key_id


@contextmanager
def temporary_key_file(root: Path, key_hex: str) -> Iterator[Path]:
    normalized = key_hex.strip().lower()
    if not _valid_key(normalized):
        raise ValueError("Invalid WOLF protected archive key")
    secret_root = root / ".secrets"
    secret_root.mkdir(parents=True, exist_ok=True)
    path = secret_root / f"wolf_{secrets.token_hex(8)}.hex"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(normalized.encode("ascii"))
        _restrict_permissions(path)
        yield path
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        try:
            secret_root.rmdir()
        except OSError:
            pass


def _load(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
        return {}
    profiles = data.get("profiles")
    return profiles if isinstance(profiles, dict) else {}


def _save(path: Path, entries: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps({"version": _CACHE_VERSION, "profiles": entries}, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)
    _restrict_permissions(path)


def _protect(data: bytes) -> bytes:
    return _crypt_data(data, protect=True)


def _unprotect(data: bytes) -> bytes:
    return _crypt_data(data, protect=False)


def _crypt_data(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt" or not hasattr(ctypes, "windll"):
        raise OSError("WOLF protected profiles require Windows DPAPI")
    input_buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(_ENTROPY)
    input_blob = _DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy_blob = _DataBlob(
        len(_ENTROPY),
        ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    output_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if protect:
        ok = function(
            ctypes.byref(input_blob),
            "EngAixt WOLF Pro profile",
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    else:
        ok = function(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def _valid_key(value: str) -> bool:
    if len(value) < 2 or len(value) % 2:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def _restrict_permissions(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass
