from __future__ import annotations

import ctypes
import hashlib
import json
import shutil
import struct
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from core.tool_manager import find_tool
from engines.wolf.detect import archive_files, find_loose_data_root, find_wolf_exe
from engines.wolf.native_bridge import (
    bridge_path as native_bridge_path,
    is_available as native_bridge_available,
    pack as native_pack,
    unpack as native_unpack,
)
from engines.wolf.profile_store import (
    lookup_verified_profile,
    lookup_verified_profile_by_id,
    temporary_key_file,
)
from engines.wolf.runtime_keys import (
    snapshot_custom_database_keys,
    verify_custom_database_keys,
)


@dataclass
class WolfState:
    game_dir: str
    data_root: str
    json_root: str
    staged_root: str = ""
    staged_exe: str = ""
    uberwolf: str = ""
    archives: list[str] = field(default_factory=list)
    pack_index: int = 7
    uberwolf_release: str = ""
    uberwolf_sha256: str = ""
    native_archive: str = ""
    native_crypt_version: int = 0
    native_key_id: str = ""
    native_profile_validation: str = ""
    native_bridge_sha256: str = ""
    runtime_key_count: int = 0
    runtime_key_fingerprint: str = ""
    bridge_source: str = "Sinflower/UberWolf@663dc2defaefc3073cc1178b488894a5aa1d4595"
    bridge_sha256: str = ""
    tool_runs: list[dict] = field(default_factory=list)

    @property
    def archived(self) -> bool:
        return bool(self.archives)


def prepare(game_path: Path, workspace: Path) -> WolfState:
    game_dir = game_path if game_path.is_dir() else game_path.parent
    wolf_root = workspace / "wolf"
    wolf_root.mkdir(parents=True, exist_ok=True)
    json_root = wolf_root / "json"
    _reset_dir(json_root)

    archives = archive_files(game_dir)
    loose_data = find_loose_data_root(game_dir)
    structured_archives = [
        archive for archive in archives
        if archive.stem.casefold() in {"data", "basicdata", "mapdata"}
    ]
    if archives and (structured_archives or loose_data is None):
        state = _prepare_archived_game(game_path, game_dir, wolf_root, archives)
    else:
        data_root = loose_data
        if data_root is None:
            raise RuntimeError("WOLF RPG 数据目录不完整：缺少 BasicData/Game.dat、CommonEvent.dat 或 MapData")
        state = WolfState(
            game_dir=str(game_dir.resolve()),
            data_root=str(data_root.resolve()),
            json_root=str(json_root.resolve()),
            bridge_sha256=_sha256(bridge_path()),
        )

    bridge = bridge_path()
    if not bridge.is_file():
        raise RuntimeError(f"缺少 WOLF 文本桥接器: {bridge}")
    result = run_hidden([str(bridge), "export", state.data_root, str(json_root)], timeout=300)
    state.tool_runs.append(result)
    if result["exit_code"] != 0:
        raise RuntimeError(f"UberWolf 结构化文本导出失败: {result['stderr'][-1000:]}")
    state.bridge_sha256 = _sha256(bridge)
    state.runtime_key_count, state.runtime_key_fingerprint = snapshot_custom_database_keys(json_root)
    _save_state(workspace, state)
    return state


