"""Incremental, session-local diagnostics for the native KRKR realtime route."""
from __future__ import annotations

import ctypes
import locale
import os
import re
import time
from pathlib import Path
from typing import Callable


def _decode_log_line(data: bytes) -> str:
    """Decode both new UTF-8 logs and legacy Windows code-page lines."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    candidates: list[str] = []
    if os.name == "nt":
        candidates.append("mbcs")
    preferred = locale.getpreferredencoding(False)
    if preferred:
        candidates.append(preferred)
    candidates.extend(("gbk", "cp936", "cp932", "shift_jis"))
    candidates.append("cp1252")
    seen: set[str] = set()
    for encoding in candidates:
        key = encoding.casefold()
        if key in seen:
            continue
        seen.add(key)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


class KirikiriRuntimeDiagnostics:
    def __init__(self, meta_dir: Path, emit: Callable[[int, str], None]):
        self.emit = emit
        self.paths = [
            meta_dir / "kirikiri_native_launcher.log",
            meta_dir / "kirikiri_native_hook.log",
            meta_dir / "kirikiri_overlay.log",
            meta_dir / "kirikiri_overlay_stderr.log",
        ]
        self.offsets = {path: path.stat().st_size if path.exists() else 0 for path in self.paths}
        self.pending = {path: b"" for path in self.paths}
        self.reported: set[str] = set()
        self.ready = False
        self.hooks = 0
        self.captures = 0
        self.displays = 0
        self.errors: list[str] = []
        self._ignored_overlay_traceback = False
        self.started_at = time.monotonic()

    def _once(self, key: str, level: int, message: str) -> None:
        if key not in self.reported:
            self.reported.add(key)
            self.emit(level, message)

    def poll(self) -> None:
        for path in self.paths:
            try:
                size = path.stat().st_size
                if size < self.offsets[path]:
                    self.offsets[path] = 0
                    self.pending[path] = b""
                with path.open("rb") as stream:
                    stream.seek(self.offsets[path])
                    data = stream.read()
                    self.offsets[path] = stream.tell()
                lines = (self.pending[path] + data).split(b"\n")
                self.pending[path] = lines.pop()
                for line in lines:
                    text = _decode_log_line(line).strip()
                    if path.name == "kirikiri_overlay_stderr.log":
                        # Older overlay builds created an async httpx client
                        # in a short-lived event loop and let it close during
                        # garbage collection. This traceback is transport
                        # cleanup noise, not a KRKR hook or translation failure.
                        if self._ignored_overlay_traceback:
                            if "RuntimeError: Event loop is closed" in text:
                                self._ignored_overlay_traceback = False
                                continue
                            if text.startswith("["):
                                self._ignored_overlay_traceback = False
                            else:
                                continue
                        if (
                            "Task exception was never retrieved" in text
                            or (
                                "AsyncClient.aclose" in text
                                and ("future:" in text or "coro=" in text)
                            )
                        ):
                            self._ignored_overlay_traceback = True
                            continue
                    if text and path.name == "kirikiri_overlay_stderr.log":
                        upper = text.upper()
                        if "TRACEBACK" in upper or "ERROR" in upper or "EXCEPTION" in upper or "FAILED" in upper:
                            self.errors.append(text)
                            self.emit(40, "KRKR \u5b57\u5e55\u8fdb\u7a0b\u9519\u8bef: " + text)
                        elif "WARNING" in upper or "WARN" in upper:
                            self.emit(30, "KRKR \u5b57\u5e55\u8fdb\u7a0b\u8b66\u544a: " + text)
                        else:
                            self._once("overlay_info:" + text, 20, "KRKR \u5b57\u5e55\u8fdb\u7a0b: " + text)
                    else:
                        self.consume(text)
            except OSError:
                continue

    def consume(self, line: str) -> None:
        if not line:
            return
        if any(token in line for token in (
            "injection_failed", "hook_ready_timeout", "ready_event_failed", "create_process_failed",
            "child_open_failed", "resume_failed", "launcher_error", "runtime_target_lost", "overlay_start_failed", "live_translate_failed",
            "live_translate_missing", "tk_callback_exception", "shm_read_failed",
        )):
            if line not in self.errors:
                self.errors.append(line)
                error = re.search(r"\bwin32=(\d+)", line)
                detail = ""
                if error and hasattr(ctypes, "FormatError"):
                    detail = " (" + ctypes.FormatError(int(error.group(1))).strip() + ")"
                self.emit(40, "KRKR \u5b9e\u65f6\u9519\u8bef: " + line + detail)
        if "process_exit pid=" in line:
            match = re.search(r"\bexit_code=(\d+)", line)
            if match and int(match.group(1)) != 0:
                self.errors.append(line)
                self.emit(40, "KRKR \u6e38\u620f\u8fdb\u7a0b\u5f02\u5e38\u9000\u51fa: " + line)
        if "child_detected" in line or "target_changed" in line:
            self._once(line, 20, "KRKR \u8ddf\u8e2a\u6e38\u620f\u5b50\u8fdb\u7a0b: " + line)
        if "hook_ready pid=" in line:
            self.ready = True
            self._once(line, 20, "KRKR DLL \u6ce8\u5165\u4e0e\u521d\u59cb\u5316\u6210\u529f: " + line)
        if "internal hook installed:" in line:
            self.hooks += 1
            self._once(line, 20, "KRKR \u6587\u672c hook \u5df2\u5b89\u88c5: " + line)
        if "signatures not found yet" in line:
            self._once("no_signature", 30,
                "KRKR \u5f53\u524d\u8fdb\u7a0b\u6682\u672a\u5339\u914d\u6587\u672c hook \u7b7e\u540d\uff1b"
                "\u5c06\u7ee7\u7eed\u68c0\u6d4b\u52a0\u8f7d\u6a21\u5757\u548c\u6e38\u620f\u5b50\u8fdb\u7a0b\u3002 " + line)
        for key, attribute in (("text_captures", "captures"), ("internal_hooks", "hooks")):
            match = re.search(r"\b" + key + r"=(\d+)", line)
            if match:
                setattr(self, attribute, max(getattr(self, attribute), int(match.group(1))))
        if self.captures:
            self._once("capture", 20, "KRKR hook \u5df2\u6355\u83b7\u6e38\u620f\u6587\u672c\u3002")
        if "text_displayed count=" in line:
            match = re.search(r"text_displayed count=(\d+)", line)
            self.displays = max(self.displays, int(match.group(1)) if match else 1)
            self._once("display", 20, "KRKR \u7b2c\u4e8c\u7a97\u53e3\u5df2\u663e\u793a\u4e2d\u6587\u8bd1\u6587: " + line)
        if "overlay_shutdown" in line or "overlay_exited" in line:
            self._once(line, 30, "KRKR \u5b57\u5e55\u7a97\u53e3\u72b6\u6001: " + line)
        if "hotkey_register_failed" in line:
            self._once("hotkey", 30, "KRKR \u5b57\u5e55\u5feb\u6377\u952e\u6ce8\u518c\u5931\u8d25\uff08\u4e0d\u5f71\u54cd\u6587\u672c\u6355\u83b7\uff09: " + line)

    def check_waiting(self) -> None:
        if self.ready and time.monotonic() - self.started_at >= 45 and not self.captures and not self.displays:
            self._once("waiting_capture", 30,
                "KRKR DLL \u5df2\u521d\u59cb\u5316\uff0c\u4f46\u5c1a\u672a\u6355\u83b7\u6587\u672c\uff1b"
                "\u82e5\u6e38\u620f\u5df2\u663e\u793a\u5267\u60c5\uff0c\u5219\u9700\u6392\u67e5\u5f53\u524d\u8fdb\u7a0b\u548c hook \u7b7e\u540d\u3002 "
                + f"hooks={self.hooks}, captures=0, displayed=0")

    def finish(self, exit_code: int) -> bool:
        self.poll()
        summary = f"exit_code={exit_code}, hooks={self.hooks}, captures={self.captures}, displayed={self.displays}"
        if exit_code or self.errors:
            self.emit(40, "KRKR \u5b9e\u65f6\u4f1a\u8bdd\u7ed3\u675f\uff0c\u5b58\u5728\u9519\u8bef: " + summary)
            return False
        if not self.ready:
            self.emit(40, "KRKR \u672a\u786e\u8ba4 DLL \u6ce8\u5165\u6210\u529f: " + summary)
            return False
        if not self.captures and not self.displays:
            reason = "\u672a\u5339\u914d\u6587\u672c hook \u7b7e\u540d" if not self.hooks else "\u5df2\u5b89\u88c5 hook \u4f46\u672a\u6355\u83b7\u6587\u672c"
            self.emit(30, "KRKR \u6e38\u620f\u5df2\u9000\u51fa\uff0c" + reason +
                "\uff08\u82e5\u672a\u8fdb\u5165\u5267\u60c5\uff0c\u4e0d\u80fd\u636e\u6b64\u5224\u5b9a\u5f15\u64ce\u4e0d\u652f\u6301\uff09: " + summary)
        elif not self.displays:
            self.emit(30, "KRKR \u5df2\u6355\u83b7\u6587\u672c\uff0c\u4f46\u672a\u786e\u8ba4\u5b57\u5e55\u663e\u793a\u8bd1\u6587: " + summary)
        else:
            self.emit(20, "KRKR \u6e38\u620f\u5df2\u9000\u51fa\uff0c\u5b9e\u65f6\u7ffb\u8bd1\u4f1a\u8bdd\u6b63\u5e38\u7ed3\u675f: " + summary)
        return True
