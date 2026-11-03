from __future__ import annotations

import asyncio
import contextlib
import re
import socket
import ssl
import time
from enum import StrEnum

from pydantic import BaseModel

SNI_PORT = 443
SNI_TIMEOUT_S = 5.0
SNI_CONCURRENCY = 4
SNI_MAX_DOMAINS = 30
DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


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


def parse_domains(raw: object) -> list[str]:
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, list):
        items = [str(item) for item in raw]
    else:
        raise ValueError("domains — строка через запятую или список")
    domains = [item.strip().lower() for item in items if item.strip()]
    if not domains:
        raise ValueError("domains пуст")
    if len(domains) > SNI_MAX_DOMAINS:
        raise ValueError(f"не больше {SNI_MAX_DOMAINS} доменов за раз")
    bad = [domain for domain in domains if not DOMAIN_RE.match(domain)]
    if bad:
        raise ValueError(f"некорректный домен: {bad[0]}")
    return domains


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    return ctx


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


async def probe_sni(domain: str, port: int = SNI_PORT, timeout: float = SNI_TIMEOUT_S) -> SniResult:
    def result(verdict: SniVerdict, error: str | None = None, ms: int | None = None) -> SniResult:
        return SniResult(domain=domain, verdict=verdict, ms=ms, error=error)

    started = time.monotonic()
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.getaddrinfo(domain, port, type=socket.SOCK_STREAM), timeout)
    except (socket.gaierror, TimeoutError) as exc:
        return result(SniVerdict.dns_fail, _error(exc))
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(domain, port, ssl=_tls_context(), server_hostname=domain),
            timeout,
        )
    except TimeoutError:
        return result(SniVerdict.tcp_fail, "таймаут")
    except ssl.SSLError as exc:
        return result(SniVerdict.tls_fail, _error(exc))
    except OSError as exc:
        return result(SniVerdict.tcp_fail, _error(exc))
    try:
        ms = int((time.monotonic() - started) * 1000)
        tls = writer.get_extra_info("ssl_object")
        if tls is None:
            return result(SniVerdict.tls_fail, "нет TLS-сессии")
        if tls.version() != "TLSv1.3":
            return result(SniVerdict.no_tls13, f"версия TLS: {tls.version()}", ms)
        if tls.selected_alpn_protocol() != "h2":
            return result(SniVerdict.no_h2, f"ALPN: {tls.selected_alpn_protocol()}", ms)
        return result(SniVerdict.ok, ms=ms)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def probe_many(
    domains: list[str], *, concurrency: int = SNI_CONCURRENCY, timeout: float = SNI_TIMEOUT_S
) -> list[SniResult]:
    gate = asyncio.Semaphore(concurrency)

    async def one(domain: str) -> SniResult:
        async with gate:
            return await probe_sni(domain, timeout=timeout)

    return list(await asyncio.gather(*(one(domain) for domain in domains)))
