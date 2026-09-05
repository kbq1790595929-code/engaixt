"""Verification for KiriKiri static patch outputs.

The verifier deliberately reads the generated XP3 payloads instead of
assuming that a successful repack command means that the game can load the
translated scripts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engines.kirikiri.codec import (
    _decode_kirikiri_sjis_tunnel_text,
    _kirikiri_placeholder_token,
)
from engines.kirikiri.xp3 import _read_xp3_entry_bytes, _read_xp3_index


def verify_kirikiri_repack_outputs(
    game_path: Path,
    translated: list,
    result: dict[str, Any],
    *,
    runtime_resource_overlay: bool = False,
) -> dict[str, Any]:
    game_dir = game_path if game_path.is_dir() else game_path.parent
    meta_dir = game_dir / "_translation_meta"
    files = _group_items(translated)
    static_diag = _read_json(meta_dir / "kirikiri_static_xp3_rebuild.json")
    placeholder_map = _read_placeholder_map(meta_dir / "kirikiri_placeholder_map.tsv")
    tunnel_table = _read_tunnel_table(game_dir, meta_dir)

    archive_candidates = _archive_candidates(game_dir, meta_dir, static_diag)
    loose_patch = meta_dir / "kirikiri_patch"
    manifest = meta_dir / "kirikiri_patch_manifest.txt"
    archive_reports: list[dict[str, Any]] = []
    loaded_archives: list[tuple[Path, dict[str, bytes]]] = []

    for archive in archive_candidates:
        report: dict[str, Any] = {
            "path": _relative_or_name(archive, game_dir),
            "exists": archive.is_file(),
            "readable": False,
            "entries": 0,
            "error": "",
        }
        if archive.is_file():
            try:
                index = _read_xp3_index(archive)
                payloads: dict[str, bytes] = {}
                for entry in index.entries:
                    payloads.setdefault(_normalize(entry.name), _read_xp3_entry_bytes(archive, entry))
                report.update({"readable": True, "entries": len(index.entries)})
                loaded_archives.append((archive, payloads))
            except Exception as exc:
                report["error"] = str(exc)
        archive_reports.append(report)

    hits = 0
    checked = 0
    matched_files: list[str] = []
    missing_files: list[str] = []
    for rel, items in files.items():
        payload = _find_archive_payload(rel, loaded_archives)
        if payload is None:
            payload = _find_loose_payload(loose_patch, rel)
        if payload is None:
            missing_files.append(rel)
            continue
        checked += 1
        matched_files.append(rel)
        if any(_translated_present(payload, item, placeholder_map, tunnel_table) for item in items):
            hits += 1

    rebuilt_files = _rebuilt_files(static_diag)
    expected_count = len(files)
    archive_readable = any(report["readable"] for report in archive_reports)
    invalid_archives = [
        str(report["path"])
        for report in archive_reports
        if report["exists"] and not report["readable"]
    ]
    patch_exists = any(report["exists"] for report in archive_reports) or loose_patch.is_dir()
    result.update({
        "checked": bool(checked or archive_readable or loose_patch.is_dir()),
        "hits": hits,
        "files_checked": checked,
        "patch_exists": patch_exists,
        "invalid_archives": invalid_archives,
        "expected_files": expected_count,
        "matched_files": matched_files[:200],
        "missing_files": missing_files[:200],
        "archive_reports": archive_reports,
        "rebuilt_archives": list(static_diag.get("rebuilt_archives") or []),
        "rebuilt_files": rebuilt_files[:200],
        "manifest": str(manifest) if manifest.exists() else "",
        "patch_dir": str(loose_patch) if loose_patch.is_dir() else "",
        "patch_xp3": _first_existing_archive(archive_candidates, game_dir),
        "note": (
            "KiriKiri runtime overlay resource verification"
            if runtime_resource_overlay
            else "KiriKiri static archive/loose patch verification"
        ),
    })

    if rebuilt_files:
        result["source_archive_rewrite_verified"] = _verify_rebuilt_sources(
            game_dir, static_diag, files, placeholder_map, tunnel_table
        )
    else:
        result["source_archive_rewrite_verified"] = None

    return result


def _group_items(items: list) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for item in items:
        rel = _normalize(str(getattr(item, "file", "") or ""))
        if rel:
            grouped.setdefault(rel, []).append(item)
    return grouped


def _archive_candidates(game_dir: Path, meta_dir: Path, static_diag: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    for path in (game_dir / "patch.xp3", meta_dir / "kirikiri_patch.xp3"):
        if path not in candidates:
            candidates.append(path)
    for value in static_diag.get("rebuilt_archives") or []:
        rel = _normalize(str(value))
        if not rel or rel.startswith("../") or "/../" in rel:
            continue
        path = game_dir / rel
        if path not in candidates:
            candidates.append(path)
    return candidates


def _find_archive_payload(rel: str, archives: list[tuple[Path, dict[str, bytes]]]) -> bytes | None:
    basename = Path(rel).name.casefold()
    for _path, payloads in archives:
        exact = payloads.get(rel)
        if exact is not None:
            return exact
        aliases = [data for name, data in payloads.items() if Path(name).name.casefold() == basename]
        if len(aliases) == 1:
            return aliases[0]
    return None


def _find_loose_payload(patch_dir: Path, rel: str) -> bytes | None:
    if not patch_dir.is_dir():
        return None
    exact = patch_dir / rel
    if exact.is_file():
        try:
            return exact.read_bytes()
        except OSError:
            return None
    aliases = list(patch_dir.rglob(Path(rel).name))
    if len(aliases) != 1:
        return None
    try:
        return aliases[0].read_bytes()
    except OSError:
        return None


def _translated_present(
    payload: bytes,
    item: Any,
    placeholder_map: dict[str, str],
    tunnel_table: bytes = b"",
) -> bool:
    translated = str(getattr(item, "translated", "") or "")
    if not translated:
        return False
    variants = [
        translated.encode("utf-8", errors="ignore"),
        translated.encode("utf-16le", errors="ignore"),
        translated.encode("utf-16", errors="ignore"),
    ]
    try:
        variants.append(translated.encode("cp932", errors="ignore"))
    except LookupError:
        pass
    if any(value and value in payload for value in variants):
        return True

    for encoding in ("utf-8", "utf-16le", "utf-16", "cp932"):
        try:
            decoded = payload.decode(encoding, errors="ignore")
        except (LookupError, UnicodeError):
            continue
        if translated in decoded:
            return True
        if tunnel_table:
            try:
                if translated in _decode_kirikiri_sjis_tunnel_text(decoded, tunnel_table):
                    return True
            except (TypeError, ValueError):
                pass

    token = _kirikiri_placeholder_token(item)
    return bool(token and token in payload.decode("ascii", errors="ignore") and token in placeholder_map)


def _verify_rebuilt_sources(
    game_dir: Path,
    static_diag: dict[str, Any],
    files: dict[str, list],
    placeholder_map: dict[str, str],
    tunnel_table: bytes,
) -> dict[str, Any]:
    archive_map = static_diag.get("rebuilt_files_by_archive") or {}
    checked = 0
    hits = 0
    missing: list[str] = []
    reports: list[dict[str, Any]] = []
    for archive_name, rebuilt in archive_map.items():
        archive = game_dir / _normalize(str(archive_name))
        report = {"path": _relative_or_name(archive, game_dir), "readable": False, "entries": 0}
        try:
            index = _read_xp3_index(archive)
            payloads = {
                _normalize(entry.name): _read_xp3_entry_bytes(archive, entry)
                for entry in index.entries
            }
            report.update({"readable": True, "entries": len(payloads)})
            for rel in rebuilt or []:
                rel = _normalize(str(rel))
                items = files.get(rel, [])
                if not items:
                    continue
                payload = payloads.get(rel)
                if payload is None:
                    missing.append(rel)
                    continue
                checked += 1
                if any(_translated_present(payload, item, placeholder_map, tunnel_table) for item in items):
                    hits += 1
        except Exception as exc:
            report["error"] = str(exc)
        reports.append(report)
    return {
        "checked": bool(reports),
        "files_checked": checked,
        "hits": hits,
        "missing_files": missing[:200],
        "archives": reports,
    }


def _rebuilt_files(static_diag: dict[str, Any]) -> list[str]:
    values = static_diag.get("rebuilt_files") or []
    return sorted({_normalize(str(value)) for value in values if str(value).strip()})


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _read_placeholder_map(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            if not line or line.startswith("#") or "\t" not in line:
                continue
            token, encoded = line.split("\t", 1)
            values[token.strip()] = encoded.strip()
    except (OSError, UnicodeError):
        return {}
    return values


def _read_tunnel_table(game_dir: Path, meta_dir: Path) -> bytes:
    for path in (meta_dir / "kirikiri_sjis_ext.bin", game_dir / "kirikiri_sjis_ext.bin"):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if data and len(data) % 2 == 0:
            return data
    return b""


def _first_existing_archive(candidates: list[Path], game_dir: Path) -> str:
    for candidate in candidates:
        if candidate.is_file():
            return _relative_or_name(candidate, game_dir)
    return ""


def _relative_or_name(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return path.name


def _normalize(value: str) -> str:
    return str(value or "").replace("\\", "/").lstrip("/")
