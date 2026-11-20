from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import os
import ssl
import struct
import time
from typing import Any, Literal

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
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
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx, server_hostname=sni),
            timeout,
        )
    except (OSError, ssl.SSLError, TimeoutError) as exc:
        return f"{type(exc).__name__}: {exc}", None
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
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


_HTTP_PREFIXES = (b"HEAD", b"POST", b"GET ", b"OPTI", b"\xee" * 4, b"\xdd" * 4, b"\x16\x03\x01\x02")


def parse_faketls_secret(secret: str) -> tuple[bytes, bytes]:
    if secret[:2].lower() == "ee":
        raw = bytes.fromhex(secret)
    else:
        raw = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    if len(raw) < 18 or raw[0] != 0xEE:
        raise ValueError("секрет mtg должен начинаться с байта 0xee и содержать домен")
    return raw[1:17], raw[17:]


def build_client_hello(key: bytes, domain: bytes, now: int, rand: bytes) -> bytes:
    sni = b"\x00" + struct.pack(">H", len(domain)) + domain
    sni = struct.pack(">H", len(sni)) + sni
    exts = b"\x00\x00" + struct.pack(">H", len(sni)) + sni
    exts += b"\x00\x0b\x00\x02\x01\x00"
    groups = b"\x00\x1d\x00\x17\x00\x18"
    exts += b"\x00\x0a" + struct.pack(">HH", len(groups) + 2, len(groups)) + groups
    algs = b"\x04\x03\x08\x04\x04\x01\x05\x03\x08\x05\x05\x01\x08\x06\x06\x01"
    exts += b"\x00\x0d" + struct.pack(">HH", len(algs) + 2, len(algs)) + algs
    alpn = b"\x02h2\x08http/1.1"
    exts += b"\x00\x10" + struct.pack(">HH", len(alpn) + 2, len(alpn)) + alpn
    ks = b"\x00\x1d\x00\x20" + rand[32:64]
    exts += b"\x00\x33" + struct.pack(">HH", len(ks) + 2, len(ks)) + ks
    exts += b"\x00\x2b\x00\x03\x02\x03\x04"
    exts += b"\x00\x2d\x00\x02\x01\x01"
    ciphers = b"\x13\x01\x13\x02\x13\x03\xc0\x2b\xc0\x2f\xc0\x2c\xc0\x30"
    body = b"\x03\x03" + b"\x00" * 32 + b"\x20" + rand[:32]
    body += struct.pack(">H", len(ciphers)) + ciphers + b"\x01\x00"
    pad_len = 517 - 4 - 5 - len(body) - 2 - len(exts) - 4
    if pad_len > 0:
        exts += b"\x00\x15" + struct.pack(">H", pad_len) + b"\x00" * pad_len
    body += struct.pack(">H", len(exts)) + exts
    hs = b"\x01" + struct.pack(">I", len(body))[1:] + body
    hello = bytearray(b"\x16\x03\x01" + struct.pack(">H", len(hs)) + hs)
    digest = bytearray(hmac.new(key, bytes(hello), hashlib.sha256).digest())
    ts = struct.pack("<I", now)
    for i in range(4):
        digest[28 + i] ^= ts[i]
    hello[11:43] = digest
    return bytes(hello)


def obfuscated2_init(key: bytes, dc: int, rand64: bytes) -> tuple[bytes, Any, Any]:
    init = bytearray(rand64)
    if init[0] == 0xEF or bytes(init[:4]) in _HTTP_PREFIXES or bytes(init[4:8]) == b"\x00" * 4:
        raise ValueError("случайные 64 байта выглядят как HTTP-запрос")
    init[56:60] = b"\xdd\xdd\xdd\xdd"
    init[60:62] = struct.pack("<h", dc)
    enc_key = hashlib.sha256(bytes(init[8:40]) + key).digest()
    enc_iv = bytes(init[40:56])
    rev = bytes(init[8:56])[::-1]
    dec_key = hashlib.sha256(rev[:32] + key).digest()
    dec_iv = rev[32:48]
    enc = Cipher(algorithms.AES(enc_key), modes.CTR(enc_iv)).encryptor()
    dec = Cipher(algorithms.AES(dec_key), modes.CTR(dec_iv)).decryptor()
    encrypted_init = enc.update(bytes(init))
    first = bytes(init[:56]) + encrypted_init[56:64]
    return first, enc, dec


async def _read_exact(reader: asyncio.StreamReader, n: int, timeout: float) -> bytes:
    buf = b""
    deadline = time.monotonic() + timeout
    while len(buf) < n:
        left = deadline - time.monotonic()
        buf += await asyncio.wait_for(reader.readexactly(n - len(buf)), left)
    return buf


