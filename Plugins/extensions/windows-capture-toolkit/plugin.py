from __future__ import annotations

import binascii
import ctypes
import hashlib
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from ctypes import wintypes
from pathlib import Path
from typing import Any

from folderbridge_mcp.extension_api import ExtensionError


VERSION = "0.1.3"
MAX_CAPTURE_PIXELS = 35_000_000
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
BI_RGB = 0
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
MONITORINFOF_PRIMARY = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_SHOWNOACTIVATE = 4
SW_SHOWMINNOACTIVE = 7
VCS_BLOCKED_SEGMENTS = {".git", ".hg", ".svn"}
WORKSPACE_BLOCKED_SEGMENTS = {
    ".idea", ".mypy_cache", ".next", ".pytest_cache", ".ruff_cache", ".tox",
    ".venv", ".vscode", "__pycache__", "build", "coverage", "dist",
    "node_modules", "target", "vendor",
}
WINDOWS_RESERVED_BASES = {
    "con", "prn", "aux", "nul", "clock$",
    *{f"com{i}" for i in range(1, 10)},
    *{f"lpt{i}" for i in range(1, 10)},
}
HANDLE_RE = re.compile(r"^0x[0-9A-Fa-f]+$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _fail(code: str, message: str, **details: Any) -> ExtensionError:
    return ExtensionError(code, message, **details)


def _require_windows() -> None:
    if os.name != "nt" or not hasattr(ctypes, "windll"):
        raise _fail("WINDOWS_CAPTURE_UNAVAILABLE", "Windows Capture Toolkit is available only on an interactive Windows desktop")


def _enable_dpi_awareness() -> str:
    _require_windows()
    user32 = ctypes.windll.user32
    try:
        func = user32.SetProcessDpiAwarenessContext
        func.argtypes = [ctypes.c_void_p]
        func.restype = wintypes.BOOL
        if func(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except Exception:
        pass
    try:
        if user32.SetProcessDPIAware():
            return "system-aware"
    except Exception:
        pass
    return "unchanged"


_DPI_MODE: str | None = None


def _dpi_mode() -> str:
    global _DPI_MODE
    if _DPI_MODE is None:
        _DPI_MODE = _enable_dpi_awareness()
    return _DPI_MODE


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
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
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", RGBQUAD * 1),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


_WIN32_CONFIGURED = False


def _configure_win32() -> None:
    global _WIN32_CONFIGURED
    _require_windows()
    if _WIN32_CONFIGURED:
        return
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32

    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user32.mouse_event.restype = None
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL

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
    gdi32.CreateDIBSection.restype = wintypes.HANDLE
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi32.SelectObject.restype = wintypes.HANDLE
    gdi32.BitBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    try:
        dwmapi = ctypes.windll.dwmapi
        dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    except Exception:
        pass
    _WIN32_CONFIGURED = True


def _handle_value(value: Any) -> int:
    if isinstance(value, int):
        return value
    raw = ctypes.cast(value, ctypes.c_void_p).value
    return int(raw or 0)


def _rect_dict(rect: RECT) -> dict[str, int]:
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    return {
        "x": int(rect.left),
        "y": int(rect.top),
        "width": width,
        "height": height,
        "right": int(rect.right),
        "bottom": int(rect.bottom),
    }


def _parse_handle(value: Any) -> int:
    if not isinstance(value, str) or not HANDLE_RE.fullmatch(value):
        raise _fail("WINDOW_HANDLE_INVALID", "window_handle must be the exact hexadecimal handle returned by list-windows")
    try:
        handle = int(value, 16)
    except ValueError as exc:
        raise _fail("WINDOW_HANDLE_INVALID", "window_handle could not be parsed") from exc
    if handle <= 0:
        raise _fail("WINDOW_HANDLE_INVALID", "window_handle must be non-zero")
    return handle


def _hwnd_text(hwnd: int) -> str:
    return f"0x{int(hwnd):X}"


def _process_name(pid: int) -> str | None:
    if not pid:
        return None
    _configure_win32()
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    try:
        query = kernel32.QueryFullProcessImageNameW
        query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        query.restype = wintypes.BOOL
    except Exception:
        return None
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not process:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not query(process, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value).name or None
    finally:
        kernel32.CloseHandle(process)


def _window_title(hwnd: int) -> str:
    _configure_win32()
    user32 = ctypes.windll.user32
    length = int(user32.GetWindowTextLengthW(wintypes.HWND(hwnd)))
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buffer, length + 1)
    return buffer.value


def _window_pid(hwnd: int) -> int:
    _configure_win32()
    pid = wintypes.DWORD(0)
    ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _is_cloaked(hwnd: int) -> bool:
    _configure_win32()
    try:
        value = wintypes.DWORD(0)
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            DWMWA_CLOAKED,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        return result == 0 and bool(value.value)
    except Exception:
        return False


def _extended_window_rect(hwnd: int) -> RECT:
    _configure_win32()
    user32 = ctypes.windll.user32
    if not user32.IsWindow(wintypes.HWND(hwnd)):
        raise _fail("WINDOW_NOT_FOUND", "The requested window handle no longer exists", window_handle=_hwnd_text(hwnd))
    rect = RECT()
    try:
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if result == 0 and rect.right > rect.left and rect.bottom > rect.top:
            return rect
    except Exception:
        pass
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        raise _fail("WINDOW_RECT_FAILED", "Could not obtain the requested window rectangle", window_handle=_hwnd_text(hwnd))
    return rect


def _raw_window_rect(hwnd: int) -> RECT:
    _configure_win32()
    user32 = ctypes.windll.user32
    rect = RECT()
    if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        raise _fail("WINDOW_RECT_FAILED", "Could not obtain the requested window rectangle", window_handle=_hwnd_text(hwnd))
    return rect


def _client_window_rect(hwnd: int) -> RECT:
    _configure_win32()
    user32 = ctypes.windll.user32
    client = RECT()
    if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(client)):
        raise _fail("WINDOW_RECT_FAILED", "Could not obtain the requested window client rectangle", window_handle=_hwnd_text(hwnd))
    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(origin)):
        raise _fail("WINDOW_RECT_FAILED", "Could not translate the client rectangle to screen coordinates", window_handle=_hwnd_text(hwnd))
    width = int(client.right - client.left)
    height = int(client.bottom - client.top)
    return RECT(origin.x, origin.y, origin.x + width, origin.y + height)


