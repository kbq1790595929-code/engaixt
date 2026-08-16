"""KiriKiri 运行时 dump 目标管理：导出、准备与外部 dump 导入的公开 API。"""

from __future__ import annotations

import shutil
from pathlib import Path

from utils.kirikiri_psb import is_kirikiri_scn
from utils.logger import debug, warning

from engines.kirikiri.codec import (
    _decode_script_bytes_for_quality,
    _is_probably_scriptless_kirikiri_payload,
    _safe_output_path,
)
from engines.kirikiri.xp3 import (
    _Xp3Entry,
    _read_xp3_index,
    _select_script_xp3_files,
    _xp3_entry_is_lightweight_obfuscated_script_candidate,
    _xp3_entry_is_obfuscated_script_candidate,
    _xp3_entry_is_script,
    _xp3_entry_needs_runtime_dump,
)


def _kirikiri_runtime_dump_targets_enabled() -> bool:
    try:
        from config import get_config

        cfg = get_config()
        return bool(
            getattr(cfg, "kirikiri_auto_launch_dump", False)
            or getattr(cfg, "kirikiri_use_external_krkrdump", False)
        )
    except Exception:
        return False


def export_kirikiri_dump_targets_from_xp3(xp3_path: Path, game_dir: Path) -> Path:
    """Write script resource names from an XP3 index for the native dumper.

    The output only contains resource names. The native runtime still reads the
    real script bytes from the target game process, so this can be used with
    third-party or historical patch archives as an index hint without copying
    their script contents.
    """
    index = _read_xp3_index(xp3_path)
    targets = sorted(
        {
            entry.name.replace("\\", "/")
            for entry in index.entries
            if (
                _xp3_entry_is_script(entry)
                or _xp3_entry_is_obfuscated_script_candidate(xp3_path, entry)
            ) and not Path(entry.name).name.lower().startswith("startup")
        },
        key=str.lower,
    )
    meta = game_dir / "_translation_meta"
    meta.mkdir(parents=True, exist_ok=True)
    out = meta / "kirikiri_dump_targets.txt"
    out.write_text("\n".join(targets) + ("\n" if targets else ""), encoding="utf-8")
    return out


def prepare_kirikiri_dump_targets_from_game(game_dir: Path) -> dict[str, object]:
    """Prepare native dump targets from XP3 indexes without extracting text."""
    game_dir = Path(game_dir)
    write_targets = _kirikiri_runtime_dump_targets_enabled()
    xp3_files = sorted(game_dir.glob("*.xp3"), key=lambda p: p.name.lower())
    candidates = _select_script_xp3_files(xp3_files)
    targets: set[str] = set()
    indexed_archives: list[str] = []
    failed_archives: list[str] = []
    protected_hint_count = 0
    logical_target_count = 0
    for xp3 in candidates:
        try:
            index = _read_xp3_index(xp3)
        except Exception:
            failed_archives.append(xp3.name)
            continue
        indexed_archives.append(xp3.name)
        for entry in index.entries:
            if Path(entry.name).name.lower().startswith("startup"):
                continue
            is_script = _xp3_entry_is_script(entry)
            is_protected_hint = _xp3_entry_needs_runtime_dump(xp3, entry)
            if is_protected_hint:
                protected_hint_count += 1
            if is_script or _xp3_entry_is_lightweight_obfuscated_script_candidate(xp3, entry) or is_protected_hint:
                logical_target_count += 1
            if is_protected_hint:
                rel = entry.name.replace("\\", "/")
                targets.add(rel)
                targets.add(f"{xp3.name}>{rel}")

    out_path: Path | None = None
    explicit_path: Path | None = None
    if write_targets and targets and protected_hint_count:
        meta = game_dir / "_translation_meta"
        meta.mkdir(parents=True, exist_ok=True)
        existing: set[str] = set()
        for name in ("kirikiri_dump_targets.txt", "kirikiri_explicit_dump_targets.txt"):
            path = meta / name
            if not path.exists():
                continue
            try:
                existing.update(
                    line.strip()
                    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                )
            except OSError:
                pass
        merged = sorted(existing | targets, key=str.lower)
        payload = "\n".join(merged) + ("\n" if merged else "")
        out_path = meta / "kirikiri_dump_targets.txt"
        explicit_path = meta / "kirikiri_explicit_dump_targets.txt"
        out_path.write_text(payload, encoding="utf-8")
        explicit_path.write_text(payload, encoding="utf-8")

    return {
        "target_count": logical_target_count,
        "dump_target_variant_count": len(targets),
        "protected_hint_count": protected_hint_count,
        "pre_dump_recommended": bool(protected_hint_count),
        "runtime_dump_targets_enabled": write_targets,
        "archive_count": len(indexed_archives),
        "archives": indexed_archives,
        "failed_archives": failed_archives,
        "target_file": str(out_path) if out_path else "",
        "explicit_target_file": str(explicit_path) if explicit_path else "",
    }


