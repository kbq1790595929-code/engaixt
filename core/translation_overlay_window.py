"""Standalone KiriKiri translation overlay window."""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import asyncio
import base64
import hashlib
import mmap
import queue
import re
import struct
import subprocess
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

from config import CONFIG_PATH, get_config, reload_config, save_config
from core.runtime_translation_context import RollingTranslationContext
from core.runtime_translation_retry import translate_with_empty_retry
from engines.base import TextItem


_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_SPEAKER_QUOTE_RE = re.compile(r"^\s*([\w\u3040-\u30ff\u3400-\u9fff々ヶー・]{1,24})[「『](.+)[」』]\s*$", re.S)
_SPEAKER_COLON_RE = re.compile(r"^\s*([\w\u3040-\u30ff\u3400-\u9fff々ヶー・]{1,24})[：:]\s*(.{2,})\s*$", re.S)
_WAITING_TEXT = "\u7b49\u5f85\u6e38\u620f\u6587\u672c..."
_PENDING_TEXT = "\u7ffb\u8bd1\u4e2d..."
_UNTRANSLATED_TEXT = "[\u5f85\u7ffb\u8bd1]"
_ORIGINAL_PREFIX = "\u539f\u6587: "
_NARRATION_SPEAKERS = {"", "ト書き", "地の文", "ナレーション", "narration"}
_VK_LBUTTON = 0x01
_MOUSE_RECALL_HOLD_SECONDS = 1.0
_MOUSE_RECALL_POLL_SECONDS = 0.05
_MOUSE_RECALL_COOLDOWN_SECONDS = 1.2
_HISTORY_LIMIT = 200


def _clamp_int(value: object, default: int, low: int, high: int) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        parsed = default
    return max(low, min(high, parsed))


def _clamp_float(value: object, default: float, low: float, high: float) -> float:
    try:
        parsed = float(str(value).strip())
    except Exception:
        parsed = default
    return max(low, min(high, parsed))


def _color(value: object, default: str) -> str:
    text = str(value or "").strip()
    return text if _HEX_COLOR_RE.match(text) else default


def split_speaker_text(text: str) -> tuple[str, str]:
    text = str(text or "").strip()
    if not text:
        return "", ""
    for pattern in (_SPEAKER_QUOTE_RE, _SPEAKER_COLON_RE):
        match = pattern.match(text)
        if match:
            speaker = match.group(1).strip()
            body = match.group(2).strip()
            if speaker and body:
                return speaker, body
    return "", text


def normalize_speaker_name(speaker: str) -> str:
    speaker = str(speaker or "").strip().replace("\u3000", " ")
    speaker = re.sub(r"\s+", "", speaker)
    return "" if speaker in _NARRATION_SPEAKERS else speaker


def runtime_dialogue_parts(original: str, speaker: str = "") -> tuple[str, str, str]:
    """Return (display speaker, display original, text to translate).

    KiriKiri hooks often expose dialogue as either a separate speaker plus body,
    or as a Luna-style combined string like NAME「line」.  The overlay should
    display the speaker separately but translate/cache only the spoken body.
    """
    original = str(original or "").strip()
    explicit_speaker = normalize_speaker_name(speaker)
    parsed_speaker, parsed_body = split_speaker_text(original)
    display_speaker = explicit_speaker or parsed_speaker
    display_original = parsed_body if parsed_speaker else original
    translate_text = parsed_body if parsed_speaker else original
    return display_speaker, display_original, translate_text


def decode_overlay_payload(payload: bytes) -> tuple[int, str, str, str] | None:
    """Decode native overlay shared memory payload.

    Layout v1: seq, original length/body, translated length/body.
    Layout v2 appends speaker length/body.  The optional tail keeps older hooks
    readable while allowing KAG name-layer hooks to provide speaker names.
    """
    if len(payload) < 12:
        return None
    seq = struct.unpack_from("I", payload, 0)[0]
    if seq == 0:
        return None
    offset = 4
    orig_len = struct.unpack_from("I", payload, offset)[0]
    offset += 4
    if orig_len == 0 or orig_len >= 30000 or offset + orig_len > len(payload):
        return None
    original = payload[offset:offset + orig_len].decode("utf-8", errors="ignore")
    offset += orig_len
    if offset + 4 > len(payload):
        return None
    trans_len = struct.unpack_from("I", payload, offset)[0]
    offset += 4
    if trans_len >= 30000 or offset + trans_len > len(payload):
        return None
    translated = ""
    if trans_len > 0:
        translated = payload[offset:offset + trans_len].decode("utf-8", errors="ignore")
        offset += trans_len
    speaker = ""
    if offset + 4 <= len(payload):
        speaker_len = struct.unpack_from("I", payload, offset)[0]
        offset += 4
        if 0 < speaker_len < 1000 and offset + speaker_len <= len(payload):
            speaker = payload[offset:offset + speaker_len].decode("utf-8", errors="ignore")
    return seq, original, translated, normalize_speaker_name(speaker)


def encode_overlay_payload(seq: int, original: str, translated: str = "", speaker: str = "") -> bytes:
    """Encode a payload in the same v2 layout the native KiriKiri hook writes.

    Lets any engine's Python-side capture loop (no native C++ hook required)
    feed the same overlay reader used by KiriKiri, so the subtitle window
    stays engine-agnostic.
    """
    orig_b = original.encode("utf-8")[:29000]
    trans_b = translated.encode("utf-8")[:29000]
    speaker_b = speaker.encode("utf-8")[:900]
    payload = (
        struct.pack("I", max(1, seq))
        + struct.pack("I", len(orig_b)) + orig_b
        + struct.pack("I", len(trans_b)) + trans_b
        + struct.pack("I", len(speaker_b)) + speaker_b
    )
    return payload[:65536]


def make_dialogue_history_entry(
    speaker: str,
    original: str,
    translated: str,
    timestamp: float | None = None,
) -> dict:
    try:
        normalized_timestamp = float(timestamp if timestamp is not None else time.time())
    except Exception:
        normalized_timestamp = time.time()
    return {
        "speaker": normalize_speaker_name(speaker),
        "original": str(original or "").strip(),
        "translated": str(translated or "").strip(),
        "timestamp": normalized_timestamp,
    }


def append_dialogue_history(history: list[dict], entry: dict, limit: int = _HISTORY_LIMIT) -> bool:
    original = str(entry.get("original") or "").strip()
    translated = str(entry.get("translated") or "").strip()
    speaker = normalize_speaker_name(str(entry.get("speaker") or ""))
    if not original and not translated:
        return False

    normalized = make_dialogue_history_entry(
        speaker,
        original,
        translated,
        timestamp=entry.get("timestamp"),
    )
    if history:
        last = history[-1]
        same_line = (
            str(last.get("speaker") or "") == normalized["speaker"]
            and str(last.get("original") or "") == normalized["original"]
        )
        if same_line and not str(last.get("translated") or "") and normalized["translated"]:
            last.update(normalized)
            return True
        if (
            same_line
            and str(last.get("translated") or "") == normalized["translated"]
        ):
            last["timestamp"] = normalized["timestamp"]
            return False

    history.append(normalized)
    del history[:max(0, len(history) - max(1, int(limit)))]
    return True


def format_dialogue_history_entry(entry: dict) -> str:
    timestamp = float(entry.get("timestamp") or 0.0)
    clock = time.strftime("%H:%M:%S", time.localtime(timestamp)) if timestamp > 0 else "--:--:--"
    speaker = normalize_speaker_name(str(entry.get("speaker") or ""))
    original = str(entry.get("original") or "").strip()
    translated = str(entry.get("translated") or "").strip()
    title = f"[{clock}] {speaker}" if speaker else f"[{clock}]"
    lines = [title]
    if original:
        lines.append(f"原文：{original}")
    if translated:
        lines.append(f"译文：{translated}")
    else:
        lines.append(f"译文：{_PENDING_TEXT}")
    return "\n".join(lines)