def _window_record(hwnd: int) -> dict[str, Any]:
    _configure_win32()
    user32 = ctypes.windll.user32
    rect = _extended_window_rect(hwnd)
    pid = _window_pid(hwnd)
    return {
        "window_handle": _hwnd_text(hwnd),
        "title": _window_title(hwnd),
        "pid": pid,
        "process_name": _process_name(pid),
        "visible": bool(user32.IsWindowVisible(wintypes.HWND(hwnd))),
        "minimized": bool(user32.IsIconic(wintypes.HWND(hwnd))),
        "cloaked": _is_cloaked(hwnd),
        "rect": _rect_dict(rect),
    }


def _list_windows(params: dict[str, Any]) -> list[dict[str, Any]]:
    _dpi_mode()
    _configure_win32()
    user32 = ctypes.windll.user32
    title_filter = str(params.get("title_contains") or "").casefold()
    process_filter = str(params.get("process_name") or "").casefold()
    include_untitled = bool(params.get("include_untitled", False))
    max_results = int(params.get("max_results", 100))
    results: list[dict[str, Any]] = []

    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd: wintypes.HWND, _lparam: wintypes.LPARAM) -> bool:
        if len(results) >= max_results:
            return False
        raw = _handle_value(hwnd)
        if not user32.IsWindowVisible(hwnd) or _is_cloaked(raw):
            return True
        title = _window_title(raw)
        if not title and not include_untitled:
            return True
        if title_filter and title_filter not in title.casefold():
            return True
        pid = _window_pid(raw)
        process_name = _process_name(pid)
        if process_filter and process_filter != str(process_name or "").casefold():
            return True
        try:
            record = _window_record(raw)
        except Exception:
            return True
        if record["rect"]["width"] <= 0 or record["rect"]["height"] <= 0:
            return True
        results.append(record)
        return True

    user32.EnumWindows(callback, 0)
    return results


def _list_monitors() -> list[dict[str, Any]]:
    _dpi_mode()
    _configure_win32()
    user32 = ctypes.windll.user32
    results: list[dict[str, Any]] = []
    monitor_enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )

    @monitor_enum_proc
    def callback(hmonitor: wintypes.HMONITOR, _hdc: wintypes.HDC, _rect: ctypes.POINTER(RECT), _data: wintypes.LPARAM) -> bool:
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            return True
        results.append({
            "index": len(results),
            "monitor_handle": _hwnd_text(_handle_value(hmonitor)),
            "device": str(info.szDevice),
            "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
            "rect": _rect_dict(info.rcMonitor),
            "work_rect": _rect_dict(info.rcWork),
        })
        return True

    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        raise _fail("MONITOR_ENUM_FAILED", "Windows monitor enumeration failed")
    return results


