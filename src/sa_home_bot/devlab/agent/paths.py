from __future__ import annotations

import fnmatch
from collections.abc import Sequence


def matches(relative: str, patterns: Sequence[str]) -> bool:
    return any(
        fnmatch.fnmatch(relative, pattern)
        or (pattern.endswith("/") and relative.startswith(pattern))
        for pattern in patterns
    )