def import_kirikiri_external_dump(game_dir: Path, source_dir: Path) -> dict[str, object]:
    """Import script resources dumped by external KiriKiri file-stream tools.

    Tools such as KrkrDump dump whatever the engine opens from XP3/storage
    streams. This normalizes their output into the pipeline's canonical
    _translation_meta/kirikiri_dump folder, without treating images/audio as
    translatable scripts.
    """
    game_dir = Path(game_dir)
    source_dir = Path(source_dir)
    dump_dir = game_dir / "_translation_meta" / "kirikiri_dump"
    result: dict[str, object] = {
        "source_dir": str(source_dir),
        "dump_dir": str(dump_dir),
        "scanned_files": 0,
        "imported_files": 0,
        "skipped_files": 0,
        "duplicates": 0,
        "script_files": [],
    }
    if not source_dir.is_dir():
        return result

    xp3_stems = {p.stem.lower() for p in game_dir.glob("*.xp3")}
    existing: set[str] = set()
    for path in dump_dir.rglob("*") if dump_dir.is_dir() else []:
        if path.is_file():
            try:
                existing.add(path.relative_to(dump_dir).as_posix().lower())
            except ValueError:
                pass

    imported: list[str] = []
    scanned = skipped = duplicates = 0
    for src in sorted(source_dir.rglob("*"), key=lambda p: str(p).lower()):
        if not src.is_file():
            continue
        scanned += 1
        rel_candidates = _external_dump_rel_candidates(src, source_dir, xp3_stems)
        if not rel_candidates or not _is_external_dump_script_candidate(src):
            skipped += 1
            continue
        copied_this_file = False
        for rel in rel_candidates:
            if rel.lower() in existing:
                duplicates += 1
                continue
            dst = _safe_output_path(dump_dir, rel)
            if dst is None:
                skipped += 1
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            except OSError as exc:
                warning(f"KiriKiri external dump import failed: {rel} - {exc}")
                skipped += 1
                continue
            existing.add(rel.lower())
            imported.append(rel)
            copied_this_file = True
        if not copied_this_file and rel_candidates:
            duplicates += 1

    if imported:
        _append_kirikiri_dump_targets_from_names(game_dir, set(imported))
        _write_kirikiri_explicit_dump_targets(game_dir, set(imported))
    result.update({
        "scanned_files": scanned,
        "imported_files": len(imported),
        "skipped_files": skipped,
        "duplicates": duplicates,
        "script_files": imported[:300],
    })
    return result


def _external_dump_rel_candidates(src: Path, source_dir: Path, xp3_stems: set[str]) -> list[str]:
    try:
        rel = src.relative_to(source_dir).as_posix()
    except ValueError:
        rel = src.name
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel or rel.startswith("../") or "/../" in rel:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        clean = value.replace("\\", "/").lstrip("/")
        if clean and clean not in seen and not clean.startswith("../") and "/../" not in clean:
            seen.add(clean)
            out.append(clean)

    parts = rel.split("/")
    if len(parts) > 1 and parts[0].lower() in xp3_stems:
        add("/".join(parts[1:]))
    else:
        add(rel)
    return out


