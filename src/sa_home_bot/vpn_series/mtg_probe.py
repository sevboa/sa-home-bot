from __future__ import annotations

import asyncio
import ssl
import time
from typing import Literal

from pydantic import BaseModel

from sa_home_bot.vpn_series.raw_probe import tcp_connect


class MtgProbeResult(BaseModel):
    tcp_ok: bool
    tls_ok: bool | None
    verdict: Literal["ok", "tcp_blocked", "tls_blocked"]
    error: str | None
    tls_ms: int | None


async def tls_handshake(
    host: str, port: int, sni: str, timeout: float
) -> tuple[str | None, int | None]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    start = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=sni),
            timeout,
        )
    except (OSError, ssl.SSLError, TimeoutError) as exc:
        return f"{type(exc).__name__}: {exc}", None
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    try:
        writer.close()
        await writer.wait_closed()
    except OSError:
        pass
    return None, elapsed_ms


async def mtg_probe(
    host: str, port: int, sni: str, timeout: float = 8.0
) -> MtgProbeResult:
    tcp_error = await tcp_connect(host, port, timeout)
    if tcp_error is not None:
        return MtgProbeResult(
            tcp_ok=False,
            tls_ok=None,
            verdict="tcp_blocked",
            error=tcp_error,
            tls_ms=None,
        )
    tls_error, tls_ms = await tls_handshake(host, port, sni, timeout)
    if tls_error is not None:
        return MtgProbeResult(
            tcp_ok=True,
            tls_ok=False,
            verdict="tls_blocked",
            error=tls_error,
            tls_ms=None,
        )
    return MtgProbeResult(
        tcp_ok=True,
        tls_ok=True,
        verdict="ok",
        error=None,
        tls_ms=tls_ms,
    )