def _validate_capture_rect(rect: RECT) -> tuple[int, int, int, int]:
    x = int(rect.left)
    y = int(rect.top)
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        raise _fail("CAPTURE_RECT_INVALID", "Capture rectangle has no visible area", x=x, y=y, width=width, height=height)
    pixels = width * height
    if pixels > MAX_CAPTURE_PIXELS:
        raise _fail("CAPTURE_TOO_LARGE", "Capture rectangle exceeds the bounded pixel limit", pixels=pixels, max_pixels=MAX_CAPTURE_PIXELS)
    return x, y, width, height


def _capture_bgra(rect: RECT) -> bytes:
    _require_windows()
    _configure_win32()
    x, y, width, height = _validate_capture_rect(rect)
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise _fail("CAPTURE_SCREEN_DC_FAILED", "Could not access the interactive desktop device context")
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    if not memory_dc:
        user32.ReleaseDC(None, screen_dc)
        raise _fail("CAPTURE_MEMORY_DC_FAILED", "Could not create a compatible capture device context")

    bitmap_info = BITMAPINFO()
    bitmap_info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bitmap_info.bmiHeader.biWidth = width
    bitmap_info.bmiHeader.biHeight = -height
    bitmap_info.bmiHeader.biPlanes = 1
    bitmap_info.bmiHeader.biBitCount = 32
    bitmap_info.bmiHeader.biCompression = BI_RGB
    bitmap_info.bmiHeader.biSizeImage = width * height * 4
    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(
        screen_dc,
        ctypes.byref(bitmap_info),
        DIB_RGB_COLORS,
        ctypes.byref(bits),
        None,
        0,
    )
    if not bitmap or not bits.value:
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)
        raise _fail("CAPTURE_BITMAP_FAILED", "Could not allocate the capture bitmap")
    old_object = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, x, y, SRCCOPY | CAPTUREBLT):
            raise _fail("CAPTURE_BITBLT_FAILED", "Windows visible-pixel capture failed", x=x, y=y, width=width, height=height)
        return ctypes.string_at(bits.value, width * height * 4)
    finally:
        if old_object:
            gdi32.SelectObject(memory_dc, old_object)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)


def _capture_window_surface_bgra(hwnd: int, rect: RECT, *, client_area: bool) -> bytes:
    _require_windows()
    _configure_win32()
    _x, _y, width, height = _validate_capture_rect(rect)
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise _fail("CAPTURE_SCREEN_DC_FAILED", "Could not access a compatible desktop device context")
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    if not memory_dc:
        user32.ReleaseDC(None, screen_dc)
        raise _fail("CAPTURE_MEMORY_DC_FAILED", "Could not create a compatible window-surface device context")

    bitmap_info = BITMAPINFO()
    bitmap_info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bitmap_info.bmiHeader.biWidth = width
    bitmap_info.bmiHeader.biHeight = -height
    bitmap_info.bmiHeader.biPlanes = 1
    bitmap_info.bmiHeader.biBitCount = 32
    bitmap_info.bmiHeader.biCompression = BI_RGB
    bitmap_info.bmiHeader.biSizeImage = width * height * 4
    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(
        screen_dc,
        ctypes.byref(bitmap_info),
        DIB_RGB_COLORS,
        ctypes.byref(bits),
        None,
        0,
    )
    if not bitmap or not bits.value:
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)
        raise _fail("CAPTURE_BITMAP_FAILED", "Could not allocate the window-surface capture bitmap")
    old_object = gdi32.SelectObject(memory_dc, bitmap)
    try:
        flags = PW_RENDERFULLCONTENT | (PW_CLIENTONLY if client_area else 0)
        if not user32.PrintWindow(wintypes.HWND(hwnd), memory_dc, flags):
            raise _fail(
                "WINDOW_SURFACE_CAPTURE_FAILED",
                "Windows could not render the requested window surface; use capture_mode=visible_pixels when the window is unobscured",
                window_handle=_hwnd_text(hwnd),
            )
        return ctypes.string_at(bits.value, width * height * 4)
    finally:
        if old_object:
            gdi32.SelectObject(memory_dc, old_object)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = binascii.crc32(kind)
    crc = binascii.crc32(payload, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _encode_png_from_bgra(raw: bytes, width: int, height: int) -> bytes:
    expected = width * height * 4
    if len(raw) != expected:
        raise _fail("CAPTURE_PIXEL_BUFFER_INVALID", "Captured pixel buffer length is inconsistent", expected_bytes=expected, actual_bytes=len(raw))
    compressor = zlib.compressobj(level=6)
    compressed_parts: list[bytes] = []
    row_bytes = width * 4
    for row_index in range(height):
        row = raw[row_index * row_bytes:(row_index + 1) * row_bytes]
        rgb = bytearray(width * 3)
        rgb[0::3] = row[2::4]
        rgb[1::3] = row[1::4]
        rgb[2::3] = row[0::4]
        part = compressor.compress(b"\x00" + bytes(rgb))
        if part:
            compressed_parts.append(part)
    tail = compressor.flush()
    if tail:
        compressed_parts.append(tail)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", b"".join(compressed_parts))
        + _png_chunk(b"IEND", b"")
    )


