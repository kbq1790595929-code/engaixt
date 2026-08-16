from __future__ import annotations

import argparse
import ctypes
import json
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = wintypes.HWND(-1)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
PM_REMOVE = 0x0001

BI_RGB = 0
DIB_RGB_COLORS = 0
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
HICON = getattr(wintypes, "HICON", wintypes.HANDLE)
HCURSOR = getattr(wintypes, "HCURSOR", wintypes.HANDLE)
HBRUSH = getattr(wintypes, "HBRUSH", wintypes.HANDLE)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", HCURSOR),
        ("hbrBackground", HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", HICON),
    ]


user32.FindWindowW.restype = wintypes.HWND
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND,
    wintypes.HDC,
    ctypes.POINTER(POINT),
    ctypes.POINTER(SIZE),
    wintypes.HDC,
    ctypes.POINTER(POINT),
    wintypes.COLORREF,
    ctypes.POINTER(BLENDFUNCTION),
    wintypes.DWORD,
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
user32.PeekMessageW.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(BITMAPINFO),
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


@dataclass
class OverlayText:
    key: str
    text: str
    x: float
    y: float
    font_size: int = 30
    color: tuple[int, int, int, int] = (255, 244, 255, 255)
    shadow: tuple[int, int, int, int] = (34, 0, 24, 230)
    anchor: str = "mm"


def _set_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def _find_window(title: str) -> wintypes.HWND | None:
    hwnd = user32.FindWindowW(None, title)
    if hwnd:
        return hwnd

    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(candidate, _lparam):
        if not user32.IsWindowVisible(candidate):
            return True
        length = user32.GetWindowTextLengthW(candidate)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(candidate, buf, length + 1)
        if title.lower() in buf.value.lower():
            found.append(int(candidate))
            return False
        return True

    user32.EnumWindows(enum_proc, 0)
    return wintypes.HWND(found[0]) if found else None


def _find_window_by_pid(pid: int) -> wintypes.HWND | None:
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(candidate, _lparam):
        if not user32.IsWindowVisible(candidate):
            return True
        proc_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(candidate, ctypes.byref(proc_id))
        if int(proc_id.value) != int(pid):
            return True
        length = user32.GetWindowTextLengthW(candidate)
        if length <= 0:
            return True
        found.append(int(candidate))
        return False

    user32.EnumWindows(enum_proc, 0)
    return wintypes.HWND(found[0]) if found else None


def _get_client_rect_on_screen(hwnd: wintypes.HWND) -> tuple[int, int, int, int] | None:
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    origin = POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    width = max(1, rect.right - rect.left)
    height = max(1, rect.bottom - rect.top)
    return origin.x, origin.y, width, height


