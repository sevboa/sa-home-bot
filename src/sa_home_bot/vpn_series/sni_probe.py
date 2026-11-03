from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import time
from enum import StrEnum

from pydantic import BaseModel


class SniVerdict(StrEnum):
    ok = "ok"
    dns_fail = "dns_fail"
    tcp_fail = "tcp_fail"
    tls_fail = "tls_fail"
    no_tls13 = "no_tls13"
    no_h2 = "no_h2"


class SniResult(BaseModel):
    domain: str
    verdict: SniVerdict
    ms: int | None
    error: str | None


async def probe_sni(domain: str, port: int = 443, timeout: float = 5.0) -> SniResult:
    start = time.monotonic()
    loop = asyncio.get_running_loop()

    try:
        await loop.getaddrinfo(domain, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return SniResult(
            domain=domain,
            verdict=SniVerdict.dns_fail,
            ms=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_alpn_protocols(["h2", "http/1.1"])

    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(domain, port, ssl=ctx, server_hostname=domain),
            timeout,
        )
    except TimeoutError:
        return SniResult(
            domain=domain,
            verdict=SniVerdict.tcp_fail,
            ms=None,
            error="таймаут TCP",
        )
    except ssl.SSLError as exc:
        return SniResult(
            domain=domain,
            verdict=SniVerdict.tls_fail,
            ms=None,
            error=f"{type(exc).__name__}: {exc}",
        )
    except OSError as exc:
        return SniResult(
            domain=domain,
            verdict=SniVerdict.tcp_fail,
            ms=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    try:
        ssl_object = writer.get_extra_info("ssl_object")
        if ssl_object is None:
            return SniResult(
                domain=domain,
                verdict=SniVerdict.tls_fail,
                ms=None,
                error="нет ssl_object",
            )

        version = ssl_object.version()
        if version != "TLSv1.3":
            return SniResult(
                domain=domain,
                verdict=SniVerdict.no_tls13,
                ms=int((time.monotonic() - start) * 1000),
                error=f"версия TLS: {version}",
            )

        alpn = ssl_object.selected_alpn_protocol()
        if alpn != "h2":
            return SniResult(
                domain=domain,
                verdict=SniVerdict.no_h2,
                ms=int((time.monotonic() - start) * 1000),
                error=f"ALPN: {alpn}",
            )

        return SniResult(
            domain=domain,
            verdict=SniVerdict.ok,
            ms=int((time.monotonic() - start) * 1000),
            error=None,
        )
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


async def probe_many(
    domains: list[str], *, concurrency: int = 4, timeout: float = 5.0
) -> list[SniResult]:
    semaphore = asyncio.Semaphore(concurrency)

    async def _probe(domain: str) -> SniResult:
        async with semaphore:
            return await probe_sni(domain, timeout=timeout)

    return await asyncio.gather(*[_probe(d) for d in domains])
