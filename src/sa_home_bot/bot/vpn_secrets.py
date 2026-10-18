from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class PendingVpnSecret:
    config_text: str
    device_label: str
    qr_png_b64: str | None
    reveal: Literal["file", "qr"]
    transport: Literal["awg", "reality"] = "awg"
    share_url: str | None = None
    deep_link: str | None = None
    location: str = ""


class PendingVpnSecrets:
    def __init__(self) -> None:
        self._items: dict[str, tuple[PendingVpnSecret, float]] = {}

    def put(
        self,
        config_text: str,
        device_label: str,
        qr_png_b64: str | None,
        reveal: str,
        ttl_s: float,
        *,
        transport: str = "awg",
        share_url: str | None = None,
        deep_link: str | None = None,
        location: str = "",
    ) -> str:
        token = secrets.token_urlsafe(6)
        self._items[token] = (
            PendingVpnSecret(
                config_text,
                device_label,
                qr_png_b64,
                reveal,
                transport=transport,
                share_url=share_url,
                deep_link=deep_link,
                location=location,
            ),
            time.monotonic() + ttl_s,
        )
        return token

    def pop(self, token: str) -> PendingVpnSecret | None:
        entry = self._items.pop(token, None)
        if entry is None:
            return None
        secret, expires_at = entry
        if time.monotonic() > expires_at:
            return None
        return secret

    def discard(self, token: str) -> None:
        self._items.pop(token, None)
