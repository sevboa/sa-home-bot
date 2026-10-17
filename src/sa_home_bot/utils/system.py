from __future__ import annotations

import sys


def system_uptime_seconds() -> float | None:
    if sys.platform == "win32":
        import ctypes

        return ctypes.windll.kernel32.GetTickCount64() / 1000.0
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
