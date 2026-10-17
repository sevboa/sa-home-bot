from __future__ import annotations

import base64
import hashlib
from typing import Any

from sa_home_bot.proto.messages import Address

CHUNK_BYTES = 700 * 1024


async def download_chunks(
    link: Any,
    action: str,
    dst: Address,
    *,
    base_args: dict[str, Any] | None = None,
    expected_sha256: str | None = None,
    integrity_error: str = "скачанный файл не прошёл проверку целостности (sha256)",
) -> bytes:
    parts: list[bytes] = []
    offset = 0
    while True:
        chunk = await link.command(
            action, {**(base_args or {}), "offset": offset, "length": CHUNK_BYTES}, dst=dst
        )
        data = base64.b64decode(chunk["data_b64"])
        if not data:
            break
        parts.append(data)
        offset += len(data)
        if chunk.get("eof"):
            break
    payload = b"".join(parts)
    if expected_sha256 and hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError(integrity_error)
    return payload
