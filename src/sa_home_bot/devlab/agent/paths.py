from __future__ import annotations

import fnmatch
from collections.abc import Sequence


def tracked_but_closed(tracked: Sequence[str], secret_paths: Sequence[str]) -> list[str]:
    return [path for path in tracked if matches(path, secret_paths)]


def matches(relative: str, patterns: Sequence[str]) -> bool:
    return any(
        fnmatch.fnmatch(relative, pattern)
        or (pattern.endswith("/") and relative.startswith(pattern))
        for pattern in patterns
    )