def _load_checkpoint(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    for raw in data.get("items", []):
        original = str(raw.get("original", "")).strip()
        translated = str(raw.get("translated", "")).strip()
        if original and translated and translated != original:
            result.setdefault(original, translated)
    return result


def _default_voidigo_menu_items(translations: dict[str, str]) -> list[OverlayText]:
    def t(original: str, fallback: str) -> str:
        return translations.get(original, fallback)

    return [
        OverlayText("PLAY", t("PLAY", "开始游戏"), 0.735, 0.875, 32),
        OverlayText("SETTINGS", t("SETTINGS", "设置"), 0.748, 0.965, 28),
        OverlayText("CREDITS", t("CREDITS", "制作人员"), 0.748, 1.055, 25),
        OverlayText("PATCH NOTES", t("PATCH NOTES", "更新日志"), 0.748, 1.145, 25),
        OverlayText("QUIT", t("QUIT", "退出"), 0.748, 1.235, 25),
    ]


def _find_font_path(bold: bool = True) -> Path | None:
    try:
        from core.open_source_fonts import ensure_source_han_sans
        font = ensure_source_han_sans()
        if font:
            return font
    except Exception:
        pass
    names = [
        "SourceHanSansCN-Regular.otf",
        "SourceHanSansSC-Regular.otf",
        "SourceHanSansHWSC-Regular.otf",
        "NotoSansCJKsc-Regular.otf",
        "NotoSansSC-Regular.otf",
        "NotoSansSC-VF.ttf",
    ]
    for name in names:
        path = Path("C:/Windows/Fonts") / name
        if path.exists():
            return path
    return None


def _wrap_text(draw, text: str, font, max_width: int) -> str:
    lines: list[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        current = ""
        for ch in paragraph:
            candidate = current + ch
            try:
                width = draw.textlength(candidate, font=font)
            except Exception:
                width = len(candidate) * 18
            if current and width > max_width:
                lines.append(current)
                current = ch
            else:
                current = candidate
        if current:
            lines.append(current)
    return "\n".join(lines)


class NativeRuntimeOverlay:
    def __init__(
        self,
        window_title: str,
        items: list[OverlayText],
        fixed_rect: tuple[int, int, int, int] | None = None,
        target_pid: int | None = None,
        live_file: Path | None = None,
    ):
        _set_dpi_aware()
        self.window_title = window_title
        self.items = items
        self.fixed_rect = fixed_rect
        self.target_pid = target_pid
        self.live_file = live_file
        self._last_live_mtime = 0
        self.class_name = f"GameTranslatorOverlay_{kernel32.GetCurrentProcessId()}"
        self.hinstance = kernel32.GetModuleHandleW(None)
        self._wnd_proc_ref = WNDPROC(self._wnd_proc)
        self.hwnd = self._create_window()
        self.last_rect: tuple[int, int, int, int] | None = None
        self.font_path = _find_font_path(bold=True) or _find_font_path(bold=False)

    def run(self, duration: float | None = None) -> None:
        started = time.time()
        try:
            while duration is None or time.time() - started < duration:
                self._pump_messages()
                self._tick()
                time.sleep(0.05)
        finally:
            if self.hwnd:
                user32.DestroyWindow(self.hwnd)

    def _create_window(self) -> wintypes.HWND:
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wnd_proc_ref
        wc.hInstance = self.hinstance
        wc.lpszClassName = self.class_name
        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            raise ctypes.WinError()
        ex_style = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE
        hwnd = user32.CreateWindowExW(
            ex_style,
            self.class_name,
            "Game Translator Overlay",
            WS_POPUP,
            0,
            0,
            1,
            1,
            None,
            None,
            self.hinstance,
            None,
        )
        if not hwnd:
            raise ctypes.WinError()
        return hwnd

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg in (WM_DESTROY, WM_CLOSE):
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _pump_messages(self) -> None:
        msg = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _tick(self) -> None:
        rect = self.fixed_rect
        if rect is None:
            hwnd = _find_window_by_pid(self.target_pid) if self.target_pid else _find_window(self.window_title)
            rect = _get_client_rect_on_screen(hwnd) if hwnd else None
        if not rect:
            user32.ShowWindow(self.hwnd, SW_HIDE)
            self.last_rect = None
            return
        x, y, width, height = rect
        if width <= 1 or height <= 1:
            return
        self._refresh_live_items()
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)
        self._render(x, y, width, height)
        self.last_rect = rect

    def _refresh_live_items(self) -> None:
        if not self.live_file or not self.live_file.exists():
            return
        try:
            stat = self.live_file.stat()
            if stat.st_mtime_ns == self._last_live_mtime:
                return
            self._last_live_mtime = stat.st_mtime_ns
            data = json.loads(self.live_file.read_text(encoding="utf-8-sig"))
            text = str(data.get("text") or "").strip()
            if not text:
                self.items = []
                return
            self.items = [
                OverlayText(
                    key="live",
                    text=text,
                    x=float(data.get("x", 0.5)),
                    y=float(data.get("y", 0.78)),
                    font_size=int(data.get("font_size", 34)),
                    color=tuple(data.get("color", (255, 246, 255, 255))),
                    shadow=tuple(data.get("shadow", (20, 18, 28, 235))),
                    anchor="mm",
                )
            ]
        except Exception:
            return

    def _render(self, x: int, y: int, width: int, height: int) -> None:
        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        scale = min(width / 1280, height / 720)
        for item in self.items:
            px = int(width * item.x)
            py = int(height * item.y)
            size = max(16, int(item.font_size * scale))
            try:
                font = ImageFont.truetype(str(self.font_path), size=size) if self.font_path else ImageFont.load_default()
            except Exception:
                font = ImageFont.load_default()
            stroke = max(2, int(size * 0.11))
            text = _wrap_text(draw, item.text, font, int(width * 0.76))
            draw.multiline_text(
                (px, py),
                text,
                font=font,
                anchor=item.anchor,
                fill=item.color,
                stroke_width=stroke,
                stroke_fill=item.shadow,
                align="center",
            )
        self._update_layered_window(x, y, image)

    def _update_layered_window(self, x: int, y: int, image) -> None:
        width, height = image.size
        bgra = image.tobytes("raw", "BGRA")
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bitmap = gdi32.CreateDIBSection(screen_dc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not bitmap or not bits:
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
            if screen_dc:
                user32.ReleaseDC(None, screen_dc)
            return
        old = gdi32.SelectObject(mem_dc, bitmap)
        ctypes.memmove(bits, bgra, len(bgra))
        dst = POINT(x, y)
        size = SIZE(width, height)
        src = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(self.hwnd, screen_dc, ctypes.byref(dst), ctypes.byref(size), mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)


def _parse_rect(raw: str | None) -> tuple[int, int, int, int] | None:
    if not raw:
        return None
    parts = [int(p.strip()) for p in raw.split(",")]
    if len(parts) != 4:
        raise ValueError("--rect must be x,y,width,height")
    return parts[0], parts[1], max(1, parts[2]), max(1, parts[3])


def main() -> int:
    parser = argparse.ArgumentParser(description="External runtime translation overlay.")
    parser.add_argument("--window-title", default="Voidigo")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--profile", choices=["voidigo-menu", "live-text"], default="voidigo-menu")
    parser.add_argument("--target-pid", type=int)
    parser.add_argument("--live-file", type=Path)
    parser.add_argument("--duration", type=float, default=0, help="Seconds to run; 0 means until closed.")
    parser.add_argument("--rect", help="Debug fixed rectangle: x,y,width,height.")
    args = parser.parse_args()

    translations = _load_checkpoint(args.checkpoint) if args.checkpoint and args.checkpoint.exists() else {}
    items = _default_voidigo_menu_items(translations) if args.profile == "voidigo-menu" else []
    duration = args.duration if args.duration > 0 else None
    NativeRuntimeOverlay(
        args.window_title,
        items,
        fixed_rect=_parse_rect(args.rect),
        target_pid=args.target_pid,
        live_file=args.live_file,
    ).run(duration=duration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