def _is_external_dump_script_candidate(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix not in {".ks", ".tjs", ".scn", ""}:
        return False
    try:
        if path.stat().st_size <= 0 or path.stat().st_size > 8 * 1024 * 1024:
            return False
        data = path.read_bytes()
    except OSError:
        return False
    if suffix == ".scn" or is_kirikiri_scn(data):
        return True
    if suffix in {".ks", ".tjs"}:
        return _decode_script_bytes_for_quality(data) is not None
    return _is_probably_scriptless_kirikiri_payload(path)


def _append_kirikiri_dump_targets_from_entries(
    xp3_path: Path,
    game_dir: Path | None,
    entries: list[_Xp3Entry] | tuple[_Xp3Entry, ...],
) -> Path | None:
    if game_dir is None:
        return None
    targets: set[str] = set()
    for entry in entries:
        if Path(entry.name).name.lower().startswith("startup"):
            continue
        if not _xp3_entry_needs_runtime_dump(xp3_path, entry):
            continue
        rel = entry.name.replace("\\", "/")
        targets.add(rel)
        targets.add(f"{xp3_path.name}>{rel}")
    if not targets:
        return None
    meta = game_dir / "_translation_meta"
    meta.mkdir(parents=True, exist_ok=True)
    out = meta / "kirikiri_dump_targets.txt"
    existing: set[str] = set()
    if out.exists():
        try:
            existing = {
                line.strip()
                for line in out.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }
        except OSError:
            existing = set()
    merged = sorted(existing | targets, key=str.lower)
    out.write_text("\n".join(merged) + ("\n" if merged else ""), encoding="utf-8")
    debug(f"KiriKiri dump targets updated from {xp3_path.name}: {len(merged)} entries")
    return out


def _expand_kirikiri_storage_refs(names: list[str] | tuple[str, ...] | set[str]) -> set[str]:
    targets: set[str] = set()
    for raw in names:
        name = str(raw).strip().replace("\\", "/")
        if not name or name.startswith(("#", ";")):
            continue
        lowered = name.lower()
        if lowered.startswith(("http://", "https://")):
            continue
        targets.add(name)
        if lowered.endswith(".ks"):
            targets.add(name + ".scn")
        elif lowered.endswith(".scn") and not lowered.endswith(".ks.scn"):
            targets.add(name[:-4] + ".ks.scn")
    return targets


def _append_kirikiri_dump_targets_from_names(
    game_dir: Path | None,
    names: list[str] | tuple[str, ...] | set[str],
) -> Path | None:
    if game_dir is None:
        return None
    targets = {name for name in names if str(name).strip()}
    if not targets:
        return None
    meta = game_dir / "_translation_meta"
    meta.mkdir(parents=True, exist_ok=True)
    out = meta / "kirikiri_dump_targets.txt"
    existing: set[str] = set()
    if out.exists():
        try:
            existing = {
                line.strip()
                for line in out.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }
        except OSError:
            existing = set()
    merged = sorted(existing | targets, key=str.lower)
    out.write_text("\n".join(merged) + ("\n" if merged else ""), encoding="utf-8")
    if targets - existing:
        debug(f"KiriKiri dump targets extended from SCN refs: +{len(targets - existing)}")
    return out


def _write_kirikiri_explicit_dump_targets(game_dir: Path | None, names: set[str]) -> Path | None:
    if game_dir is None or not names:
        return None
    meta = game_dir / "_translation_meta"
    meta.mkdir(parents=True, exist_ok=True)
    out = meta / "kirikiri_explicit_dump_targets.txt"
    existing: set[str] = set()
    if out.exists():
        try:
            existing = {
                line.strip()
                for line in out.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }
        except OSError:
            existing = set()
    merged = sorted(existing | {name for name in names if str(name).strip()}, key=str.lower)
    out.write_text("\n".join(merged) + ("\n" if merged else ""), encoding="utf-8")
    return out
