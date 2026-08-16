from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _windows_libraries():
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    return user32, kernel32


def _open_windows_clipboard(user32, retries: int = 10) -> bool:
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def _get_windows_clipboard_text() -> str:
    user32, kernel32 = _windows_libraries()

    if not _open_windows_clipboard(user32):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _set_windows_clipboard_text(text: str) -> bool:
    user32, kernel32 = _windows_libraries()

    payload = (str(text or "") + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        return False
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        return False
    try:
        ctypes.memmove(pointer, payload, len(payload))
    finally:
        kernel32.GlobalUnlock(handle)

    if not _open_windows_clipboard(user32):
        kernel32.GlobalFree(handle)
        return False
    transferred = False
    try:
        if not user32.EmptyClipboard():
            return False
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            return False
        transferred = True
        return True
    finally:
        user32.CloseClipboard()
        if not transferred:
            kernel32.GlobalFree(handle)


def _get_tk_clipboard_text() -> str:
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    try:
        return root.clipboard_get()
    finally:
        root.destroy()


def _set_tk_clipboard_text(text: str) -> bool:
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    try:
        root.clipboard_clear()
        root.clipboard_append(str(text or ""))
        root.update()
        return True
    finally:
        root.destroy()


def get_clipboard_text() -> str:
    try:
        if sys.platform == "win32":
            return _get_windows_clipboard_text()
        return _get_tk_clipboard_text()
    except Exception:
        return ""


def set_clipboard_text(text: str) -> bool:
    try:
        if sys.platform == "win32":
            return _set_windows_clipboard_text(text)
        return _set_tk_clipboard_text(text)
    except Exception:
        return False