async def _read_record(reader: asyncio.StreamReader, timeout: float) -> tuple[int, bytes]:
    hdr = await _read_exact(reader, 5, timeout)
    length = struct.unpack(">H", hdr[3:5])[0]
    data = await _read_exact(reader, length, timeout)
    return hdr[0], data


class MtprotoProbeResult(BaseModel):
    verdict: Literal["ok", "tcp_blocked", "tls_failed", "mtproto_failed"]
    error: str | None
    tls_ms: int | None
    total_ms: int | None


async def mtproto_probe(
    host: str, port: int, secret: str, dc: int = 2, timeout: float = 10.0
) -> MtprotoProbeResult:
    start = time.perf_counter()
    tcp_error = await tcp_connect(host, port, timeout)
    if tcp_error is not None:
        return MtprotoProbeResult(
            verdict="tcp_blocked",
            error=tcp_error,
            tls_ms=None,
            total_ms=None,
        )
    try:
        key, domain = parse_faketls_secret(secret)
    except ValueError as exc:
        return MtprotoProbeResult(
            verdict="mtproto_failed",
            error=str(exc),
            tls_ms=None,
            total_ms=int((time.perf_counter() - start) * 1000),
        )
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError) as exc:
        return MtprotoProbeResult(
            verdict="tcp_blocked",
            error=f"{type(exc).__name__}: {exc}",
            tls_ms=None,
            total_ms=int((time.perf_counter() - start) * 1000),
        )
    hello = build_client_hello(key, domain, int(time.time()), os.urandom(64))
    handshake_start = time.perf_counter()
    try:
        writer.write(hello)
        await asyncio.wait_for(writer.drain(), timeout)
        t, _ = await _read_record(reader, timeout)
        if t != 0x16:
            raise EOFError(f"нет ServerHello: тип записи 0x{t:02x}")
        _, _ = await _read_record(reader, timeout)
        _, _ = await _read_record(reader, timeout)
    except (EOFError, OSError, TimeoutError) as exc:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return MtprotoProbeResult(
            verdict="tls_failed",
            error=f"{type(exc).__name__}: {exc}",
            tls_ms=int((time.perf_counter() - handshake_start) * 1000),
            total_ms=int((time.perf_counter() - start) * 1000),
        )
    tls_ms = int((time.perf_counter() - handshake_start) * 1000)
    while True:
        rand64 = os.urandom(64)
        try:
            first, enc, dec = obfuscated2_init(key, dc, rand64)
            break
        except ValueError:
            continue
    nonce = os.urandom(16)
    msg_id = int(time.time() * 2**32) & ~3
    body = struct.pack("<I", 0xBE7E8EF1) + nonce
    mt = struct.pack("<qqi", 0, msg_id, len(body)) + body
    pad = os.urandom(4)
    packet = struct.pack("<I", len(mt) + len(pad)) + mt + pad
    payload = first + enc.update(packet)
    try:
        writer.write(
            b"\x14\x03\x03\x00\x01\x01"
            + b"\x17\x03\x03"
            + struct.pack(">H", len(payload))
            + payload
        )
        await asyncio.wait_for(writer.drain(), timeout)
        stream = b""
        while len(stream) < 4:
            t, data = await _read_record(reader, timeout)
            if t == 0x17:
                stream += dec.update(data)
        n = struct.unpack("<I", stream[:4])[0]
        while len(stream) < 4 + n:
            t, data = await _read_record(reader, timeout)
            if t == 0x17:
                stream += dec.update(data)
        resp = stream[4:4 + n]
        resp_ctor = struct.unpack("<I", resp[20:24])[0]
        resp_nonce = resp[24:40]
    except (EOFError, OSError, TimeoutError) as exc:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return MtprotoProbeResult(
            verdict="mtproto_failed",
            error=f"{type(exc).__name__}: {exc}",
            tls_ms=tls_ms,
            total_ms=int((time.perf_counter() - start) * 1000),
        )
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    total_ms = int((time.perf_counter() - start) * 1000)
    if resp_ctor != 0x05162463 or resp_nonce != nonce:
        return MtprotoProbeResult(
            verdict="mtproto_failed",
            error="ответ не resPQ или nonce не совпал",
            tls_ms=tls_ms,
            total_ms=total_ms,
        )
    return MtprotoProbeResult(
        verdict="ok",
        error=None,
        tls_ms=tls_ms,
        total_ms=total_ms,
    )