def repack(
    workspace: Path,
    state: WolfState,
    post_apply: Callable[[Path], None] | None = None,
) -> dict:
    data_root = Path(state.data_root)
    json_root = Path(state.json_root)
    generated = workspace / "wolf" / "generated_data"
    _reset_dir(generated)
    bridge = bridge_path()

    apply_result = run_hidden(
        [str(bridge), "apply", str(data_root), str(json_root), str(generated)],
        timeout=600,
    )
    state.tool_runs.append(apply_result)
    if apply_result["exit_code"] != 0:
        raise RuntimeError(f"WOLF 结构化回填失败: {apply_result['stderr'][-1000:]}")

    if post_apply is not None:
        post_apply(generated)

    # Validate the freshly generated dataset before it can reach either the
    # staged archives or the game's normal manifest/copy-back path.
    verify_result = run_hidden([str(bridge), "verify", str(generated)], timeout=300)
    state.tool_runs.append(verify_result)
    if verify_result["exit_code"] != 0:
        raise RuntimeError(f"WOLF 回填后结构校验失败: {verify_result['stderr'][-1000:]}")
    runtime_key_verification = _verify_runtime_key_snapshot(
        workspace,
        state,
        generated,
        "generated",
    )

    output_root = workspace / "original"
    output_root.mkdir(parents=True, exist_ok=True)
    outputs: list[str] = []
    native_roundtrip_verified = False
    native_roundtrip_runtime_keys = {
        "checked": False,
        "count": 0,
        "fingerprint_match": True,
    }
    archive_roundtrip_verified = False
    archive_roundtrip_runtime_keys = {
        "checked": False,
        "count": 0,
        "fingerprint_match": True,
    }
    if state.archived:
        for source in generated.rglob("*"):
            if source.is_file():
                relative = source.relative_to(generated)
                target = data_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        if state.native_archive:
            pack_result, native_roundtrip_runtime_keys = _native_pack_and_verify(
                workspace,
                state,
                data_root,
            )
            native_roundtrip_verified = True
            archive_roundtrip_verified = True
            archive_roundtrip_runtime_keys = native_roundtrip_runtime_keys
        else:
            pack_result = run_hidden(
                [state.uberwolf, state.staged_exe, "--override", "--pack", str(state.pack_index)],
                timeout=1800,
            )
            state.tool_runs.append(pack_result)
            if pack_result["exit_code"] != 0:
                raise RuntimeError(f"UberWolf 重打包失败: {pack_result['stderr'][-1000:]}")
            archive_roundtrip_runtime_keys = _standard_pack_and_verify(workspace, state)
            archive_roundtrip_verified = True
        staged_root = Path(state.staged_root)
        for rel in state.archives:
            source = staged_root / rel
            if not source.is_file():
                raise RuntimeError(f"UberWolf 未生成预期归档: {rel}")
            destination = output_root / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            outputs.append(rel)
    else:
        game_dir = Path(state.game_dir)
        data_prefix = Path(state.data_root).relative_to(game_dir)
        for source in generated.rglob("*"):
            if not source.is_file():
                continue
            rel = (data_prefix / source.relative_to(generated)).as_posix()
            destination = output_root / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            outputs.append(rel)

    result = {
        "checked": True,
        "bridge_verified": True,
        "archives_repacked": len(state.archives) if state.archived else 0,
        "native_roundtrip_verified": native_roundtrip_verified,
        "native_roundtrip_runtime_keys": native_roundtrip_runtime_keys,
        "archive_roundtrip_verified": archive_roundtrip_verified,
        "archive_roundtrip_runtime_keys": archive_roundtrip_runtime_keys,
        "runtime_keys": runtime_key_verification,
        "outputs": outputs,
        "tool_runs": state.tool_runs,
    }
    _save_state(workspace, state)
    (workspace / "wolf" / "repack_verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return result


def load_state(workspace: Path) -> WolfState:
    path = workspace / "wolf" / "state.json"
    if not path.is_file():
        raise RuntimeError("WOLF 工作区状态不存在，请先执行提取阶段")
    return WolfState(**json.loads(path.read_text(encoding="utf-8")))


def resolve_json_target(state: WolfState, json_rel: str) -> tuple[str, str]:
    data_root = Path(state.data_root)
    parts = Path(json_rel).parts
    if not parts:
        raise ValueError(f"Invalid WOLF JSON path: {json_rel}")
    category = parts[0]
    stem = Path(parts[-1]).stem
    if category == "game":
        source = data_root / "BasicData" / "Game.dat"
    elif category == "common_events":
        source = data_root / "BasicData" / "CommonEvent.dat"
    elif category == "databases":
        source = data_root / "BasicData" / f"{stem}.dat"
    elif category == "maps":
        matches = list(data_root.rglob(f"{stem}.mps"))
        if not matches:
            raise RuntimeError(f"找不到 WOLF 地图源文件: {stem}.mps")
        source = matches[0]
    else:
        raise ValueError(f"Unknown WOLF JSON category: {category}")

    source_rel = source.relative_to(Path(state.staged_root) if state.archived else Path(state.game_dir))
    if not state.archived:
        return source_rel.as_posix(), source_rel.as_posix()

    staged_root = Path(state.staged_root)
    for archive_rel in state.archives:
        extracted = (staged_root / archive_rel).with_suffix("")
        try:
            source.relative_to(extracted)
            return archive_rel.replace("\\", "/"), source_rel.as_posix()
        except ValueError:
            continue
    raise RuntimeError(f"无法把 WOLF 数据文件映射回原归档: {source}")


def resolve_script_target(state: WolfState, source: Path) -> tuple[str, str]:
    """Map an extracted Data/Script CSV back to its source file or archive."""
    data_root = Path(state.data_root).resolve()
    source = source.resolve()
    try:
        source.relative_to(data_root / "Script")
    except ValueError as exc:
        raise ValueError(f"WOLF Script CSV is outside Data/Script: {source}") from exc

    if not state.archived:
        source_rel = source.relative_to(Path(state.game_dir).resolve()).as_posix()
        return source_rel, source_rel

    staged_root = Path(state.staged_root).resolve()
    for archive_rel in state.archives:
        extracted = (staged_root / archive_rel).with_suffix("").resolve()
        try:
            source.relative_to(extracted)
            return archive_rel.replace("\\", "/"), source.relative_to(staged_root).as_posix()
        except ValueError:
            continue
    raise RuntimeError(f"无法把 WOLF Script CSV 映射回原归档: {source}")


def bridge_path() -> Path:
    return Path(__file__).resolve().parents[2] / "assets" / "wolf_runtime" / "wolf_text_bridge.exe"


def bundled_uberwolf_path() -> Path:
    return Path(__file__).resolve().parents[2] / "assets" / "wolf_runtime" / "UberWolfCli.exe"


def resolve_uberwolf_path() -> Path | None:
    """Resolve an installed or bundled CLI without downloading during a job."""
    managed = find_tool("uberwolf")
    if managed is not None:
        return managed
    bundled = bundled_uberwolf_path()
    return bundled if bundled.is_file() else None


def run_hidden(command: list[str], timeout: int) -> dict:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=str(Path(command[0]).parent),
        capture_output=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    return {
        "command": [str(part) for part in command],
        "exit_code": int(completed.returncode),
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "stdout": _decode_output(completed.stdout),
        "stderr": _decode_output(completed.stderr),
    }


def _prepare_archived_game(
    game_path: Path,
    game_dir: Path,
    wolf_root: Path,
    archives: list[Path],
) -> WolfState:
    uberwolf = resolve_uberwolf_path()
    if uberwolf is None:
        raise RuntimeError("缺少 UberWolfCli，无法解开 WOLF RPG 归档")
    exe = find_wolf_exe(game_path)
    if exe is None:
        raise RuntimeError("WOLF RPG 归档游戏缺少 Game.exe/GamePro.exe")

    staged_root = wolf_root / "staged_game"
    _reset_dir(staged_root)
    staged_exe = staged_root / exe.name
    shutil.copy2(exe, staged_exe)
    archive_rels: list[str] = []
    for archive in archives:
        rel = archive.relative_to(game_dir)
        destination = staged_root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, destination)
        archive_rels.append(rel.as_posix())

    unpack_result = run_hidden(
        [str(uberwolf), str(staged_exe), "--override", "--unprotect"],
        timeout=1800,
    )
    tool_runs = [unpack_result]
    native_archive = ""
    native_crypt_version = 0
    native_key_id = ""
    native_profile_validation = ""
    native_sha256 = ""
    if unpack_result["exit_code"] != 0:
        native_candidate = _native_pro_archive(archives)
        if native_candidate is None:
            raise RuntimeError(f"UberWolf 解包失败: {unpack_result['stderr'][-1000:]}")
        archive, crypt_version = native_candidate
        if not native_bridge_available():
            raise RuntimeError("WOLF Pro native bridge 缺失，提取在 AI 翻译前安全停止")
        profile = lookup_verified_profile(archive, crypt_version)
        if profile is None:
            raise RuntimeError(
                "WOLF Pro 归档没有匹配的已验证 profile；"
                "提取在 AI 翻译前安全停止，不会修改游戏资源"
            )
        native_archive = archive.relative_to(game_dir).as_posix()
        native_crypt_version = crypt_version
        native_key_id = profile.key_id
        native_profile_validation = profile.validation
        native_sha256 = _sha256(native_bridge_path())
        output_dir = (staged_root / native_archive).with_suffix("")
        _reset_dir(output_dir)
        with temporary_key_file(wolf_root, profile.key_hex) as key_file:
            native_result = native_unpack(
                staged_root / native_archive,
                key_file,
                crypt_version,
            )
        tool_runs.append(native_result)
        if native_result["exit_code"] != 0:
            raise RuntimeError(f"WOLF native Pro 解包失败: {native_result['stderr'][-1000:]}")
    data_root = find_loose_data_root(staged_root)
    if data_root is None:
        raise RuntimeError("UberWolf 已运行，但未找到解包后的 BasicData/MapData")

    state = WolfState(
        game_dir=str(game_dir.resolve()),
        data_root=str(data_root.resolve()),
        json_root=str((wolf_root / "json").resolve()),
        staged_root=str(staged_root.resolve()),
        staged_exe=str(staged_exe.resolve()),
        uberwolf=str(uberwolf.resolve()),
        archives=archive_rels,
        pack_index=_detect_pack_index(archives, exe),
        uberwolf_release="v0.6.3" if uberwolf.resolve() == bundled_uberwolf_path().resolve() else "managed",
        uberwolf_sha256=_sha256(uberwolf),
        native_archive=native_archive,
        native_crypt_version=native_crypt_version,
        native_key_id=native_key_id,
        native_profile_validation=native_profile_validation,
        native_bridge_sha256=native_sha256,
        bridge_sha256=_sha256(bridge_path()),
        tool_runs=tool_runs,
    )
    return state