class OverlayShmWriter:
    """Python-side counterpart to kirikiri_native_hook.cpp's shared memory writer.

    Engines without a native display hook (i.e. everything routed through
    ``frida/realtime_hook.js``) capture original text and translate it in
    Python; this writes results into a named shared memory segment so a
    ``TranslationOverlayWindow`` subprocess can display them exactly like the
    KiriKiri native path, without the hook ever touching the game's own
    string memory.
    """

    def __init__(self, shm_name: str):
        self.shm_name = shm_name
        self._seq = 0
        self._lock = threading.Lock()
        self._shm = mmap.mmap(-1, 65536, tagname=shm_name, access=mmap.ACCESS_WRITE)

    def write(self, original: str, translated: str = "", speaker: str = "") -> None:
        with self._lock:
            self._seq += 1
            payload = encode_overlay_payload(self._seq, original, translated, speaker)
            self._shm.seek(0)
            self._shm.write(payload.ljust(65536, b"\x00"))

    def close(self) -> None:
        try:
            self._shm.close()
        except Exception:
            pass


class TranslationOverlayWindow:
    """LunaTranslator-style display window fed by a shared-memory text source.

    The source is either the native KiriKiri hook (C++) or an ``OverlayShmWriter``
    driven by a Python-side Frida capture loop for other engines — the reader
    side (this class) does not care which, as long as the payload format matches.
    """

    def __init__(
        self,
        game_name: str = "游戏",
        game_exe: str = "GAME.exe",
        game_exe_path: str = "",
        shm_name: str = "",
    ):
        self.game_name = game_name
        self.game_exe = game_exe
        self.game_exe_path = str(game_exe_path or "").strip()
        self._normalized_game_exe_path = self._normalize_path(self.game_exe_path)
        self.game_hwnd = None
        self.game_pid: int | None = None
        self._game_seen = False
        self._game_process_name_seen = False
        self._last_process_name_check = 0.0
        self._started_at = time.time()
        self._close_requested = False
        self.text_queue: queue.Queue[dict] = queue.Queue(maxsize=100)
        self.translate_queue: queue.Queue[dict] = queue.Queue(maxsize=200)
        self.running = False
        self.window: tk.Tk | None = None
        self.text_widget: tk.Label | None = None
        self.text_canvas: tk.Canvas | None = None
        self.speaker_label: tk.Label | None = None
        self.original_label: tk.Label | None = None
        self.status_label: tk.Label | None = None
        self.follow_button: tk.Button | None = None
        self.lock_button: tk.Button | None = None
        self.embed_button: tk.Button | None = None
        self.history_button: tk.Widget | None = None
        self.history_window: tk.Toplevel | None = None
        self.history_text: tk.Text | None = None
        self.dialogue_history: list[dict] = []
        self._translation_context = RollingTranslationContext(limit=3, max_chars=400)
        self.transparent_key = "#010203"
        self.shm_name = shm_name or "kirikiri_translation_overlay"
        self.last_original = ""
        self._last_seq = 0
        self._pending_geometry: tuple[int, int, int, int] | None = None
        self._last_game_rect: dict | None = None
        self.log_path = self._resolve_log_path()
        self.map_path = self._resolve_map_path()
        self.local_translations = self._load_local_translation_map()
        self._translation_inflight: set[str] = set()
        self._translator = None
        self._translator_provider = ""
        self._hotkey_id = 0x4754
        self._taskbar_minimized = False
        self._hidden_for_recall = False
        self._restore_requested = False
        self._restore_request_source = ""
        self._mouse_recall_down_at: float | None = None
        self._mouse_recall_cooldown_until = 0.0
        self._config_mtime_ns: int | None = None
        self._last_config_check = 0.0
        self._layout_text_only = True
        self._display_speaker = ""
        self._display_original = ""
        self._display_text = _WAITING_TEXT
        self._display_state = "waiting"
        self._display_color = ""
        self.drag_start_x = 0
        self.drag_start_y = 0
        self.drag_start_root_x = 0
        self.drag_start_root_y = 0
        self.drag_window_x = 0
        self.drag_window_y = 0
        self._dragging = False
        self._last_drag_update = 0.0
        self._drag_pending_xy: tuple[int, int] | None = None
        self._load_config()
        self._layout_text_only = self.text_only
        self._display_color = self.waiting_color

    def _load_config(self) -> None:
        cfg = get_config()
        self.font_family = str(getattr(cfg, "kirikiri_overlay_font_family", "") or "Microsoft YaHei UI").strip()
        self.font_size = _clamp_int(getattr(cfg, "kirikiri_overlay_font_size", 18), 18, 10, 42)
        self.original_font_size = _clamp_int(getattr(cfg, "kirikiri_overlay_original_font_size", 11), 11, 8, 28)
        self.text_color = _color(getattr(cfg, "kirikiri_overlay_text_color", "#f4f7ff"), "#f4f7ff")
        self.speaker_color = _color(getattr(cfg, "kirikiri_overlay_speaker_color", "#ffd36e"), "#ffd36e")
        self.original_color = _color(getattr(cfg, "kirikiri_overlay_original_color", "#a6adbb"), "#a6adbb")
        self.bg_color = _color(getattr(cfg, "kirikiri_overlay_bg_color", "#111318"), "#111318")
        self.pending_color = _color(getattr(cfg, "kirikiri_overlay_pending_color", "#f6b73c"), "#f6b73c")
        self.waiting_color = _color(getattr(cfg, "kirikiri_overlay_waiting_color", "#7c8496"), "#7c8496")
        self.opacity = _clamp_float(getattr(cfg, "kirikiri_overlay_opacity", 0.92), 0.92, 0.30, 1.0)
        self.height = _clamp_int(getattr(cfg, "kirikiri_overlay_height", 180), 180, 90, 420)
        self.max_width = _clamp_int(getattr(cfg, "kirikiri_overlay_max_width", 900), 900, 420, 2200)
        self.no_window_timeout = _clamp_int(
            getattr(cfg, "kirikiri_no_window_timeout_seconds", 45),
            45,
            10,
            300,
        )
        self.text_only = bool(getattr(cfg, "kirikiri_overlay_text_only", True))
        self.show_speaker = bool(getattr(cfg, "kirikiri_overlay_show_speaker", True))
        self.show_original = bool(getattr(cfg, "kirikiri_overlay_show_original", True))
        self.live_translate = bool(getattr(cfg, "kirikiri_overlay_live_translate", True))
        self.follow_window = bool(getattr(cfg, "kirikiri_overlay_follow_window", True))
        self.locked = bool(getattr(cfg, "kirikiri_overlay_locked", False))
        self._embedded = bool(getattr(cfg, "kirikiri_overlay_embedded", True))
        self.remember_position = bool(getattr(cfg, "kirikiri_overlay_remember_position", True))
        self.saved_x = _clamp_int(getattr(cfg, "kirikiri_overlay_saved_x", -1), -1, -1, 20000)
        self.saved_y = _clamp_int(getattr(cfg, "kirikiri_overlay_saved_y", -1), -1, -1, 20000)
        self.saved_width = _clamp_int(getattr(cfg, "kirikiri_overlay_saved_width", 0), 0, 0, 2200)
        self._config_mtime_ns = self._config_mtime()

    def _config_mtime(self) -> int | None:
        try:
            return CONFIG_PATH.stat().st_mtime_ns
        except OSError:
            return None

    def _reload_config_if_changed(self) -> None:
        now = time.time()
        if now - self._last_config_check < 0.5:
            return
        self._last_config_check = now
        mtime = self._config_mtime()
        if mtime is None or mtime == self._config_mtime_ns:
            return
        old_text_only = self._layout_text_only
        old_embedded = self._embedded  # 保存当前嵌入状态
        try:
            reload_config()
            self._load_config()
            if self.text_only != old_text_only:
                self.text_only = old_text_only
            self._embedded = old_embedded  # 恢复嵌入状态，避免被配置重载覆盖
            self._apply_runtime_config()
            self._log("config_reloaded")
        except Exception as exc:
            self._log(f"config_reload_failed: {exc}")

    def _apply_runtime_config(self) -> None:
        if self.window is None:
            return
        try:
            self.window.attributes("-alpha", 1.0 if self._layout_text_only else self.opacity)
            self.window.configure(bg=self.transparent_key if self._layout_text_only else self.bg_color)
            if self._display_state == "translated":
                self._display_color = self.text_color
            elif self._display_state == "pending":
                self._display_color = self.pending_color
            else:
                self._display_color = self.waiting_color
            width = int(self.window.winfo_width() or self.saved_width or self.max_width)
            width, height, x, y = self._clamp_overlay_geometry(
                min(max(420, width), self.max_width),
                self.height,
                int(self.window.winfo_x()),
                int(self.window.winfo_y()),
            )
            self.window.geometry(f"{width}x{height}+{x}+{y}")
            if self.status_label is not None:
                self.status_label.config(bg=self.bg_color, fg=self.original_color, font=(self.font_family, 9))
            if self.speaker_label is not None:
                self.speaker_label.config(
                    bg=self.bg_color,
                    fg=self.speaker_color,
                    font=(self.font_family, max(10, self.font_size - 2), "bold"),
                )
            if self.original_label is not None:
                self.original_label.config(
                    bg=self.bg_color,
                    fg=self.original_color,
                    font=(self.font_family, self.original_font_size),
                )
            if self.text_widget is not None:
                self.text_widget.config(
                    bg=self.bg_color,
                    fg=self._display_color or self.text_color,
                    font=(self.font_family, self.font_size, "bold"),
                )
            self._refresh_buttons()
            self._update_wraplength()
            self._draw_text_canvas()
        except Exception as exc:
            self._log(f"apply_config_failed: {exc}")

    def start(self) -> None:
        self.running = True
        self._log("overlay_start")
        self._create_window()

        # 根据配置初始化嵌入状态（写入共享内存）
        if self._embedded:
            self._enable_embed_mode()
        else:
            self._disable_embed_mode()

        reader_thread = threading.Thread(target=self._read_shm_loop, daemon=True)
        reader_thread.start()

        tracker_thread = threading.Thread(target=self._track_game_window, daemon=True)
        tracker_thread.start()

        translate_thread = threading.Thread(target=self._translation_worker_loop, daemon=True)
        translate_thread.start()

        hotkey_thread = threading.Thread(target=self._hotkey_loop, daemon=True)
        hotkey_thread.start()

        mouse_recall_thread = threading.Thread(target=self._mouse_recall_loop, daemon=True)
        mouse_recall_thread.start()

        assert self.window is not None
        self.window.after(100, self._update_display)
        self.window.mainloop()

    def _normalize_path(self, value: str) -> str:
        if not value:
            return ""
        try:
            return str(Path(value).resolve()).casefold()
        except Exception:
            return str(value).casefold()

    def _resolve_log_path(self) -> Path | None:
        if not self.game_exe_path:
            return None
        try:
            meta_dir = Path(self.game_exe_path).resolve().parent / "_translation_meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            return meta_dir / "kirikiri_overlay.log"
        except Exception:
            return None

    def _resolve_map_path(self) -> Path | None:
        if not self.game_exe_path:
            return None
        try:
            meta_dir = Path(self.game_exe_path).resolve().parent / "_translation_meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            return meta_dir / "kirikiri_native_map.tsv"
        except Exception:
            return None

    def _log(self, message: str) -> None:
        if not self.log_path:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            self.log_path.open("a", encoding="utf-8").write(f"[{timestamp}] {message}\n")
        except Exception:
            pass

    def _load_local_translation_map(self) -> dict[str, str]:
        path = self.map_path
        if not path or not path.exists():
            return {}
        out: dict[str, str] = {}
        try:
            for line in path.read_text(encoding="ascii", errors="ignore").splitlines():
                if not line or line.startswith("#") or "\t" not in line:
                    continue
                src_b64, dst_b64 = line.split("\t", 1)
                src = base64.b64decode(src_b64).decode("utf-8", errors="ignore")
                dst = base64.b64decode(dst_b64).decode("utf-8", errors="ignore")
                if src and dst and src != dst:
                    out[src] = dst
        except Exception as exc:
            self._log(f"map_load_failed: {exc}")
        return out

    def _append_local_translation(self, original: str, translated: str) -> None:
        if not original or not translated or original == translated:
            return
        self.local_translations[original] = translated
        if not self.map_path:
            return
        try:
            self.map_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.map_path.exists():
                self.map_path.write_text("# base64_utf8_original\tbase64_utf8_translated\n", encoding="ascii")
            src = base64.b64encode(original.encode("utf-8")).decode("ascii")
            dst = base64.b64encode(translated.encode("utf-8")).decode("ascii")
            with self.map_path.open("a", encoding="ascii") as f:
                f.write(f"{src}\t{dst}\n")
        except Exception as exc:
            self._log(f"map_append_failed: {exc}")

    def _find_game_window(self) -> None:
        user32 = ctypes.windll.user32

        if self.game_pid:
            def enum_pid_callback(hwnd, _) -> bool:
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = ctypes.wintypes.DWORD()
                try:
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                except Exception:
                    return True
                if int(pid.value) == int(self.game_pid):
                    self.game_hwnd = hwnd
                    self._game_seen = True
                    return False
                return True

            enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
            user32.EnumWindows(enum_proc(enum_pid_callback), 0)
            if self.game_hwnd:
                return

        def enum_callback(hwnd, _) -> bool:
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value
                    if self.game_name in title or self.game_exe.replace(".exe", "") in title:
                        self.game_hwnd = hwnd
                        self._remember_game_pid(hwnd)
                        return False
            return True

        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        user32.EnumWindows(enum_proc(enum_callback), 0)

    def _remember_game_pid(self, hwnd: int) -> None:
        user32 = ctypes.windll.user32
        pid = ctypes.wintypes.DWORD()
        try:
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                self.game_pid = int(pid.value)
                self._game_seen = True
        except Exception:
            pass

    def _game_process_alive(self) -> bool | None:
        if not self.game_pid:
            return None
        kernel32 = ctypes.windll.kernel32
        synchronize = 0x00100000
        wait_timeout = 0x00000102
        handle = kernel32.OpenProcess(synchronize, False, int(self.game_pid))
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)

    def _game_exe_process_running(self) -> bool | None:
        exe = (self.game_exe or "").strip()
        if not exe:
            return None
        if not exe.lower().endswith(".exe"):
            exe += ".exe"
        now = time.time()
        if now - self._last_process_name_check < 2.0:
            return None
        self._last_process_name_check = now
        matches = self._processes_by_name(exe)
        if not matches:
            return False
        if self._normalized_game_exe_path:
            for pid, path in matches:
                if self._normalize_path(path) == self._normalized_game_exe_path:
                    self.game_pid = pid
                    self._game_seen = True
                    return True
            return False
        self.game_pid = matches[0][0]
        self._game_seen = True
        return True

    def _processes_by_name(self, exe: str) -> list[tuple[int, str]]:
        if not exe:
            return []
        exe_lower = exe.lower()
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.wintypes.HANDLE
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if not snapshot or snapshot == ctypes.wintypes.HANDLE(-1).value:
            return []

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.wintypes.DWORD),
                ("cntUsage", ctypes.wintypes.DWORD),
                ("th32ProcessID", ctypes.wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", ctypes.wintypes.DWORD),
                ("cntThreads", ctypes.wintypes.DWORD),
                ("th32ParentProcessID", ctypes.wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        kernel32.Process32FirstW.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        matches: list[tuple[int, str]] = []
        try:
            has_entry = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while has_entry:
                name = str(entry.szExeFile or "")
                if name.lower() == exe_lower:
                    pid = int(entry.th32ProcessID)
                    matches.append((pid, self._process_image_path(pid)))
                has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return matches

    def _process_image_path(self, pid: int) -> str:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.LPWSTR,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return ""
        try:
            size = ctypes.wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
        except Exception:
            return ""
        finally:
            kernel32.CloseHandle(handle)
        return ""

    def _get_game_window_rect(self) -> dict | None:
        if not self.game_hwnd:
            return None
        user32 = ctypes.windll.user32
        if user32.IsIconic(self.game_hwnd):
            return None
        rect = ctypes.wintypes.RECT()
        if user32.GetWindowRect(self.game_hwnd, ctypes.byref(rect)):
            info = {
                "left": rect.left,
                "top": rect.top,
                "right": rect.right,
                "bottom": rect.bottom,
                "width": rect.right - rect.left,
                "height": rect.bottom - rect.top,
            }
            if info["width"] < 160 or info["height"] < 90:
                return None
            if not self._rect_intersects_virtual_screen(info):
                return None
            return info
        return None

    def _virtual_screen_bounds(self) -> tuple[int, int, int, int]:
        user32 = ctypes.windll.user32
        try:
            left = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
            top = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
            width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
            height = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
            if width > 0 and height > 0:
                return left, top, left + width, top + height
        except Exception:
            pass
        return 0, 0, int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))

    def _rect_intersects_virtual_screen(self, rect: dict) -> bool:
        left, top, right, bottom = self._virtual_screen_bounds()
        return (
            int(rect["right"]) > left
            and int(rect["left"]) < right
            and int(rect["bottom"]) > top
            and int(rect["top"]) < bottom
        )

    def _clamp_overlay_geometry(self, width: int, height: int, x: int, y: int) -> tuple[int, int, int, int]:
        left, top, right, bottom = self._virtual_screen_bounds()
        screen_width = max(420, right - left)
        screen_height = max(120, bottom - top)
        width = min(max(420, int(width)), min(self.max_width, screen_width))
        height = min(max(90, int(height)), screen_height)
        x = max(left, min(int(x), right - width))
        y = max(top, min(int(y), bottom - height))
        return width, height, x, y

    def _hotkey_loop(self) -> None:
        if not hasattr(ctypes, "windll"):
            return
        user32 = ctypes.windll.user32
        mod_alt = 0x0001
        mod_control = 0x0002
        vk_t = ord("T")
        try:
            registered = user32.RegisterHotKey(None, self._hotkey_id, mod_alt | mod_control, vk_t)
        except Exception:
            registered = False
        if not registered:
            self._log("hotkey_register_failed: Ctrl+Alt+T")
            return
        self._log("hotkey_registered: Ctrl+Alt+T")

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.wintypes.HWND),
                ("message", ctypes.wintypes.UINT),
                ("wParam", ctypes.wintypes.WPARAM),
                ("lParam", ctypes.wintypes.LPARAM),
                ("time", ctypes.wintypes.DWORD),
                ("pt", ctypes.wintypes.POINT),
            ]

        msg = MSG()
        wm_hotkey = 0x0312
        try:
            while self.running:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    time.sleep(0.5)
                    continue
                if msg.message == wm_hotkey and int(msg.wParam) == self._hotkey_id:
                    self._request_restore("hotkey")
        finally:
            try:
                user32.UnregisterHotKey(None, self._hotkey_id)
            except Exception:
                pass

    def _request_restore(self, source: str) -> None:
        if not self.running:
            return
        self._restore_requested = True
        self._restore_request_source = source
        self._log(f"mouse_recall_requested source={source}" if source == "mouse" else f"overlay_restore_requested source={source}")

    def _mouse_recall_loop(self) -> None:
        """Restore the hidden subtitle window after a global left-button hold."""
        if not hasattr(ctypes, "windll"):
            return
        user32 = ctypes.windll.user32
        try:
            user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            user32.GetAsyncKeyState.restype = ctypes.c_short
        except Exception:
            pass
        self._log("mouse_recall_registered: hold_left_button")
        while self.running:
            try:
                is_down = bool(user32.GetAsyncKeyState(_VK_LBUTTON) & 0x8000)
            except Exception as exc:
                self._log(f"mouse_recall_failed: {exc}")
                time.sleep(0.5)
                continue

            now = time.time()
            if not self._taskbar_minimized:
                self._mouse_recall_down_at = None
                time.sleep(_MOUSE_RECALL_POLL_SECONDS)
                continue

            if not is_down:
                self._mouse_recall_down_at = None
                time.sleep(_MOUSE_RECALL_POLL_SECONDS)
                continue

            if self._mouse_recall_down_at is None:
                self._mouse_recall_down_at = now
            elif now - self._mouse_recall_down_at >= _MOUSE_RECALL_HOLD_SECONDS and now >= self._mouse_recall_cooldown_until:
                self._mouse_recall_down_at = None
                self._mouse_recall_cooldown_until = now + _MOUSE_RECALL_COOLDOWN_SECONDS
                self._request_restore("mouse")

            time.sleep(_MOUSE_RECALL_POLL_SECONDS)

    def _consume_restore_request(self) -> None:
        if not self._restore_requested:
            return
        source = self._restore_request_source or "unknown"
        self._restore_requested = False
        self._restore_request_source = ""
        self._restore_window(source=source)

    def _restore_window(self, source: str = "manual") -> None:
        if self.window is None:
            return
        try:
            self._log(f"overlay_restore_begin source={source}")
            width = self.saved_width if self.saved_width > 0 else max(720, self.window.winfo_width())
            width, height, x, y = self._clamp_overlay_geometry(width, self.height, self.saved_x if self.saved_x >= 0 else 100, self.saved_y if self.saved_y >= 0 else 100)
            self._taskbar_minimized = False
            self._hidden_for_recall = False
            self._pending_geometry = None
            self.window.overrideredirect(False)
            self.window.deiconify()
            try:
                self.window.state("normal")
            except Exception:
                pass
            self.window.geometry(f"{width}x{height}+{x}+{y}")
            self.window.update_idletasks()
            self.window.attributes("-topmost", False)
            self.window.attributes("-topmost", True)
            self.window.lift()
            try:
                self.window.focus_force()
            except Exception:
                pass
            self._update_wraplength()
            self.window.after(60, self._restore_borderless_chrome)
            self._log(f"overlay_restore_done state={self.window.state()} geometry={self.window.geometry()}")
        except Exception as exc:
            self._log(f"overlay_restore_failed: {exc}")

    def _restore_borderless_chrome(self) -> None:
        if self.window is None or not self.running:
            return
        try:
            self.window.overrideredirect(True)
            self.window.attributes("-topmost", True)
            self.window.lift()
            self._update_wraplength()
        except Exception as exc:
            self._log(f"overlay_restore_chrome_failed: {exc}")

    def _report_callback_exception(self, exc_type, exc_value, exc_tb) -> None:
        try:
            detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb)).strip()
            self._log(f"tk_callback_exception: {detail}")
        except Exception:
            pass

    def _track_game_window(self) -> None:
        while self.running:
            running_by_name = self._game_exe_process_running()
            if running_by_name is True:
                self._game_process_name_seen = True
            elif running_by_name is False and self._game_process_name_seen:
                self._close_requested = True
                return
            if self._game_seen:
                alive = self._game_process_alive()
                if alive is False:
                    self._close_requested = True
                    return
                if self.game_hwnd and not ctypes.windll.user32.IsWindow(self.game_hwnd):
                    self._close_requested = True
                    return

            if not self.game_hwnd:
                self._find_game_window()
            if self.game_hwnd:
                rect = self._get_game_window_rect()
                if rect:
                    self._last_game_rect = rect
                    if self.locked or not self.follow_window:
                        time.sleep(0.5)
                        continue
                    width = min(max(420, rect["width"]), self.max_width)
                    self._pending_geometry = self._clamp_overlay_geometry(
                        width,
                        self.height,
                        rect["left"],
                        rect["bottom"],
                    )
            time.sleep(0.5)

    def _default_geometry(self) -> str:
        width = self.saved_width if self.saved_width > 0 else 900
        width = min(max(420, width), self.max_width)
        if self.remember_position and self.saved_x >= 0 and self.saved_y >= 0:
            width, height, x, y = self._clamp_overlay_geometry(width, self.height, self.saved_x, self.saved_y)
            return f"{width}x{height}+{x}+{y}"
        width, height, x, y = self._clamp_overlay_geometry(width, self.height, 100, 100)
        return f"{width}x{height}+{x}+{y}"

    def _create_window(self) -> None:
        self.window = tk.Tk()
        self.window.report_callback_exception = self._report_callback_exception
        self.window.title(f"翻译 - {self.game_name}")
        self.window.geometry(self._default_geometry())
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 1.0 if self.text_only else self.opacity)
        self.window.configure(bg=self.transparent_key if self.text_only else self.bg_color)
        self.window.overrideredirect(True)
        if self.text_only:
            try:
                self.window.attributes("-transparentcolor", self.transparent_key)
            except Exception as exc:
                self._log(f"transparentcolor_failed: {exc}")
                self.window.attributes("-alpha", self.opacity)
            main = tk.Frame(self.window, bg=self.transparent_key, bd=0, highlightthickness=0)
            main.pack(fill=tk.BOTH, expand=True)

            controls = tk.Frame(main, bg=self.transparent_key, bd=0, highlightthickness=0)
            controls.pack(fill=tk.X, padx=2, pady=(0, 0))
            handle = self._make_text_control(controls, "≡", self._restore_window)
            handle.pack(side=tk.LEFT, padx=(0, 8))
            self.follow_button = self._make_text_control(controls, "跟随" if not self.follow_window else "固定", self._toggle_follow)
            self.follow_button.pack(side=tk.LEFT, padx=(0, 8))
            self.lock_button = self._make_text_control(controls, "解锁" if self.locked else "锁定", self._toggle_lock)
            self.lock_button.pack(side=tk.LEFT, padx=(0, 8))
            self.embed_button = self._make_text_control(controls, "嵌入" if not self._embedded else "取消嵌入", self._toggle_embed)
            self.embed_button.pack(side=tk.LEFT, padx=(0, 8))
            self.history_button = self._make_text_control(controls, "历史", self._toggle_history_window)
            self.history_button.pack(side=tk.LEFT, padx=(0, 8))
            self._make_text_control(controls, "—", self._minimize).pack(side=tk.RIGHT, padx=(10, 0))
            self._make_text_control(controls, "×", self._on_close).pack(side=tk.RIGHT, padx=(10, 0))

            self.text_canvas = tk.Canvas(
                main,
                bg=self.transparent_key,
                bd=0,
                highlightthickness=0,
                relief=tk.FLAT,
            )
            self.text_canvas.pack(fill=tk.BOTH, expand=True)

            for widget in (main, controls, handle, self.follow_button, self.lock_button, self.embed_button, self.history_button, self.text_canvas):
                if widget is not None:
                    self._bind_drag(widget)
                    widget.bind("<Button-3>", self._minimize)

            self.window.bind("<Button-3>", self._minimize)
            self.window.bind("<Double-Button-1>", self._toggle_follow)
            self.window.bind("<Configure>", self._update_wraplength)
            self.window.bind("<Map>", self._on_window_mapped)
            self.window.protocol("WM_DELETE_WINDOW", self._on_close)
            self._update_wraplength()
            self._draw_text_canvas()
            return

        main = tk.Frame(self.window, bg=self.bg_color, bd=1, highlightthickness=1, highlightbackground="#2b303a")
        main.pack(fill=tk.BOTH, expand=True)

        header = tk.Frame(main, bg=self.bg_color)
        header.pack(fill=tk.X, padx=8, pady=(6, 2))
        self.status_label = tk.Label(
            header,
            text=self._status_text(),
            bg=self.bg_color,
            fg=self.original_color,
            font=(self.font_family, 9),
            anchor=tk.W,
        )
        self.status_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.follow_button = self._make_button(header, "", self._toggle_follow)
        self.follow_button.pack(side=tk.LEFT, padx=(4, 0))
        self.lock_button = self._make_button(header, "", self._toggle_lock)
        self.lock_button.pack(side=tk.LEFT, padx=(4, 0))
        self.embed_button = self._make_button(header, "", self._toggle_embed)
        self.embed_button.pack(side=tk.LEFT, padx=(4, 0))
        self.history_button = self._make_button(header, "历史", self._toggle_history_window)
        self.history_button.pack(side=tk.LEFT, padx=(4, 0))
        self._make_button(header, "-", self._minimize).pack(side=tk.LEFT, padx=(4, 0))
        self._make_button(header, "×", self._on_close).pack(side=tk.LEFT, padx=(4, 0))
        self._refresh_buttons()

        self.speaker_label = tk.Label(
            main,
            text="",
            bg=self.bg_color,
            fg=self.speaker_color,
            font=(self.font_family, max(10, self.font_size - 2), "bold"),
            anchor=tk.W,
            justify=tk.LEFT,
            padx=10,
            pady=(4, 0),
        )
        self.speaker_label.pack(fill=tk.X)

        if self.show_original:
            self.original_label = tk.Label(
                main,
                text="",
                bg=self.bg_color,
                fg=self.original_color,
                font=(self.font_family, self.original_font_size),
                anchor=tk.W,
                justify=tk.LEFT,
                padx=10,
                pady=3,
            )
            self.original_label.pack(fill=tk.X)
            separator = tk.Frame(main, bg="#2b303a", height=1)
            separator.pack(fill=tk.X, padx=10, pady=2)

        self.text_widget = tk.Label(
            main,
            text=_WAITING_TEXT,
            bg=self.bg_color,
            fg=self.waiting_color,
            font=(self.font_family, self.font_size, "bold"),
            anchor=tk.W,
            justify=tk.LEFT,
            padx=10,
            pady=6,
        )
        self.text_widget.pack(fill=tk.BOTH, expand=True)

        for widget in (main, header, self.status_label, self.speaker_label, self.original_label, self.text_widget):
            if widget is not None:
                self._bind_drag(widget)

        self.window.bind("<Double-Button-1>", self._toggle_follow)
        self.window.bind("<Configure>", self._update_wraplength)
        self.window.protocol("WM_DELETE_WINDOW", self._on_close)
        self._update_wraplength()

    def _make_button(self, parent: tk.Widget, text: str, command) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg="#1d222b",
            fg=self.text_color,
            activebackground="#2c3440",
            activeforeground=self.text_color,
            relief=tk.FLAT,
            bd=0,
            padx=7,
            pady=1,
            font=(self.font_family, 9),
            takefocus=False,
        )

    def _make_text_control(self, parent: tk.Widget, text: str, command) -> tk.Label:
        label = tk.Label(
            parent,
            text=text,
            bg=self.transparent_key,
            fg=self.original_color,
            activeforeground=self.text_color,
            font=(self.font_family, 9),
            padx=3,
            pady=0,
            cursor="hand2",
        )
        label.bind("<Enter>", lambda _e, w=label: w.config(fg=self.text_color))
        label.bind("<Leave>", lambda _e, w=label: w.config(fg=self.original_color))
        label.bind("<ButtonRelease-1>", lambda _e: command(), add="+")
        return label

    def _toggle_history_window(self) -> None:
        if self.history_window is not None:
            try:
                if self.history_window.winfo_exists():
                    self._close_history_window()
                    return
            except Exception:
                self.history_window = None
                self.history_text = None
        self._create_history_window()

    def _create_history_window(self) -> None:
        if self.window is None:
            return
        win = tk.Toplevel(self.window)
        self.history_window = win
        win.title("历史对话")
        win.configure(bg=self.bg_color)
        win.geometry(self._history_window_geometry())
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self._close_history_window)
        win.bind("<Escape>", lambda _e: self._close_history_window())

        main = tk.Frame(win, bg=self.bg_color, bd=1, highlightthickness=1, highlightbackground="#2b303a")
        main.pack(fill=tk.BOTH, expand=True)

        header = tk.Frame(main, bg=self.bg_color)
        header.pack(fill=tk.X, padx=10, pady=(8, 4))
        title = tk.Label(
            header,
            text="历史对话",
            bg=self.bg_color,
            fg=self.text_color,
            font=(self.font_family, 10, "bold"),
            anchor=tk.W,
        )
        title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._make_button(header, "清空", self._clear_history).pack(side=tk.LEFT, padx=(4, 0))
        self._make_button(header, "关闭", self._close_history_window).pack(side=tk.LEFT, padx=(4, 0))

        body = tk.Frame(main, bg=self.bg_color)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(2, 10))
        scrollbar = tk.Scrollbar(body)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.history_text = tk.Text(
            body,
            bg="#0d1016",
            fg=self.text_color,
            insertbackground=self.text_color,
            selectbackground="#334155",
            selectforeground=self.text_color,
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=8,
            wrap=tk.WORD,
            yscrollcommand=scrollbar.set,
            font=(self.font_family, 10),
            state=tk.DISABLED,
        )
        self.history_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.history_text.yview)
        self._render_history_window()
        self._log("history_window_opened")

    def _history_window_geometry(self) -> str:
        width, height = 560, 420
        x, y = 120, 120
        if self.window is not None:
            try:
                x = int(self.window.winfo_x()) + 24
                y = int(self.window.winfo_y()) - height - 12
                if y < 20:
                    y = int(self.window.winfo_y()) + int(self.window.winfo_height()) + 12
            except Exception:
                pass
        left, top, right, bottom = self._virtual_screen_bounds()
        x = max(left, min(x, right - width))
        y = max(top, min(y, bottom - height))
        return f"{width}x{height}+{x}+{y}"

    def _close_history_window(self) -> None:
        win = self.history_window
        self.history_window = None
        self.history_text = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        self._log("history_window_closed")

    def _clear_history(self) -> None:
        self.dialogue_history.clear()
        self._render_history_window()
        self._log("history_cleared")

    def _add_history_entry(
        self,
        speaker: str,
        original: str,
        translated: str,
        timestamp: float | None = None,
        render: bool = True,
    ) -> bool:
        changed = append_dialogue_history(
            self.dialogue_history,
            make_dialogue_history_entry(speaker, original, translated, timestamp),
            limit=_HISTORY_LIMIT,
        )
        if changed and render:
            self._render_history_window()
        return changed

    def _render_history_window(self) -> None:
        text = self.history_text
        if text is None:
            return
        try:
            content = "\n\n".join(format_dialogue_history_entry(entry) for entry in self.dialogue_history)
            if not content:
                content = "还没有捕获到对话。"
            text.config(state=tk.NORMAL)
            text.delete("1.0", tk.END)
            text.insert(tk.END, content)
            text.config(state=tk.DISABLED)
            text.see(tk.END)
        except Exception as exc:
            self._log(f"history_render_failed: {exc}")

    def _bind_drag(self, widget: tk.Widget) -> None:
        widget.bind("<Button-1>", self._start_drag, add="+")
        widget.bind("<B1-Motion>", self._do_drag, add="+")
        widget.bind("<ButtonRelease-1>", self._end_drag, add="+")

    def _status_text(self) -> str:
        follow = "跟随" if self.follow_window else "固定"
        locked = "锁定" if self.locked else "可拖动"
        return f"{follow} · {locked}"

    def _refresh_buttons(self) -> None:
        if self.status_label is not None:
            self.status_label.config(text=self._status_text())
        if self.follow_button is not None:
            self.follow_button.config(text="跟随" if not self.follow_window else "固定")
        if self.lock_button is not None:
            self.lock_button.config(text="解锁" if self.locked else "锁定")
        if self.embed_button is not None:
            # _embedded = True 表示当前已嵌入，按钮显示"取消嵌入"
            # _embedded = False 表示当前未嵌入，按钮显示"嵌入"
            self.embed_button.config(text="取消嵌入" if self._embedded else "嵌入")

    def _start_drag(self, event) -> None:
        if self.window is None:
            return
        self._dragging = not self.locked
        self.drag_start_root_x = int(getattr(event, "x_root", event.x))
        self.drag_start_root_y = int(getattr(event, "y_root", event.y))
        self.drag_window_x = int(self.window.winfo_x())
        self.drag_window_y = int(self.window.winfo_y())
        self._last_drag_update = 0.0
        self._drag_pending_xy = None
        if not self.locked:
            self._pending_geometry = None
            self.follow_window = False
            self._refresh_buttons()
        self.drag_start_x = event.x
        self.drag_start_y = event.y

    def _do_drag(self, event) -> None:
        if self.locked or self.window is None:
            return
        x, y = self._drag_position(event)
        now = time.time()
        if now - self._last_drag_update < 0.016:
            self._drag_pending_xy = (x, y)
            return
        self._apply_drag_position(x, y)

    def _drag_position(self, event) -> tuple[int, int]:
        root_x = int(getattr(event, "x_root", self.drag_start_root_x))
        root_y = int(getattr(event, "y_root", self.drag_start_root_y))
        return (
            self.drag_window_x + (root_x - self.drag_start_root_x),
            self.drag_window_y + (root_y - self.drag_start_root_y),
        )

    def _apply_drag_position(self, x: int, y: int) -> None:
        if self.window is None:
            return
        self.window.geometry(f"+{x}+{y}")
        self._last_drag_update = time.time()

    def _end_drag(self, event=None) -> None:
        if self._dragging and self.window is not None and not self.locked:
            if event is not None:
                x, y = self._drag_position(event)
            elif self._drag_pending_xy is not None:
                x, y = self._drag_pending_xy
            else:
                x, y = int(self.window.winfo_x()), int(self.window.winfo_y())
            self._apply_drag_position(x, y)
        self._dragging = False
        self._drag_pending_xy = None
        self._update_wraplength()
        self._save_window_state()

    def _toggle_follow(self, _event=None) -> None:
        self.follow_window = not self.follow_window
        if self.follow_window:
            self.game_hwnd = None
        self._refresh_buttons()
        self._save_window_state()

    def _toggle_lock(self) -> None:
        self.locked = not self.locked
        self._refresh_buttons()
        self._save_window_state()

    def _toggle_embed(self) -> None:
        """切换文字嵌入到游戏内对话框的功能。"""
        if not self._embedded:
            # 显示确认对话框
            if self.window is None:
                return
            result = self._show_embed_confirmation()
            if not result:
                return
            self._embedded = True
            self._enable_embed_mode()
        else:
            self._embedded = False
            self._disable_embed_mode()
        self._refresh_buttons()
        self._save_window_state()

    def _show_embed_confirmation(self) -> bool:
        """显示嵌入功能的确认对话框。"""
        try:
            import tkinter.messagebox as messagebox
            return messagebox.askyesno(
                "嵌入确认",
                "是否启用文字嵌入到游戏内对话框？\n\n"
                "注意：部分游戏可能无法成功嵌入\n"
                "如果游戏出现显示异常，请点击\"取消嵌入\"按钮。",
                parent=self.window
            )
        except Exception as exc:
            self._log(f"embed_confirmation_dialog_failed: {exc}")
            return True

    def _enable_embed_mode(self) -> None:
        """启用文字嵌入模式。"""
        try:
            # 通过共享内存通知 native hook 启用嵌入
            control_shm_name = f"{self.shm_name}_control"
            shm = mmap.mmap(-1, 4096, tagname=control_shm_name, access=mmap.ACCESS_WRITE)
            shm.seek(0)
            # 写入控制标志：1 = 启用嵌入
            shm.write(struct.pack("I", 1))
            shm.close()
            self._log("embed_mode_enabled")
        except Exception as exc:
            self._log(f"enable_embed_failed: {exc}")

    def _disable_embed_mode(self) -> None:
        """禁用文字嵌入模式。"""
        try:
            # 通过共享内存通知 native hook 禁用嵌入
            control_shm_name = f"{self.shm_name}_control"
            shm = mmap.mmap(-1, 4096, tagname=control_shm_name, access=mmap.ACCESS_WRITE)
            shm.seek(0)
            # 写入控制标志：0 = 禁用嵌入
            shm.write(struct.pack("I", 0))
            shm.close()
            self._log("embed_mode_disabled")
        except Exception as exc:
            self._log(f"disable_embed_failed: {exc}")

    def _minimize(self) -> None:
        if self.window is None:
            return
        try:
            self._save_window_state()
            self._taskbar_minimized = True
            self._hidden_for_recall = False
            self.window.attributes("-topmost", False)
            self.window.overrideredirect(False)
            self.window.iconify()
            self._log("overlay_minimized")
        except Exception:
            pass

    def _hide_for_recall(self) -> None:
        if self.window is None:
            return
        try:
            self._save_window_state()
            self._taskbar_minimized = True
            self._hidden_for_recall = True
            self.window.withdraw()
            self._log("overlay_hidden_for_recall")
        except Exception as exc:
            self._log(f"overlay_hide_failed: {exc}")

    def _on_window_mapped(self, _event=None) -> None:
        if not self._taskbar_minimized or self.window is None:
            return

        def restore_chrome():
            if self.window is None:
                return
            try:
                self._taskbar_minimized = False
                self.window.overrideredirect(True)
                self.window.attributes("-topmost", True)
                self.window.lift()
                self._update_wraplength()
            except Exception as exc:
                self._log(f"restore_chrome_failed: {exc}")

        self.window.after(120, restore_chrome)

    def _save_window_state(self) -> None:
        if self.window is None:
            return
        cfg = get_config()
        try:
            cfg.kirikiri_overlay_follow_window = bool(self.follow_window)
            cfg.kirikiri_overlay_locked = bool(self.locked)
            cfg.kirikiri_overlay_embedded = bool(self._embedded)
            if self.remember_position:
                cfg.kirikiri_overlay_saved_x = int(self.window.winfo_x())
                cfg.kirikiri_overlay_saved_y = int(self.window.winfo_y())
                cfg.kirikiri_overlay_saved_width = min(int(self.window.winfo_width()), int(self.max_width))
            save_config()
        except Exception:
            pass

    def _draw_text_canvas(self) -> None:
        canvas = self.text_canvas
        if canvas is None:
            return
        try:
            canvas.delete("subtitle")
            width = self._game_text_wrap_width(canvas)
            y = 2
            if self.show_speaker and self._display_speaker:
                y = self._draw_outlined_text(
                    canvas,
                    6,
                    y,
                    self._display_speaker,
                    (self.font_family, max(10, self.font_size - 2), "bold"),
                    self.speaker_color,
                    width,
                ) + 2
            if self.show_original and self._display_original:
                y = self._draw_outlined_text(
                    canvas,
                    6,
                    y,
                    self._display_original,
                    (self.font_family, self.original_font_size),
                    self.original_color,
                    width,
                ) + 4
            self._draw_outlined_text(
                canvas,
                6,
                y,
                self._display_text or _WAITING_TEXT,
                (self.font_family, self.font_size),
                self._display_color or self.text_color,
                width,
            )
        except Exception as exc:
            self._log(f"draw_text_failed: {exc}")

    def _draw_outlined_text(
        self,
        canvas: tk.Canvas,
        x: int,
        y: int,
        text: str,
        font: tuple,
        fill: str,
        width: int,
    ) -> int:
        shadow = "#05070a"
        wrapped_text = self._wrap_text_like_game(text, font, width)
        outline_offsets = [(-1, 0), (1, 0), (0, -1), (0, 1), (1, 1)]
        for ox, oy in outline_offsets:
            canvas.create_text(
                x + ox,
                y + oy,
                text=wrapped_text,
                fill=shadow,
                font=font,
                anchor=tk.NW,
                justify=tk.LEFT,
                tags=("subtitle",),
            )
        item = canvas.create_text(
            x,
            y,
            text=wrapped_text,
            fill=fill,
            font=font,
            anchor=tk.NW,
            justify=tk.LEFT,
            tags=("subtitle",),
        )
        bbox = canvas.bbox(item)
        return int(bbox[3]) if bbox else y + int(font[1]) + 4

    def _game_text_wrap_width(self, canvas: tk.Canvas) -> int:
        canvas_width = int(canvas.winfo_width() or (self.window.winfo_width() if self.window else 900))
        canvas_width = max(240, canvas_width - 12)
        if not self._last_game_rect:
            return canvas_width
        game_width = int(self._last_game_rect.get("width") or 0)
        if game_width <= 0:
            return canvas_width
        # KiriKiri/KAG 对话框通常左右留出约 7%-10% 边距。这里用游戏窗口宽度
        # 推出文本框宽度，再与当前字幕窗口宽度取较小值，避免字幕比游戏多塞一行字。
        game_text_width = max(360, int(game_width * 0.86))
        return min(canvas_width, game_text_width)

    def _wrap_text_like_game(self, text: str, font_spec: tuple, max_width: int) -> str:
        text = str(text or "")
        if not text:
            return ""
        max_width = max(120, int(max_width))
        try:
            font = tkfont.Font(font=font_spec)
        except Exception:
            return text

        wrapped: list[str] = []
        for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if paragraph == "":
                wrapped.append("")
                continue
            wrapped.extend(self._wrap_paragraph_like_game(paragraph, font, max_width))
        return "\n".join(wrapped)

    def _wrap_paragraph_like_game(self, text: str, font: tkfont.Font, max_width: int) -> list[str]:
        closing = set("，。！？、；：）」』】》〉）〕］…,.!?;:%")
        opening = set("「『（《〈【〔［")
        lines: list[str] = []
        current = ""

        for ch in text:
            candidate = current + ch
            if not current or font.measure(candidate) <= max_width:
                current = candidate
                continue

            if ch in closing:
                lines.append(candidate)
                current = ""
                continue

            if current[-1:] in opening and lines:
                previous = lines.pop()
                lines.append(previous + current[-1])
                current = current[:-1] + ch
                continue

            lines.append(current.rstrip())
            current = ch.lstrip()

        if current:
            lines.append(current.rstrip())
        return lines or [text]

    def _update_wraplength(self, _event=None) -> None:
        if self.window is None:
            return
        if self._dragging and _event is not None:
            return
        if self.text_canvas is not None:
            self._draw_text_canvas()
            return
        wrap = max(240, self.window.winfo_width() - 28)
        if self.original_label is not None:
            self.original_label.config(wraplength=wrap)
        if self.text_widget is not None:
            self.text_widget.config(wraplength=wrap)

    def _read_shm_loop(self) -> None:
        shm = None
        consecutive_errors = 0
        while self.running:
            try:
                if shm is None:
                    try:
                        # Use a writable mapping even though the overlay only reads.
                        # On Windows, mmap creates the named section when it wins the
                        # startup race; creating it read-only prevents the native hook
                        # from mapping the same section for writes.
                        shm = mmap.mmap(-1, 65536, tagname=self.shm_name, access=mmap.ACCESS_WRITE)
                        consecutive_errors = 0
                    except (FileNotFoundError, OSError):
                        time.sleep(0.5)
                        continue

                shm.seek(0)
                payload = shm.read(65536)
                decoded = decode_overlay_payload(payload)
                if decoded is None:
                    time.sleep(0.1)
                    continue
                seq, original, translated, speaker = decoded
                if seq == self._last_seq:
                    time.sleep(0.08)
                    continue

                self._last_seq = seq
                self._enqueue_text(original, translated, seq, speaker)
                time.sleep(0.05)
                consecutive_errors = 0
            except Exception:
                consecutive_errors += 1
                if consecutive_errors > 10:
                    if shm:
                        try:
                            shm.close()
                        except Exception:
                            pass
                    shm = None
                    consecutive_errors = 0
                time.sleep(1)

        if shm:
            try:
                shm.close()
            except Exception:
                pass

    def _enqueue_text(self, original: str, translated: str, seq: int, speaker: str = "") -> None:
        display_speaker, _display_original, translate_text = runtime_dialogue_parts(original, speaker)
        if original and not translated:
            cached = self.local_translations.get(original)
            if not cached and translate_text != original:
                cached = self.local_translations.get(translate_text)
            if cached:
                translated = cached
            elif self.live_translate:
                self._request_live_translation(translate_text, display_speaker, display_original=original)
        if translated:
            _translated_speaker, _translated_display, translated_text = runtime_dialogue_parts(
                translated, display_speaker
            )
            self._translation_context.add(
                translate_text,
                translated_text,
                display_speaker,
            )
        item = {
            "original": original,
            "translated": translated,
            "speaker": display_speaker,
            "translate_key": translate_text,
            "seq": seq,
            "timestamp": time.time(),
        }
        try:
            self.text_queue.put_nowait(item)
        except queue.Full:
            try:
                self.text_queue.get_nowait()
                self.text_queue.put_nowait(item)
            except Exception:
                pass

    def _request_live_translation(self, original: str, speaker: str = "", display_original: str = "") -> None:
        original = (original or "").strip()
        if not original or original in self._translation_inflight or original in self.local_translations:
            return
        self._translation_inflight.add(original)
        request = {
            "original": original,
            "speaker": normalize_speaker_name(speaker),
            "display_original": str(display_original or original),
            "context": self._translation_context.snapshot(),
        }
        try:
            self.translate_queue.put_nowait(request)
        except queue.Full:
            try:
                self.translate_queue.get_nowait()
                self.translate_queue.put_nowait(request)
            except Exception:
                self._translation_inflight.discard(original)

    def _translation_worker_loop(self) -> None:
        while self.running:
            try:
                request = self.translate_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if isinstance(request, dict):
                original = str(request.get("original") or "")
                speaker = normalize_speaker_name(str(request.get("speaker") or ""))
                display_original = str(request.get("display_original") or original)
                context = list(request.get("context") or [])
            else:
                original = str(request or "")
                speaker = ""
                display_original = original
                context = []
            try:
                translated = translate_with_empty_retry(
                    lambda: self._translate_live_text(original, context=context, speaker=speaker),
                    original,
                    on_retry=lambda reason, attempt: self._log(
                        f"live_translate_retry: attempt={attempt + 1} reason={reason} text={original[:60]}"
                    ),
                )
                if translated and translated != original:
                    self._append_local_translation(original, translated)
                    self._enqueue_text(display_original, translated, self._last_seq + 1, speaker)
                    self._log(f"live_translated: {original[:60]}")
                else:
                    self._log(f"live_translate_empty: {original[:60]}")
            except Exception as exc:
                self._log(f"live_translate_failed: {exc}")
            finally:
                self._translation_inflight.discard(original)

    def _translate_live_text(
        self,
        original: str,
        *,
        context: list[dict[str, str]] | None = None,
        speaker: str = "",
    ) -> str:
        async def run_once() -> str:
            translator = self._get_translator()
            realtime_translate = getattr(translator, "translate_realtime_text", None)
            if callable(realtime_translate):
                return str(
                    await realtime_translate(
                        original,
                        "ja",
                        str(getattr(get_config(), "target_lang", "zh-CN") or "zh-CN"),
                        context=context or [],
                        speaker=speaker,
                    )
                    or ""
                ).strip()
            item = TextItem(
                file="__kirikiri_live__.jsonl",
                key=hashlib.sha1(original.encode("utf-8")).hexdigest()[:16],
                original=original,
                translated="",
                context="message",
                meta={"kind": "message", "runtime_capture": True},
            )
            cfg = get_config()
            result = await translator.translate_batch([item], "ja", str(getattr(cfg, "target_lang", "zh-CN") or "zh-CN"))
            if result and result[0].translated:
                return str(result[0].translated).strip()
            return ""

        return asyncio.run(run_once())

    def _get_translator(self):
        if self._translator is not None:
            return self._translator
        active = str(getattr(get_config(), "active_translator", "deepseek") or "deepseek").lower()
        from translators.factory import create_translator

        self._translator = create_translator(active)
        self._translator_provider = active
        if self._translator is None:
            self._translator = create_translator("deepseek")
            self._translator_provider = "deepseek"
            self._log(f"live_translator_fallback requested={active} provider=deepseek")
        else:
            self._log(f"live_translator_provider={active}")
        return self._translator

    def _update_display(self) -> None:
        if not self.running:
            return
        if self.window is None:
            return
        if self._close_requested:
            self._shutdown("game_closed")
            return
        self._consume_restore_request()
        self._reload_config_if_changed()

        if self._pending_geometry and not self.locked:
            try:
                w, h, x, y = self._pending_geometry
                self.window.geometry(f"{w}x{h}+{x}+{y}")
                self._pending_geometry = None
                self._update_wraplength()
            except Exception:
                pass

        texts_batch = []
        while not self.text_queue.empty():
            try:
                texts_batch.append(self.text_queue.get_nowait())
            except queue.Empty:
                break

        if texts_batch:
            history_changed = False
            for history_item in texts_batch:
                history_original = str(history_item.get("original", "") or "")
                history_translated = str(history_item.get("translated", "") or "")
                history_explicit_speaker = normalize_speaker_name(str(history_item.get("speaker", "") or ""))
                history_original_speaker, history_original_body = split_speaker_text(history_original)
                history_translated_speaker, history_translated_body = split_speaker_text(history_translated)
                history_speaker = history_explicit_speaker or history_translated_speaker or history_original_speaker
                if history_speaker:
                    history_speaker = normalize_speaker_name(self.local_translations.get(history_speaker, history_speaker))
                history_original_display = history_original_body if history_original_speaker else history_original
                history_translated_display = history_translated_body if history_translated_speaker else history_translated
                history_changed = self._add_history_entry(
                    history_speaker,
                    history_original_display,
                    history_translated_display,
                    history_item.get("timestamp"),
                    render=False,
                ) or history_changed
            if history_changed:
                self._render_history_window()

            latest = texts_batch[-1]
            original = latest.get("original", "")
            translated = latest.get("translated", "")
            translate_key = latest.get("translate_key", original)
            explicit_speaker = normalize_speaker_name(str(latest.get("speaker", "")))
            original_speaker, original_body = split_speaker_text(original)
            translated_speaker, translated_body = split_speaker_text(translated)
            speaker = explicit_speaker or translated_speaker or original_speaker
            if speaker:
                speaker = normalize_speaker_name(self.local_translations.get(speaker, speaker))
            original_display = original_body if original_speaker else original

            if self.original_label is not None:
                original_text = original_display if self.text_only else f"{_ORIGINAL_PREFIX}{original_display}"
                self.original_label.config(text=original_text if original_display and self.show_original else "")
            if self.speaker_label is not None:
                self.speaker_label.config(text=speaker if self.show_speaker and speaker else "")
            if original:
                self.last_original = original

            if translated:
                display_text = translated_body if translated_speaker else translated
                display_color = self.text_color
                display_state = "translated"
            elif original:
                display_text = _PENDING_TEXT if translate_key in self._translation_inflight else _UNTRANSLATED_TEXT
                display_color = self.pending_color
                display_state = "pending"
            else:
                display_text = _WAITING_TEXT
                display_color = self.waiting_color
                display_state = "waiting"

            if self.text_canvas is not None:
                self._display_speaker = speaker if self.show_speaker else ""
                self._display_original = original_display if self.show_original else ""
                self._display_text = display_text
                self._display_color = display_color
                self._display_state = display_state
                self._draw_text_canvas()
            elif self.text_widget is not None:
                self._display_state = display_state
                self.text_widget.config(text=display_text, fg=display_color)

        self.window.after(100, self._update_display)

    def _on_close(self) -> None:
        self._hide_for_recall()

    def _shutdown(self, reason: str = "shutdown") -> None:
        self._log(f"overlay_shutdown reason={reason}")
        self._save_window_state()
        self.running = False
        self._close_history_window()
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass

    def stop(self) -> None:
        self._shutdown("stop")


def run_overlay_window(game_name: str = "游戏翻译", game_exe: str = "GAME.exe", game_exe_path: str = "", shm_name: str = "") -> None:
    window = TranslationOverlayWindow(game_name, game_exe, game_exe_path, shm_name)
    window.start()


if __name__ == "__main__":
    import sys

    game_name = sys.argv[1] if len(sys.argv) > 1 else "游戏"
    game_exe = sys.argv[2] if len(sys.argv) > 2 else "GAME.exe"
    game_exe_path = sys.argv[3] if len(sys.argv) > 3 else ""
    shm_name = sys.argv[4] if len(sys.argv) > 4 else ""
    run_overlay_window(game_name, game_exe, game_exe_path, shm_name)
