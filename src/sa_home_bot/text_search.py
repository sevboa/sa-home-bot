from __future__ import annotations

import re
from collections.abc import Iterable

WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
MIN_WORD_CHARS = 3


def fts_or_query(stems: Iterable[str]) -> str:
    return " OR ".join(f'"{stem}"' for stem in stems)