def _native_pack_and_verify(
    workspace: Path,
    state: WolfState,
    data_root: Path,
) -> tuple[dict, dict[str, object]]:
    profile = lookup_verified_profile_by_id(state.native_key_id, state.native_crypt_version)
    if profile is None or profile.validation != state.native_profile_validation:
        raise RuntimeError("WOLF Pro 已验证 profile 不可用，拒绝重封归档")

    staged_archive = Path(state.staged_root) / state.native_archive
    roundtrip_root = workspace / "wolf" / "native_roundtrip"
    _reset_dir(roundtrip_root)
    with temporary_key_file(workspace / "wolf", profile.key_hex) as key_file:
        pack_result = native_pack(data_root, key_file, state.native_crypt_version)
        state.tool_runs.append(pack_result)
        if pack_result["exit_code"] != 0:
            raise RuntimeError(f"WOLF native 重打包失败: {pack_result['stderr'][-1000:]}")
        if not staged_archive.is_file():
            raise RuntimeError(f"WOLF native 未生成预期归档: {state.native_archive}")

        verification_archive = roundtrip_root / state.native_archive
        verification_archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged_archive, verification_archive)
        unpack_result = native_unpack(
            verification_archive,
            key_file,
            state.native_crypt_version,
        )
        state.tool_runs.append(unpack_result)
        if unpack_result["exit_code"] != 0:
            raise RuntimeError(f"WOLF native 重封包二次解包失败: {unpack_result['stderr'][-1000:]}")

    roundtrip_data = find_loose_data_root(roundtrip_root)
    if roundtrip_data is None:
        raise RuntimeError("WOLF native 重封包二次解包未生成结构化 Data 目录")
    bridge = bridge_path()
    verify_result = run_hidden([str(bridge), "verify", str(roundtrip_data)], timeout=300)
    state.tool_runs.append(verify_result)
    if verify_result["exit_code"] != 0:
        raise RuntimeError(f"WOLF native 重封包结构校验失败: {verify_result['stderr'][-1000:]}")
    runtime_keys = _verify_runtime_key_snapshot(
        workspace,
        state,
        roundtrip_data,
        "native_roundtrip",
    )
    return pack_result, runtime_keys


