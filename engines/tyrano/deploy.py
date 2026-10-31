"""Transactional deployment for already-staged TyranoScript outputs."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath

def _digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _relative_path(value: object) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("Tyrano output path must be a relative path")
    name = os.fspath(value).replace("\\", "/")
    posix = PurePosixPath(name)
    windows = PureWindowsPath(name)
    if (
        not name
        or not posix.parts
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in (".", "..") or ":" in part for part in posix.parts)
    ):
        raise ValueError(f"Unsafe Tyrano output path: {name!r}")
    return Path(*posix.parts)


def _key(path: Path) -> str:
    return path.as_posix().casefold()


def _checked_target(game_root: Path, rel: Path) -> Path:
    current = game_root
    for part in rel.parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError(f"Tyrano output uses a link: {rel}")
    target = current.resolve(strict=True)
    if not target.is_relative_to(game_root) or not target.is_file():
        raise ValueError(f"Tyrano output is not an existing game file: {rel}")
    return target


def _new_temp(parent: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=".engaixt-tyrano-", suffix=".tmp", dir=parent)
    os.close(fd)
    return Path(name)


def _copy_verified(source: Path, dest: Path, expected: tuple[int, str]) -> None:
    with source.open("rb") as src, dest.open("wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    shutil.copystat(source, dest)
    if _digest(dest) != expected:
        raise OSError(f"Tyrano staged file verification failed: {dest}")


def _manifest_record(manifest, target: Path) -> dict | None:
    target_key = os.path.normcase(str(target))
    for record in manifest.data.get("modified_files", []):
        if os.path.normcase(str(Path(str(record.get("path") or "")).resolve())) == target_key:
            return record
    return None


def _ensure_manifest_backup(manifest, game_root: Path, target: Path, original: tuple[int, str]) -> None:
    if manifest is None or not callable(getattr(manifest, "backup_file", None)):
        raise OSError("Tyrano deployment requires a game manifest backup")
    if Path(manifest.game_dir).resolve() != game_root:
        raise ValueError("Tyrano manifest belongs to a different game")

    record = _manifest_record(manifest, target)
    old_hash = str(record.get("original_sha256") or "") if record else ""
    backup_root = Path(manifest.backup_root).resolve()
    expected_path = (backup_root / target.relative_to(game_root)).resolve()
    if not expected_path.is_relative_to(backup_root):
        raise ValueError(f"Unsafe Tyrano manifest backup path: {target}")

    existed = expected_path.exists()
    if existed:
        if not expected_path.is_file():
            raise OSError(f"Tyrano backup is not a file: {expected_path}")
        backup_digest = _digest(expected_path)
        if old_hash:
            if backup_digest[1] != old_hash:
                raise OSError(f"Tyrano existing backup failed verification: {target}")
        elif backup_digest != original:
            raise OSError(f"Tyrano unrecorded backup does not match game file: {target}")
    elif old_hash:
        raise OSError(f"Tyrano original backup is missing: {target}")

    try:
        backup = manifest.backup_file(target)
        if backup is None or Path(backup).resolve() != expected_path or not expected_path.is_file():
            raise OSError(f"Cannot back up Tyrano game file: {target}")
        backup_digest = _digest(expected_path)
        if (old_hash and backup_digest[1] != old_hash) or (not old_hash and backup_digest != original):
            raise OSError(f"Tyrano backup verification failed: {target}")
    except Exception:
        if not existed:
            expected_path.unlink(missing_ok=True)
        raise


def deploy_tyrano_outputs(
    game_dir: Path,
    workspace: Path,
    items: list,
    staged_outputs,
    manifest,
) -> dict:
    """Commit every changed target or restore the exact pre-deployment files.

    ``staged_outputs`` maps game-relative names to completed files in the
    workspace. Originals are never touched until all paths, staging copies,
    current-run snapshots and persistent manifest backups have been verified.
    """
    changed = [item for item in items or [] if item.translated and item.translated != item.original]
    if not changed:
        return {"files": 0}
    if not isinstance(staged_outputs, Mapping) or not staged_outputs:
        raise ValueError("Missing staged Tyrano outputs")

    game_root = Path(game_dir).resolve(strict=True)
    workspace_root = Path(workspace).resolve(strict=True)
    expected = {_key(_relative_path(item.file)) for item in changed}
    outputs = {}
    for raw_rel, raw_source in staged_outputs.items():
        rel = _relative_path(raw_rel)
        key = _key(rel)
        if key in outputs:
            raise ValueError(f"Duplicate Tyrano output target: {rel}")
        target = _checked_target(game_root, rel)
        source_path = Path(raw_source)
        if not source_path.is_absolute():
            raise ValueError(f"Tyrano staged file must be absolute: {source_path}")
        source = source_path.resolve(strict=True)
        if not source.is_file() or not source.is_relative_to(workspace_root):
            raise ValueError(f"Unsafe Tyrano staged file: {source_path}")
        outputs[key] = (target, source)
    if set(outputs) != expected:
        raise ValueError("Tyrano staged outputs do not match changed game files")
    if {target for target, _source in outputs.values()} & {
        source for _target, source in outputs.values()
    }:
        raise ValueError("A Tyrano staged file cannot also be a deployment target")

    transaction = Path(tempfile.mkdtemp(prefix=".tyrano-transaction-", dir=workspace_root))
    temporary: list[Path] = []
    committed: list[tuple[Path, Path, tuple[int, str]]] = []
    preserve_recovery = False
    try:
        prepared = []
        for index, (target, source) in enumerate(outputs.values()):
            original_digest = _digest(target)
            snapshot = transaction / f"{index}.original"
            _copy_verified(target, snapshot, original_digest)
            if _digest(target) != original_digest:
                raise OSError(f"Tyrano game file changed during snapshot: {target}")
            staged_digest = _digest(source)
            temp = _new_temp(target.parent)
            temporary.append(temp)
            _copy_verified(source, temp, staged_digest)
            prepared.append((target, snapshot, original_digest, temp, staged_digest))

        for target, _snapshot, original_digest, _temp, _staged_digest in prepared:
            _ensure_manifest_backup(manifest, game_root, target, original_digest)

        for target, snapshot, original_digest, temp, staged_digest in prepared:
            if _digest(target) != original_digest:
                raise OSError(f"Tyrano game file changed before deployment: {target}")
            os.replace(temp, target)
            committed.append((target, snapshot, original_digest))
            if _digest(target) != staged_digest:
                raise OSError(f"Tyrano deployed file verification failed: {target}")

        for target, _snapshot, _original_digest in committed:
            manifest.record_modified(target)
        return {"files": len(committed)}
    except Exception as exc:
        restore_errors = []
        for target, snapshot, original_digest in reversed(committed):
            rollback_temp = None
            try:
                rollback_temp = _new_temp(target.parent)
                temporary.append(rollback_temp)
                _copy_verified(snapshot, rollback_temp, original_digest)
                os.replace(rollback_temp, target)
                if _digest(target) != original_digest:
                    raise OSError("restored bytes do not match snapshot")
            except Exception as restore_exc:
                restore_errors.append(f"{target}: {restore_exc}")
        if restore_errors:
            preserve_recovery = True
            raise OSError(
                f"Tyrano deployment failed: {exc}; restoration failed: "
                f"{'; '.join(restore_errors)}; snapshots retained at {transaction}"
            ) from exc
        raise
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
        if not preserve_recovery:
            shutil.rmtree(transaction, ignore_errors=True)
