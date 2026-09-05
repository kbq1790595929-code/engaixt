"""Isolated adapters for optional KRKR static tools.

The pipeline owns the ``TextItem`` model and never lets an external tool write
into the game directory.  This module only runs a discovered sidecar in a
temporary workspace, records facts about the attempt, and promotes validated
script files explicitly at the call site.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from core.tool_manager import find_tool, get_tool_spec
from engines.kirikiri.codec import _read_text_guess
from utils.kirikiri_psb import is_kirikiri_scn
from utils.logger import debug, warning


SCRIPT_SUFFIXES = frozenset({".ks", ".tjs", ".scn"})


def _safe_relative_path(value: object) -> str:
    """Return a normalized relative path, or an empty string if unsafe."""
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ":" in raw.split("/", 1)[0]:
        return ""
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


@dataclass(frozen=True)
class ExternalToolResult:
    """Facts from one sidecar invocation; return code alone is never success."""

    tool: str
    path: str = ""
    version: str = ""
    input_path: str = ""
    output_path: str = ""
    command: tuple[str, ...] = ()
    status: str = "unavailable"
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    generated_files: tuple[str, ...] = ()
    script_files: tuple[str, ...] = ()
    detail: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "success" and bool(self.script_files or self.generated_files)

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "path": self.path,
            "version": self.version,
            "input_path": self.input_path,
            "output_path": self.output_path,
            "command": list(self.command),
            "status": self.status,
            "returncode": self.returncode,
            "stdout": self.stdout[:500],
            "stderr": self.stderr[:500],
            "generated_files": list(self.generated_files),
            "script_files": list(self.script_files),
            "detail": self.detail,
            **self.metadata,
        }


def _relative_files(root: Path) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    result: list[Path] = []
    for path in root.rglob("*"):
        if path.is_file():
            try:
                result.append(path.relative_to(root))
            except ValueError:
                continue
    return tuple(sorted(result, key=lambda value: str(value).casefold()))


def _script_files(root: Path) -> tuple[Path, ...]:
    return tuple(
        rel for rel in _relative_files(root)
        if rel.suffix.lower() in SCRIPT_SUFFIXES or not rel.suffix
    )


def _script_outputs_are_readable(root: Path, scripts: Iterable[Path]) -> bool:
    for rel in scripts:
        path = Path(root) / rel
        try:
            data = path.read_bytes()
        except OSError:
            return False
        if not data:
            return False
        suffix = path.suffix.lower()
        if suffix == ".scn":
            if not is_kirikiri_scn(data):
                return False
        elif suffix == ".tjs":
            if not data.startswith(b"TJS2100\x00") and _read_text_guess(path) is None:
                return False
        elif _read_text_guess(path) is None:
            return False
    return True


def _safe_summary(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()[:500]
    return str(value).strip()[:500]


def _tool_version(name: str) -> str:
    spec = get_tool_spec(name)
    return str(getattr(spec, "version", "") or "unknown") if spec else "unknown"


def _unavailable(name: str, input_path: Path, output_path: Path, detail: str = "") -> ExternalToolResult:
    return ExternalToolResult(
        tool=name,
        version=_tool_version(name),
        input_path=str(input_path),
        output_path=str(output_path),
        status="unavailable",
        detail=detail or f"{name} not found",
    )


def _run_sidecar(
    name: str,
    args: Iterable[str | Path],
    *,
    input_path: Path,
    output_path: Path,
    timeout: int = 300,
    cwd: Path | None = None,
    script_root: Path | None = None,
    require_scripts: bool = True,
    output_is_file: bool = False,
) -> ExternalToolResult:
    """Run one sidecar without shell expansion and inspect its output."""
    tool = find_tool(name)
    if not tool:
        return _unavailable(name, input_path, output_path)

    # Every caller passes a disposable candidate directory. Clearing it avoids
    # stale files from a previous attempt satisfying the output contract.
    if output_path.exists() and output_is_file:
        try:
            if output_path.is_dir():
                shutil.rmtree(output_path)
            else:
                output_path.unlink()
        except OSError as exc:
            return ExternalToolResult(
                tool=name,
                path=str(tool),
                version=_tool_version(name),
                input_path=str(input_path),
                output_path=str(output_path),
                status="error",
                detail=f"cannot prepare isolated output file: {exc}",
            )
    elif output_path.exists():
        try:
            if output_path.is_dir():
                shutil.rmtree(output_path)
            else:
                output_path.unlink()
        except OSError as exc:
            return ExternalToolResult(
                tool=name,
                path=str(tool),
                version=_tool_version(name),
                input_path=str(input_path),
                output_path=str(output_path),
                status="error",
                detail=f"cannot prepare isolated output directory: {exc}",
            )
    if output_is_file:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_path.mkdir(parents=True, exist_ok=True)
    command = tuple([str(tool), *(str(arg) for arg in args)])
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd or output_path),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        warning(f"KRKR 外部工具超时: {name}")
        return ExternalToolResult(
            tool=name,
            path=str(tool),
            version=_tool_version(name),
            input_path=str(input_path),
            output_path=str(output_path),
            command=command,
            status="timeout",
            detail=str(exc),
        )
    except OSError as exc:
        debug(f"KRKR 外部工具启动失败 {name}: {exc}")
        return ExternalToolResult(
            tool=name,
            path=str(tool),
            version=_tool_version(name),
            input_path=str(input_path),
            output_path=str(output_path),
            command=command,
            status="error",
            detail=str(exc),
        )

    if output_is_file:
        files = (Path(output_path.name),) if output_path.is_file() else ()
        scripts = files if output_path.is_file() and output_path.suffix.lower() in SCRIPT_SUFFIXES else ()
    else:
        files = _relative_files(output_path)
        scripts = _script_files(script_root or output_path)
    if scripts:
        if not _script_outputs_are_readable(script_root or output_path, scripts):
            status = "invalid_script_output"
            detail = f"generated {len(scripts)} script files but at least one is not readable"
        else:
            status = "success"
            detail = f"generated {len(scripts)} script files"
    elif files and not require_scripts:
        status = "success"
        detail = f"generated {len(files)} output files"
    elif files:
        status = "no_script_output"
        detail = f"generated {len(files)} files but no readable script files"
    elif completed.returncode != 0:
        status = "failed"
        detail = f"returncode={completed.returncode}"
    else:
        status = "no_output"
        detail = "returncode=0 but output contract was not met"

    return ExternalToolResult(
        tool=name,
        path=str(tool),
        version=_tool_version(name),
        input_path=str(input_path),
        output_path=str(output_path),
        command=command,
        status=status,
        returncode=completed.returncode,
        stdout=_safe_summary(completed.stdout),
        stderr=_safe_summary(completed.stderr),
        generated_files=tuple(str(rel).replace("\\", "/") for rel in files),
        script_files=tuple(str(rel).replace("\\", "/") for rel in scripts),
        detail=detail,
    )


def run_msg_tool_unpack(archive: Path, output_dir: Path, *, timeout: int = 300) -> ExternalToolResult:
    """Run ``msg-tool unpack`` and require actual files, not just rc=0."""
    archive = Path(archive)
    output_dir = Path(output_dir)
    return _run_sidecar(
        "msg_tool",
        ("unpack", archive, output_dir),
        input_path=archive,
        output_path=output_dir,
        timeout=timeout,
        cwd=output_dir,
    )


def run_vntextpatch_export(
    input_dir: Path,
    output_dir: Path,
    *,
    format_name: str = "kirikiriks",
    timeout: int = 300,
) -> ExternalToolResult:
    """Run VNTextPatch's folder export in an isolated output directory."""
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    tool = find_tool("vntextpatch")
    if not tool:
        return _unavailable("vntextpatch", input_dir, output_dir)
    return _run_sidecar(
        "vntextpatch",
        (f"--format={format_name}", "extractlocal", input_dir, output_dir),
        input_path=input_dir,
        output_path=output_dir,
        timeout=timeout,
        cwd=output_dir,
        script_root=output_dir,
        require_scripts=False,
    )


