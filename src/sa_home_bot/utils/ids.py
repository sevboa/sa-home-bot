from __future__ import annotations

import uuid


def short_token(length: int = 8) -> str:
    return uuid.uuid4().hex[:length]


def random_seed32() -> int:
    return int.from_bytes(uuid.uuid4().bytes[:4], "big")
