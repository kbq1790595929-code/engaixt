from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path

from core.path_resolver import resolve_game_path


class PipelineAlreadyRunning(RuntimeError):
    pass


_PROCESS_GUARD = threading.Lock()
_PROCESS_ACTIVE: set[str] = set()


def game_identity(input_path: str | Path) -> Path:
    resolved = Path(resolve_game_path(str(input_path))).resolve(strict=False)
    if resolved.is_dir():
        return resolved
    if resolved.suffix.casefold() in {".exe", ".bat", ".cmd", ".lnk"}:
        return resolved.parent
    return resolved


def lock_path_for_game(input_path: str | Path, base_dir: Path | None = None) -> Path:
    identity = game_identity(input_path)
    key = hashlib.sha256(str(identity).casefold().encode("utf-8")).hexdigest()
    root = base_dir or Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "EngAixt" / "pipeline_locks"
    return root / f"{key}.lock"


class GamePipelineLock(AbstractContextManager):
    def __init__(self, input_path: str | Path, *, base_dir: Path | None = None):
        self.input_path = str(input_path)
        self.identity = game_identity(input_path)
        self.path = lock_path_for_game(input_path, base_dir)
        self._key = str(self.path).casefold()
        self._handle = None
        self._process_registered = False

    def __enter__(self):
        with _PROCESS_GUARD:
            if self._key in _PROCESS_ACTIVE:
                raise self._busy_error()
            _PROCESS_ACTIVE.add(self._key)
            self._process_registered = True

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a+b")
            if self.path.stat().st_size == 0:
                self._handle.write(b"\0")
                self._handle.flush()
            self._lock_file()
            self._write_owner()
            return self
        except Exception:
            self._cleanup()
            raise

    def __exit__(self, exc_type, exc, tb):
        self._cleanup(unlock=True)
        return False

    def _lock_file(self) -> None:
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise self._busy_error() from exc

    def _unlock_file(self) -> None:
        if not self._handle:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    def _write_owner(self) -> None:
        payload = {
            "pid": os.getpid(),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "game": str(self.identity),
        }
        self._handle.seek(1)
        self._handle.truncate()
        self._handle.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self._handle.flush()

    def _busy_error(self) -> PipelineAlreadyRunning:
        return PipelineAlreadyRunning(f"该游戏已有翻译任务在运行，已阻止重复启动: {self.identity}")

    def _cleanup(self, *, unlock: bool = False) -> None:
        if self._handle:
            if unlock:
                self._unlock_file()
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        if self._process_registered:
            with _PROCESS_GUARD:
                _PROCESS_ACTIVE.discard(self._key)
            self._process_registered = False


def game_pipeline_lock(input_path: str | Path) -> GamePipelineLock:
    return GamePipelineLock(input_path)