def run_vntextpatch_import(
    input_dir: Path,
    translation_dir: Path,
    output_dir: Path,
    *,
    format_name: str = "kirikiriks",
    timeout: int = 300,
) -> ExternalToolResult:
    """Run VNTextPatch import; the caller must validate the copied output."""
    input_dir = Path(input_dir)
    translation_dir = Path(translation_dir)
    output_dir = Path(output_dir)
    tool = find_tool("vntextpatch")
    if not tool:
        return _unavailable("vntextpatch", input_dir, output_dir)
    return _run_sidecar(
        "vntextpatch",
        (f"--format={format_name}", "insertlocal", input_dir, translation_dir, output_dir),
        input_path=input_dir,
        output_path=output_dir,
        timeout=timeout,
        cwd=output_dir.parent,
        script_root=output_dir.parent,
        require_scripts=True,
        output_is_file=True,
    )


def run_xp3pack(
    input_dir: Path,
    output_archive: Path,
    *,
    timeout: int = 300,
) -> ExternalToolResult:
    """Run KirikiriTools' ``Xp3Pack <folder>`` in an isolated directory.

    Xp3Pack always writes ``<folder>.xp3`` beside the input folder. The caller
    therefore supplies a disposable folder name and receives a copied archive
    at the requested path only after the process produced one.
    """
    input_dir = Path(input_dir)
    output_archive = Path(output_archive)
    tool = find_tool("kirikiri_xp3pack")
    if not tool:
        return _unavailable("kirikiri_xp3pack", input_dir, output_archive)
    if not input_dir.is_dir():
        return ExternalToolResult(
            tool="kirikiri_xp3pack",
            path=str(tool),
            version=_tool_version("kirikiri_xp3pack"),
            input_path=str(input_dir),
            output_path=str(output_archive),
            status="invalid_input",
            detail="input folder does not exist",
        )

    generated = input_dir.parent / f"{input_dir.name}.xp3"
    try:
        generated.unlink(missing_ok=True)
        command = (str(tool), input_dir.name)
        completed = subprocess.run(
            list(command),
            cwd=str(input_dir.parent),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return ExternalToolResult(
            tool="kirikiri_xp3pack",
            path=str(tool),
            version=_tool_version("kirikiri_xp3pack"),
            input_path=str(input_dir),
            output_path=str(output_archive),
            command=command if "command" in locals() else (str(tool), input_dir.name),
            status="timeout",
            detail=str(exc),
        )
    except OSError as exc:
        return ExternalToolResult(
            tool="kirikiri_xp3pack",
            path=str(tool),
            version=_tool_version("kirikiri_xp3pack"),
            input_path=str(input_dir),
            output_path=str(output_archive),
            command=(str(tool), input_dir.name),
            status="error",
            detail=str(exc),
        )

    if not generated.is_file():
        status = "failed" if completed.returncode else "no_output"
        detail = (
            f"returncode={completed.returncode}"
            if completed.returncode
            else "returncode=0 but Xp3Pack did not create an archive"
        )
        return ExternalToolResult(
            tool="kirikiri_xp3pack",
            path=str(tool),
            version=_tool_version("kirikiri_xp3pack"),
            input_path=str(input_dir),
            output_path=str(output_archive),
            command=(str(tool), input_dir.name),
            status=status,
            returncode=completed.returncode,
            stdout=_safe_summary(completed.stdout),
            stderr=_safe_summary(completed.stderr),
            detail=detail,
        )

    try:
        output_archive.parent.mkdir(parents=True, exist_ok=True)
        if generated.resolve() != output_archive.resolve():
            shutil.copy2(generated, output_archive)
        generated_files = (output_archive.name,)
    except OSError as exc:
        return ExternalToolResult(
            tool="kirikiri_xp3pack",
            path=str(tool),
            version=_tool_version("kirikiri_xp3pack"),
            input_path=str(input_dir),
            output_path=str(output_archive),
            command=(str(tool), input_dir.name),
            status="error",
            returncode=completed.returncode,
            stdout=_safe_summary(completed.stdout),
            stderr=_safe_summary(completed.stderr),
            detail=f"archive promotion failed: {exc}",
        )
    return ExternalToolResult(
        tool="kirikiri_xp3pack",
        path=str(tool),
        version=_tool_version("kirikiri_xp3pack"),
        input_path=str(input_dir),
        output_path=str(output_archive),
        command=(str(tool), input_dir.name),
        status="success" if completed.returncode == 0 else "failed",
        returncode=completed.returncode,
        stdout=_safe_summary(completed.stdout),
        stderr=_safe_summary(completed.stderr),
        generated_files=generated_files,
        detail="archive generated" if completed.returncode == 0 else f"returncode={completed.returncode}",
    )


def find_vntextpatch_json(export_dir: Path, source_rel: str) -> Path | None:
    """Find the JSON export corresponding to one safe source script path."""
    export_dir = Path(export_dir)
    safe_rel = _safe_relative_path(source_rel)
    if not safe_rel:
        return None
    source = Path(safe_rel)
    candidates = (
        export_dir / source.with_suffix(".json"),
        export_dir / Path(f"{safe_rel}.json"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    parent = export_dir / source.parent
    if parent.is_dir():
        for candidate in sorted(parent.glob("*.json"), key=lambda p: p.name.casefold()):
            if candidate.stem.casefold() in {source.stem.casefold(), source.name.casefold()}:
                return candidate
    return None


def write_vntextpatch_translation_json(
    source_json: Path,
    output_json: Path,
    translations: Iterable[object],
) -> int:
    """Create one VNTextPatch JSON file with matching translated fields.

    VNTextPatch's JSON format is deliberately position-oriented. Matching by
    role and source text keeps duplicate lines deterministic while preserving
    every unselected name/message from the original export.
    """
    try:
        data = json.loads(Path(source_json).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return 0
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("items", data.get("entries", data.get("lines", [])))
    else:
        rows = []
    if not isinstance(rows, list):
        return 0

    queues: dict[tuple[str, str], list[str]] = {}
    for item in translations:
        original = str(getattr(item, "original", "") or "")
        translated = str(getattr(item, "translated", "") or "")
        if not original or not translated or original == translated:
            continue
        role = str(getattr(item, "context", "message") or "message").lower()
        role = "speaker" if role in {"speaker", "name", "character", "charactername"} else "message"
        queues.setdefault((role, original), []).append(translated)

    changed = 0

    def replace_value(value: object, role: str) -> object:
        nonlocal changed
        if not isinstance(value, str) or not value:
            return value
        queue = queues.get((role, value))
        if not queue:
            return value
        changed += 1
        return queue.pop(0)

    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("name", "Name"):
            if key in row:
                row[key] = replace_value(row[key], "speaker")
        for key in ("names", "Names"):
            if isinstance(row.get(key), list):
                row[key] = [replace_value(value, "speaker") for value in row[key]]
        for key in ("message", "Message", "text", "Text", "original", "Original"):
            if key in row:
                row[key] = replace_value(row[key], "message")

    if not changed:
        return 0
    try:
        output_json = Path(output_json)
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return 0
    return changed


def load_vntextpatch_json_items(output_dir: Path, source_root: Path) -> list[dict]:
    """Read common VNTextPatch JSON shapes into plain records.

    This is intentionally conservative. Unknown JSON is ignored and the
    native parser remains authoritative, so an external export can never
    inject guessed keys into the normal patch path.
    """
    output_dir = Path(output_dir)
    source_root = Path(source_root)
    result: list[dict] = []
    for json_path in sorted(output_dir.rglob("*.json"), key=lambda p: str(p).casefold()):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("items", data.get("entries", data.get("lines", [])))
        else:
            rows = []
        if not isinstance(rows, list):
            continue
        json_rel = json_path.relative_to(output_dir)
        source_rel = _find_source_for_json(json_rel, source_root)
        rel_name = source_rel or _safe_relative_path(str(json_rel.with_suffix(".ks")))
        if not rel_name:
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            file_name = _safe_relative_path(row.get("file", row.get("File", rel_name))) or rel_name
            base_key = str(row.get("key", row.get("Key", "")) or "")
            source_json = str(json_rel).replace("\\", "/")
            line = int(row.get("line", row.get("Line", 0)) or 0)
            meta = {"external_tool": "vntextpatch", "source_json": source_json}

            names = row.get("names", row.get("Names"))
            if names is None:
                names = [row.get("name", row.get("Name"))]
            elif isinstance(names, str):
                names = [names]
            if isinstance(names, list):
                for name_index, name in enumerate(names):
                    if not isinstance(name, str) or not name.strip():
                        continue
                    translated_name = row.get("translated_names", row.get("TranslatedNames", []))
                    if isinstance(translated_name, list) and name_index < len(translated_name):
                        name_translation = translated_name[name_index]
                    else:
                        name_translation = row.get("translated_name", row.get("TranslatedName", ""))
                    result.append({
                        "file": file_name,
                        "key": f"{base_key}:name:{name_index}" if base_key else f"name:{name_index}",
                        "original": name,
                        "translated": str(name_translation or ""),
                        "context": "speaker",
                        "line": line,
                        "meta": {**meta, "role": "speaker"},
                    })

            original = row.get(
                "original",
                row.get("Original", row.get("message", row.get("Message", row.get("text", row.get("Text", "")))))
            )
            if not isinstance(original, str) or not original.strip():
                continue
            translated = row.get(
                "translated",
                row.get("Translation", row.get("translation", row.get("Translated", ""))),
            )
            result.append({
                "file": file_name,
                "key": base_key or "message",
                "original": original,
                "translated": str(translated or ""),
                "context": "message",
                "line": line,
                "meta": {**meta, "role": "text"},
            })
    return result


def _find_source_for_json(json_rel: Path, source_root: Path) -> str:
    """Map VNTextPatch's ``foo.json`` back to a safe source script path."""
    source_root = Path(source_root)
    candidates = [json_rel.with_suffix("")]
    if json_rel.suffix.lower() == ".json":
        for suffix in sorted(SCRIPT_SUFFIXES):
            candidates.append(json_rel.with_suffix(suffix))
    for candidate in candidates:
        safe = _safe_relative_path(str(candidate))
        if safe and (source_root / safe).is_file():
            return safe
    parent = source_root / json_rel.parent
    stem = json_rel.stem.casefold()
    if parent.is_dir():
        for candidate in sorted(parent.iterdir(), key=lambda p: p.name.casefold()):
            if candidate.is_file() and candidate.suffix.lower() in SCRIPT_SUFFIXES and candidate.stem.casefold() == stem:
                return _safe_relative_path(str(candidate.relative_to(source_root)))
    return ""


def promote_script_files(source_dir: Path, target_dir: Path, script_files: Iterable[str]) -> int:
    """Copy only validated script outputs into the engine's isolated source root."""
    source_dir = Path(source_dir).resolve()
    target_dir = Path(target_dir).resolve()
    count = 0
    for rel_name in script_files:
        safe_rel = _safe_relative_path(rel_name)
        if not safe_rel:
            continue
        source = (source_dir / safe_rel).resolve()
        try:
            source.relative_to(source_dir)
        except ValueError:
            continue
        if not source.is_file() or source.suffix.lower() not in SCRIPT_SUFFIXES and source.suffix:
            continue
        target = (target_dir / safe_rel).resolve()
        try:
            target.relative_to(target_dir)
        except ValueError:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        count += 1
    return count