def _standard_pack_and_verify(
    workspace: Path,
    state: WolfState,
) -> dict[str, object]:
    roundtrip_root = workspace / "wolf" / "archive_roundtrip"
    _reset_dir(roundtrip_root)
    staged_root = Path(state.staged_root)
    verification_exe = roundtrip_root / Path(state.staged_exe).name
    shutil.copy2(state.staged_exe, verification_exe)
    for archive_rel in state.archives:
        source = staged_root / archive_rel
        if not source.is_file():
            raise RuntimeError(f"WOLF standard roundtrip archive missing: {archive_rel}")
        destination = roundtrip_root / archive_rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    unpack_result = run_hidden(
        [state.uberwolf, str(verification_exe), "--override", "--unprotect"],
        timeout=1800,
    )
    state.tool_runs.append(unpack_result)
    if unpack_result["exit_code"] != 0:
        raise RuntimeError(
            f"WOLF standard archive roundtrip unpack failed: {unpack_result['stderr'][-1000:]}"
        )

    roundtrip_data = find_loose_data_root(roundtrip_root)
    if roundtrip_data is None:
        raise RuntimeError("WOLF standard archive roundtrip did not produce a Data directory")
    bridge = bridge_path()
    verify_result = run_hidden([str(bridge), "verify", str(roundtrip_data)], timeout=300)
    state.tool_runs.append(verify_result)
    if verify_result["exit_code"] != 0:
        raise RuntimeError(
            f"WOLF standard archive roundtrip validation failed: {verify_result['stderr'][-1000:]}"
        )
    return _verify_runtime_key_snapshot(
        workspace,
        state,
        roundtrip_data,
        "archive_roundtrip",
    )