def _pixel_stats(raw: bytes, width: int, height: int) -> dict[str, Any]:
    pixels = width * height
    if pixels <= 0:
        return {"sampled_pixels": 0, "black_fraction": 1.0, "near_blank": True}
    stride = max(1, pixels // 100_000)
    sampled = 0
    black = 0
    minimum = 255
    maximum = 0
    total = 0
    step = stride * 4
    for offset in range(0, len(raw), step):
        if offset + 2 >= len(raw):
            break
        b = raw[offset]
        g = raw[offset + 1]
        r = raw[offset + 2]
        value_max = max(r, g, b)
        value_min = min(r, g, b)
        if value_max <= 7:
            black += 1
        minimum = min(minimum, value_min)
        maximum = max(maximum, value_max)
        total += (int(r) + int(g) + int(b)) // 3
        sampled += 1
    black_fraction = (black / sampled) if sampled else 1.0
    mean_luma = (total / sampled) if sampled else 0.0
    near_blank = bool(sampled and black_fraction >= 0.995 and maximum <= 16)
    return {
        "sampled_pixels": sampled,
        "black_fraction": round(black_fraction, 6),
        "mean_luma": round(mean_luma, 3),
        "channel_min": minimum if sampled else 0,
        "channel_max": maximum if sampled else 0,
        "near_blank": near_blank,
        "warning": "near-blank visible pixels; a protected/occluded surface or genuinely dark frame is possible" if near_blank else None,
    }


def _validate_path_segment(part: str) -> None:
    if not part or part in {".", ".."}:
        raise _fail("CAPTURE_PATH_INVALID", "Output path contains an ambiguous segment")
    if ":" in part or any(ord(char) < 32 for char in part):
        raise _fail("CAPTURE_PATH_BLOCKED", "Windows ADS/control-character path segments are not allowed", segment=part)
    if part.endswith((" ", ".")):
        raise _fail("CAPTURE_PATH_BLOCKED", "Windows-trimmed path segments are not allowed", segment=part)
    folded = part.casefold()
    if folded.split(".", 1)[0].rstrip(" .") in WINDOWS_RESERVED_BASES:
        raise _fail("CAPTURE_PATH_BLOCKED", "Windows reserved device names are not allowed", segment=part)
    if folded in VCS_BLOCKED_SEGMENTS or folded in WORKSPACE_BLOCKED_SEGMENTS:
        raise _fail("CAPTURE_PATH_BLOCKED", "VCS/dependency/generated destinations are not allowed", segment=part)
    if folded == ".folderbridge.json":
        raise _fail("CAPTURE_PATH_BLOCKED", "FolderBridge control files are not allowed", segment=part)


def _is_linkish(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & reparse)


def _resolve_output(context: Any, raw: Any) -> tuple[Path, Path]:
    if not isinstance(context, dict) or not context.get("workspace_root"):
        raise _fail("CAPTURE_WORKSPACE_REQUIRED", "A selected FolderBridge workspace is required for capture output")
    if bool(context.get("workspace_read_only", False)):
        raise _fail("READ_ONLY", "FolderBridge is running read-only")
    if not isinstance(raw, str) or not raw or len(raw) > 1024 or "\\" in raw or "\x00" in raw:
        raise _fail("CAPTURE_PATH_INVALID", "output_path must be a bounded POSIX-style workspace-relative path")
    relative = Path(raw)
    if relative.is_absolute() or relative.drive or raw.endswith("/"):
        raise _fail("CAPTURE_PATH_INVALID", "output_path must name one workspace-relative PNG file")
    if relative.suffix.casefold() != ".png":
        raise _fail("CAPTURE_PATH_INVALID", "output_path must end with .png")
    for part in relative.parts:
        _validate_path_segment(part)
    root = Path(str(context["workspace_root"])).resolve()
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and _is_linkish(current):
            raise _fail("CAPTURE_PATH_BLOCKED", "Output parent contains a link/reparse point")
    target = root.joinpath(*relative.parts)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise _fail("CAPTURE_PATH_INVALID", "Output path escapes the selected workspace") from exc
    if target.exists() and _is_linkish(target):
        raise _fail("CAPTURE_PATH_BLOCKED", "Output path is a link/reparse point")
    return Path(*relative.parts), target


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_expected_sha(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise _fail("CAPTURE_SHA_INVALID", "expected_target_sha256 must be 64 hexadecimal characters")
    return value.lower()


def _publish_png(target: Path, png: bytes, *, overwrite: bool, expected_target_sha256: str | None) -> str:
    if overwrite and expected_target_sha256 is None:
        raise _fail("CAPTURE_TARGET_SHA_REQUIRED", "overwrite=true requires expected_target_sha256")
    if not overwrite and expected_target_sha256 is not None:
        raise _fail("CAPTURE_TARGET_SHA_INVALID", "expected_target_sha256 is valid only when overwrite=true")
    if target.exists():
        if not overwrite:
            raise _fail("CAPTURE_EXISTS", "Screenshot destination already exists; overwrite is false")
        if not target.is_file() or _is_linkish(target):
            raise _fail("CAPTURE_PATH_BLOCKED", "Existing screenshot destination is not a safe regular file")
        actual = _sha256_file(target)
        if actual != expected_target_sha256:
            raise _fail(
                "CAPTURE_TARGET_STALE",
                "Existing screenshot changed; refusing overwrite",
                expected_target_sha256=expected_target_sha256,
                actual_target_sha256=actual,
            )
    elif overwrite:
        raise _fail("CAPTURE_TARGET_STALE", "overwrite=true requires the expected destination to exist")

    target.parent.mkdir(parents=True, exist_ok=True)
    for ancestor in (target.parent, *target.parent.parents):
        if ancestor.exists() and _is_linkish(ancestor):
            raise _fail("CAPTURE_PATH_BLOCKED", "Output parent became a link/reparse point")
        if ancestor.parent == ancestor:
            break

    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.capture-", dir=str(target.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(png)
            stream.flush()
            os.fsync(stream.fileno())
        digest = hashlib.sha256(png).hexdigest()
        if overwrite:
            os.replace(temp_path, target)
        else:
            try:
                os.link(temp_path, target)
            except FileExistsError as exc:
                raise _fail("CAPTURE_EXISTS", "Screenshot destination appeared before publish; refusing to clobber") from exc
            except OSError as exc:
                raise _fail("CAPTURE_PUBLISH_FAILED", "Atomic no-clobber publish requires same-filesystem hard-link support", exception_type=type(exc).__name__) from exc
            else:
                temp_path.unlink(missing_ok=True)
        return digest
    finally:
        temp_path.unlink(missing_ok=True)


def _delay(params: dict[str, Any]) -> int:
    value = int(params.get("delay_ms", 0))
    if value < 0 or value > 30000:
        raise _fail("CAPTURE_DELAY_INVALID", "delay_ms must be between 0 and 30000")
    if value:
        time.sleep(value / 1000.0)
    return value


def _capture_mode(params: dict[str, Any]) -> str:
    value = str(params.get("capture_mode") or "window_surface")
    if value not in {"window_surface", "visible_pixels"}:
        raise _fail("CAPTURE_MODE_INVALID", "capture_mode must be window_surface or visible_pixels")
    return value


def _persist_capture(
    rect: RECT,
    raw: bytes,
    params: dict[str, Any],
    context: Any,
    source: dict[str, Any],
    *,
    capture_method: str,
    delay_ms: int,
) -> dict[str, Any]:
    x, y, width, height = _validate_capture_rect(rect)
    png = _encode_png_from_bgra(raw, width, height)
    stats = _pixel_stats(raw, width, height)
    relative, target = _resolve_output(context, params.get("output_path"))
    overwrite = bool(params.get("overwrite", False))
    expected_target = _normalize_expected_sha(params.get("expected_target_sha256"))
    digest = _publish_png(target, png, overwrite=overwrite, expected_target_sha256=expected_target)
    return {
        "ok": True,
        "path": relative.as_posix(),
        "size": len(png),
        "sha256": digest,
        "width": width,
        "height": height,
        "capture_rect": {"x": x, "y": y, "width": width, "height": height},
        "capture_method": capture_method,
        "delay_ms": delay_ms,
        "source": source,
        "visual_stats": stats,
        "drm_bypass_attempted": False,
        "continuous_recording": False,
        "workspace_artifacts": [{"path": relative.as_posix(), "label": "screenshot", "kind": "image"}],
    }


def _restore_minimized_window_if_needed(hwnd: int, params: dict[str, Any]) -> bool:
    _configure_win32()
    user32 = ctypes.windll.user32
    if not user32.IsIconic(wintypes.HWND(hwnd)):
        return False
    if not bool(params.get("restore_minimized", True)):
        raise _fail(
            "WINDOW_MINIMIZED",
            "The requested window is minimized and restore_minimized is false",
            window_handle=_hwnd_text(hwnd),
        )
    user32.ShowWindowAsync(wintypes.HWND(hwnd), SW_SHOWNOACTIVATE)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and user32.IsIconic(wintypes.HWND(hwnd)):
        time.sleep(0.05)
    if user32.IsIconic(wintypes.HWND(hwnd)):
        raise _fail(
            "WINDOW_RESTORE_FAILED",
            "Windows did not restore the minimized window in time",
            window_handle=_hwnd_text(hwnd),
        )
    return True


def _reminimize_window(hwnd: int) -> None:
    try:
        _configure_win32()
        user32 = ctypes.windll.user32
        if user32.IsWindow(wintypes.HWND(hwnd)):
            user32.ShowWindowAsync(wintypes.HWND(hwnd), SW_SHOWMINNOACTIVE)
    except Exception:
        pass


def _click_window(params: dict[str, Any]) -> dict[str, Any]:
    _dpi_mode()
    _configure_win32()
    user32 = ctypes.windll.user32
    hwnd = _parse_handle(params.get("window_handle"))
    if not user32.IsWindow(wintypes.HWND(hwnd)):
        raise _fail("WINDOW_NOT_FOUND", "The requested window no longer exists", window_handle=_hwnd_text(hwnd))
    restored_from_minimized = _restore_minimized_window_if_needed(hwnd, params)
    previous_foreground = _handle_value(user32.GetForegroundWindow())
    previous_cursor = wintypes.POINT()
    have_cursor = bool(user32.GetCursorPos(ctypes.byref(previous_cursor)))
    try:
        if not user32.IsWindowVisible(wintypes.HWND(hwnd)) or _is_cloaked(hwnd):
            raise _fail("WINDOW_NOT_VISIBLE", "Mouse click requires a visible, non-cloaked target window", window_handle=_hwnd_text(hwnd))
        rect = _raw_window_rect(hwnd)
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        x = int(params.get("x"))
        y = int(params.get("y"))
        if x < 0 or y < 0 or x >= width or y >= height:
            raise _fail(
                "CLICK_POINT_OUTSIDE_WINDOW",
                "Click coordinates must stay inside the exact target window",
                x=x,
                y=y,
                window_width=width,
                window_height=height,
            )
        user32.BringWindowToTop(wintypes.HWND(hwnd))
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if _handle_value(user32.GetForegroundWindow()) == hwnd:
                break
            time.sleep(0.03)
        if _handle_value(user32.GetForegroundWindow()) != hwnd:
            raise _fail("WINDOW_FOCUS_FAILED", "Windows did not foreground the exact target window; refusing to click")
        screen_x = int(rect.left) + x
        screen_y = int(rect.top) + y
        if not user32.SetCursorPos(screen_x, screen_y):
            raise _fail("CURSOR_MOVE_FAILED", "Windows could not position the pointer inside the target window")
        time.sleep(0.04)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)
        after_click_ms = int(params.get("after_click_ms", 300))
        if after_click_ms < 0 or after_click_ms > 5000:
            raise _fail("CLICK_DELAY_INVALID", "after_click_ms must be between 0 and 5000")
        if after_click_ms:
            time.sleep(after_click_ms / 1000.0)
        record = _window_record(hwnd)
        return {
            "ok": True,
            "window_handle": record["window_handle"],
            "title": record["title"],
            "pid": record["pid"],
            "process_name": record["process_name"],
            "coordinate_space": "window",
            "x": x,
            "y": y,
            "restored_from_minimized": restored_from_minimized,
            "after_click_ms": after_click_ms,
            "button": "left",
            "keyboard_input": False,
            "drag_input": False,
        }
    finally:
        if have_cursor:
            try:
                user32.SetCursorPos(int(previous_cursor.x), int(previous_cursor.y))
            except Exception:
                pass
        if previous_foreground and previous_foreground != hwnd and user32.IsWindow(wintypes.HWND(previous_foreground)):
            try:
                user32.SetForegroundWindow(wintypes.HWND(previous_foreground))
            except Exception:
                pass
        if restored_from_minimized:
            _reminimize_window(hwnd)


def _demo_folderbridge_executable() -> Path:
    executable = Path(sys.executable).resolve()
    if executable.name.casefold() != "folderbridge.exe":
        raise _fail(
            "DEMO_LAUNCH_UNAVAILABLE",
            "The screenshot-only FolderBridge demo instance is available only from the frozen FolderBridge.exe runtime",
            executable_name=executable.name,
        )
    return executable


def _launch_demo_folderbridge(params: dict[str, Any], context: Any) -> dict[str, Any]:
    _require_windows()
    executable = _demo_folderbridge_executable()
    base = Path(tempfile.mkdtemp(prefix="folderbridge-capture-demo-"))
    local_app_data = base / "LocalAppData"
    roaming_app_data = base / "RoamingAppData"
    local_app_data.mkdir(parents=True, exist_ok=True)
    roaming_app_data.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.pop("FOLDERBRIDGE_CONFIG_ROOT", None)
    env.pop("FOLDERBRIDGE_CONFIG_ROOT_MARKER", None)
    env["LOCALAPPDATA"] = str(local_app_data)
    env["APPDATA"] = str(roaming_app_data)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    process: subprocess.Popen[Any] | None = None
    try:
        process = subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        return_code = process.wait()
        return {
            "ok": return_code == 0,
            "mode": "isolated-demo-instance",
            "pid": int(process.pid),
            "return_code": int(return_code),
            "isolated_localappdata": True,
            "isolated_appdata": True,
            "shared_primary_profile": False,
        }
    finally:
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
        shutil.rmtree(base, ignore_errors=True)


def _status() -> dict[str, Any]:
    if os.name != "nt" or not hasattr(ctypes, "windll"):
        return {
            "ok": True,
            "ready": False,
            "platform": os.name,
            "reason": "Windows desktop APIs are unavailable",
            "version": VERSION,
        }
    mode = _dpi_mode()
    _configure_win32()
    user32 = ctypes.windll.user32
    return {
        "ok": True,
        "ready": True,
        "platform": "windows",
        "version": VERSION,
        "dpi_awareness": mode,
        "virtual_screen": {
            "x": int(user32.GetSystemMetrics(76)),
            "y": int(user32.GetSystemMetrics(77)),
            "width": int(user32.GetSystemMetrics(78)),
            "height": int(user32.GetSystemMetrics(79)),
        },
        "capabilities": {
            "exact_window_surface": True,
            "visible_window_pixels": True,
            "active_window": True,
            "monitor": True,
            "region": True,
            "delayed_one_shot": True,
            "temporary_restore_minimized": True,
            "isolated_folderbridge_demo_instance": True,
            "visible_video_playback_pixels": True,
            "continuous_recording": False,
            "input_control": True,
            "bounded_left_click": True,
            "keyboard_input": False,
            "drag_input": False,
            "absolute_desktop_click": False,
            "drm_hdcp_bypass": False,
        },
    }


def _capture_window(params: dict[str, Any], context: Any, *, active: bool) -> dict[str, Any]:
    _dpi_mode()
    _configure_win32()
    user32 = ctypes.windll.user32
    if active:
        hwnd = _handle_value(user32.GetForegroundWindow())
        if not hwnd:
            raise _fail("WINDOW_NOT_FOUND", "No active foreground window is available")
    else:
        hwnd = _parse_handle(params.get("window_handle"))
    if not user32.IsWindow(wintypes.HWND(hwnd)):
        raise _fail("WINDOW_NOT_FOUND", "The requested window no longer exists", window_handle=_hwnd_text(hwnd))
    capture_mode = _capture_mode(params)
    restored_from_minimized = _restore_minimized_window_if_needed(hwnd, params)
    try:
        delay_ms = _delay(params)
        if not user32.IsWindow(wintypes.HWND(hwnd)):
            raise _fail("WINDOW_NOT_FOUND", "The requested window disappeared during capture delay", window_handle=_hwnd_text(hwnd))
        if not user32.IsWindowVisible(wintypes.HWND(hwnd)) or _is_cloaked(hwnd):
            raise _fail("WINDOW_NOT_VISIBLE", "Window capture requires a currently visible, non-cloaked window", window_handle=_hwnd_text(hwnd))
        client_area = bool(params.get("client_area", False))
        if capture_mode == "window_surface":
            rect = _client_window_rect(hwnd) if client_area else _raw_window_rect(hwnd)
            raw = _capture_window_surface_bgra(hwnd, rect, client_area=client_area)
            capture_method = "windows-window-surface-printwindow"
        else:
            rect = _client_window_rect(hwnd) if client_area else _extended_window_rect(hwnd)
            raw = _capture_bgra(rect)
            capture_method = "windows-visible-pixels-gdi"
        record = _window_record(hwnd)
        source = {
            "kind": "active-window" if active else "window",
            "window_handle": record["window_handle"],
            "title": record["title"],
            "pid": record["pid"],
            "process_name": record["process_name"],
            "client_area": client_area,
            "capture_mode": capture_mode,
            "restored_from_minimized": restored_from_minimized,
        }
        return _persist_capture(
            rect,
            raw,
            params,
            context,
            source,
            capture_method=capture_method,
            delay_ms=delay_ms,
        )
    finally:
        if restored_from_minimized:
            _reminimize_window(hwnd)


def _capture_monitor(params: dict[str, Any], context: Any) -> dict[str, Any]:
    delay_ms = _delay(params)
    monitors = _list_monitors()
    index = int(params.get("monitor_index"))
    if index < 0 or index >= len(monitors):
        raise _fail("MONITOR_NOT_FOUND", "monitor_index is outside the current monitor list", monitor_index=index, monitor_count=len(monitors))
    monitor = monitors[index]
    rect_data = monitor["rect"]
    rect = RECT(rect_data["x"], rect_data["y"], rect_data["right"], rect_data["bottom"])
    raw = _capture_bgra(rect)
    source = {
        "kind": "monitor",
        "monitor_index": index,
        "monitor_handle": monitor["monitor_handle"],
        "device": monitor["device"],
        "primary": monitor["primary"],
    }
    return _persist_capture(
        rect,
        raw,
        params,
        context,
        source,
        capture_method="windows-visible-pixels-gdi",
        delay_ms=delay_ms,
    )


def _capture_region(params: dict[str, Any], context: Any) -> dict[str, Any]:
    delay_ms = _delay(params)
    x = int(params.get("x"))
    y = int(params.get("y"))
    width = int(params.get("width"))
    height = int(params.get("height"))
    rect = RECT(x, y, x + width, y + height)
    raw = _capture_bgra(rect)
    source = {"kind": "region"}
    return _persist_capture(
        rect,
        raw,
        params,
        context,
        source,
        capture_method="windows-visible-pixels-gdi",
        delay_ms=delay_ms,
    )


def handle(action: str, params: dict[str, Any], context: Any) -> dict[str, Any]:
    if action == "status":
        return _status()
    if action == "list-windows":
        _require_windows()
        windows = _list_windows(params)
        return {"ok": True, "count": len(windows), "windows": windows}
    if action == "list-monitors":
        _require_windows()
        monitors = _list_monitors()
        return {"ok": True, "count": len(monitors), "monitors": monitors}
    if action == "capture-window":
        return _capture_window(params, context, active=False)
    if action == "capture-active-window":
        return _capture_window(params, context, active=True)
    if action == "click-window":
        return _click_window(params)
    if action == "launch-demo-folderbridge":
        return _launch_demo_folderbridge(params, context)
    if action == "capture-monitor":
        _require_windows()
        return _capture_monitor(params, context)
    if action == "capture-region":
        _require_windows()
        return _capture_region(params, context)
    raise _fail("CAPTURE_ACTION_UNSUPPORTED", f"Unsupported action: {action}")
