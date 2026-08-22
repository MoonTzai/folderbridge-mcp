"""Windows DPI helpers with platform-neutral fallbacks."""

from __future__ import annotations

import os
from typing import Protocol


BASE_DPI = 96
MIN_REASONABLE_DPI = 48
MAX_REASONABLE_DPI = 768


class TkWindow(Protocol):
    def update_idletasks(self) -> None: ...

    def winfo_fpixels(self, number: str) -> float: ...

    def winfo_id(self) -> int: ...


def scale_for_dpi(dpi: int | float | None) -> float:
    """Return a Windows UI scale factor, falling back to 100%."""

    if isinstance(dpi, bool) or not isinstance(dpi, (int, float)):
        return 1.0
    if not MIN_REASONABLE_DPI <= dpi <= MAX_REASONABLE_DPI:
        return 1.0
    return float(dpi) / BASE_DPI


def tk_scaling_for_dpi(dpi: int | float | None) -> float:
    """Convert Windows DPI to Tk's pixels-per-point scaling value."""

    return scale_for_dpi(dpi) * BASE_DPI / 72.0


def scaled_pixels(value: int | float, scale: float) -> int:
    """Scale a pixel measurement while keeping non-zero values visible."""

    result = round(float(value) * scale)
    if value > 0:
        return max(1, result)
    if value < 0:
        return min(-1, result)
    return 0


def fitted_window_size(
    dpi: int | float | None,
    screen_width: int,
    screen_height: int,
    *,
    base_width: int = 940,
    base_height: int = 820,
) -> tuple[int, int]:
    """Scale the initial window and keep it inside the visible screen."""

    scale = scale_for_dpi(dpi)
    available_width = max(1, round(screen_width * 0.92))
    available_height = max(1, round(screen_height * 0.90))
    return (
        min(scaled_pixels(base_width, scale), available_width),
        min(scaled_pixels(base_height, scale), available_height),
    )


def enable_windows_dpi_awareness() -> None:
    """Request the best DPI awareness supported by the Windows version."""

    if os.name != "nt":
        return

    import ctypes

    try:
        set_context = ctypes.windll.user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        if set_context(ctypes.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return
    except (AttributeError, OSError, TypeError, ValueError):
        pass

    try:
        # PROCESS_PER_MONITOR_DPI_AWARE, supported since Windows 8.1.
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except (AttributeError, OSError):
        pass

    try:
        # Last-resort system-DPI awareness for older Windows versions.
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def window_dpi(window: TkWindow) -> int:
    """Read the current window DPI, with Tk and 96-DPI fallbacks."""

    if os.name == "nt":
        try:
            import ctypes

            window.update_idletasks()
            get_dpi = ctypes.windll.user32.GetDpiForWindow
            get_dpi.argtypes = [ctypes.c_void_p]
            get_dpi.restype = ctypes.c_uint
            dpi = int(get_dpi(ctypes.c_void_p(window.winfo_id())))
            if MIN_REASONABLE_DPI <= dpi <= MAX_REASONABLE_DPI:
                return dpi
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    try:
        dpi = round(float(window.winfo_fpixels("1i")))
        if MIN_REASONABLE_DPI <= dpi <= MAX_REASONABLE_DPI:
            return dpi
    except (AttributeError, TypeError, ValueError):
        pass
    return BASE_DPI