def _verify_runtime_key_snapshot(
    workspace: Path,
    state: WolfState,
    data_root: Path,
    label: str,
) -> dict[str, object]:
    if not state.runtime_key_fingerprint:
        return {"checked": False, "count": 0, "fingerprint_match": True}
    json_root = workspace / "wolf" / f"{label}_runtime_keys"
    _reset_dir(json_root)
    export_result = run_hidden(
        [str(bridge_path()), "export", str(data_root), str(json_root)],
        timeout=300,
    )
    state.tool_runs.append(export_result)
    if export_result["exit_code"] != 0:
        raise RuntimeError(f"WOLF 运行时键复核导出失败: {export_result['stderr'][-1000:]}")
    return verify_custom_database_keys(
        json_root,
        state.runtime_key_count,
        state.runtime_key_fingerprint,
    )


def _detect_pack_index(archives: list[Path], exe: Path) -> int:
    mapping = {0x012C: 4, 0x013A: 5, 0x014B: 6, 0x015E: 7, 0x0064: 8}
    for archive in archives:
        try:
            crypt_version = _archive_crypt_version(archive)
            if crypt_version in mapping:
                return mapping[crypt_version]
        except OSError:
            continue
    version = _windows_file_version(exe)
    if version:
        major, minor, _build, _revision = version
        if major <= 2:
            if minor <= 1:
                return 0
            if minor <= 10:
                return 1
            if minor <= 20:
                return 2
            return 3
        if major == 3:
            if minor < 14:
                return 4
            if minor < 31:
                return 5
            if minor < 50:
                return 6
            return 7
    # UberWolf's v3.50 packer is the compatible normal archive format for
    # current WOLF releases and for Pro data after --unprotect.
    return 7


def _native_pro_archive(archives: list[Path]) -> tuple[Path, int] | None:
    if len(archives) != 1:
        return None
    crypt_version = _archive_crypt_version(archives[0])
    if crypt_version is None or crypt_version < 1000:
        return None
    return archives[0], crypt_version


def _archive_crypt_version(archive: Path) -> int | None:
    with archive.open("rb") as stream:
        header = stream.read(48)
    if len(header) < 48 or header[:2] != b"DX":
        return None
    return struct.unpack_from("<H", header, 46)[0]


def _windows_file_version(path: Path) -> tuple[int, int, int, int] | None:
    if not hasattr(ctypes, "windll"):
        return None
    try:
        version = ctypes.windll.version
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        value = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not version.VerQueryValueW(buffer, "\\", ctypes.byref(value), ctypes.byref(length)):
            return None
        fixed = ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
        if fixed[0] != 0xFEEF04BD:
            return None
        return (
            fixed[2] >> 16,
            fixed[2] & 0xFFFF,
            fixed[3] >> 16,
            fixed[3] & 0xFFFF,
        )
    except (AttributeError, OSError, ValueError):
        return None


def _save_state(workspace: Path, state: WolfState) -> None:
    path = workspace / "wolf" / "state.json"
    path.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")


def _reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _decode_output(data: bytes) -> str:
    for encoding in ("utf-8", "cp932", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
