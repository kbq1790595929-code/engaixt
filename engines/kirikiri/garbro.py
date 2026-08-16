"""GARbro 外部工具调用：条目列表探测与脚本条目提取。"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from utils.logger import debug, warning

from engines.kirikiri.codec import _safe_output_path
from engines.kirikiri.xp3 import _is_script_archive_name


@dataclass(frozen=True)
class _GarbroEntry:
    name: str
    size: int


@dataclass(frozen=True)
class _ExternalToolProbe:
    tool: str
    path: str
    status: str
    message: str = ""
    archive_file: str = ""


@dataclass(frozen=True)
class _GarbroListResult:
    entries: list[_GarbroEntry]
    status: str
    message: str = ""


def _probe_garbro_entries(garbro: Path, xp3: Path) -> _GarbroListResult:
    try:
        result = subprocess.run(
            [str(garbro), str(xp3)],
            cwd=str(xp3.parent),
            capture_output=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        warning(f"GARbro 列表超时: {xp3.name}")
        return _GarbroListResult([], "timeout", "GARbro list timeout")
    except Exception as exc:
        debug(f"GARbro 列表失败 {xp3.name}: {exc}")
        return _GarbroListResult([], "error", str(exc))

    output = _decode_garbro_output(result.stdout) + "\n" + _decode_garbro_output(result.stderr)
    entries: list[_GarbroEntry] = []
    for line in output.splitlines():
        match = re.match(r"\s*(\d+)\s+\[[0-9A-Fa-f]+\]\s+(.+?)\s*$", line)
        if not match:
            continue
        name = match.group(2).strip().replace("\\", "/")
        if not name:
            continue
        try:
            size = int(match.group(1))
        except ValueError:
            size = 0
        entries.append(_GarbroEntry(name=name, size=size))
    message = output.strip()[:500]
    if not entries and "unknown format" in output.lower():
        debug(f"GARbro 不支持 XP3 格式 {xp3.name}: {message}")
        return _GarbroListResult(entries, "unknown_format", message)
    if result.returncode != 0 and not entries:
        debug(f"GARbro 列表未成功 {xp3.name}: rc={result.returncode}")
        status = "failed"
        return _GarbroListResult(entries, status, message)
    status = "entries_found" if entries else "no_entries"
    return _GarbroListResult(entries, status, message)


def _list_garbro_entries(garbro: Path, xp3: Path) -> list[_GarbroEntry]:
    return _probe_garbro_entries(garbro, xp3).entries


def _decode_garbro_output(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    for encoding in ("utf-8-sig", "utf-8", "mbcs", "cp932", "shift_jis"):
        try:
            return data.decode(encoding)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def _garbro_script_entries(xp3: Path, entries: list[_GarbroEntry]) -> list[_GarbroEntry]:
    if not entries:
        return []
    direct: list[_GarbroEntry] = []
    extensionless: list[_GarbroEntry] = []
    for entry in entries:
        suffix = Path(entry.name).suffix.lower()
        if Path(entry.name).name.lower().startswith("startup"):
            continue
        if entry.size <= 0 or entry.size > 8 * 1024 * 1024:
            continue
        if suffix in {".ks", ".tjs", ".scn"}:
            direct.append(entry)
        elif not suffix and _is_script_archive_name(xp3.name):
            extensionless.append(entry)
    if direct:
        return sorted(direct, key=lambda entry: entry.name.lower())
    return sorted(extensionless, key=lambda entry: entry.name.lower())


def _extract_garbro_entries(garbro: Path, xp3: Path, extract_dir: Path, entries: list[_GarbroEntry]) -> bool:
    extract_dir.mkdir(parents=True, exist_ok=True)
    extracted = 0
    for chunk in _chunked(entries, 64):
        cmd = [str(garbro), "-x", str(xp3)] + [entry.name for entry in chunk]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(extract_dir),
                capture_output=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            warning(f"GARbro 解包超时: {xp3.name}")
            continue
        except Exception as exc:
            debug(f"GARbro 解包失败 {xp3.name}: {exc}")
            continue

        if result.returncode != 0:
            debug(
                f"GARbro 命令未成功: {xp3.name} rc={result.returncode} "
                f"{_decode_garbro_output(result.stderr).strip()[:300]}"
            )
        for entry in chunk:
            out_path = _safe_output_path(extract_dir, entry.name)
            if out_path is not None and out_path.exists():
                extracted += 1
    return extracted > 0


def _chunked(items: list[_GarbroEntry], size: int):
    for idx in range(0, len(items), size):
        yield items[idx:idx + size]
