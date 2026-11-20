from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import struct
import time

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from sa_home_bot.vpn_series import mtg_probe


def _hex_secret(key: bytes, domain: bytes) -> str:
    return (b"\xee" + key + domain).hex()


def _b64_secret(key: bytes, domain: bytes) -> str:
    enc = base64.urlsafe_b64encode(b"\xee" + key + domain)
    return enc.rstrip(b"=").decode()


class TestParseSecret:
    @pytest.mark.parametrize(
        "key,domain",
        [(b"\x01" * 16, b"www.microsoft.com"), (b"\x0f" * 16, b"api.mtgsome.zed")],
    )
    def test_hex(self, key, domain):
        parsed_key, parsed_domain = mtg_probe.parse_faketls_secret(_hex_secret(key, domain))
        assert parsed_key == key
        assert parsed_domain == domain

    def test_base64(self):
        key = b"\x01" * 16
        domain = b"www.microsoft.com"
        parsed_key, parsed_domain = mtg_probe.parse_faketls_secret(_b64_secret(key, domain))
        assert parsed_key == key
        assert parsed_domain == domain

    def test_rejects_wrong_first_byte(self):
        secret = (b"\x7f" + b"\x01" * 16 + b"www.microsoft.com").hex()
        with pytest.raises(ValueError):
            mtg_probe.parse_faketls_secret(secret)


class TestClientHello:
    def test_length_sni_and_hmac(self):
        key = b"\x01" * 16
        domain = b"www.microsoft.com"
        now = 1769000000
        rand = bytes(range(64))
        hello = mtg_probe.build_client_hello(key, domain, now, rand)
        assert len(hello) == 517
        sni_inner = b"\x00" + struct.pack(">H", len(domain)) + domain
        sni = struct.pack(">H", len(sni_inner)) + sni_inner
        sni_ext = b"\x00\x00" + struct.pack(">H", len(sni)) + sni
        assert sni_ext in hello
        start = hello.index(sni_ext)
        assert hello[start + len(sni_ext) - len(domain) : start + len(sni_ext)] == domain
        zeroed = hello[:11] + b"\x00" * 32 + hello[43:]
        recomputed = hmac.new(key, zeroed, hashlib.sha256).digest()
        diff = bytes(a ^ b for a, b in zip(hello[11:43], recomputed, strict=False))
        assert diff == b"\x00" * 28 + struct.pack("<I", now)


def _ctr(key: bytes, iv: bytes):
    return Cipher(algorithms.AES(key), modes.CTR(iv))


async def _fake_mtg(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    key: bytes,
    mode: str,
) -> None:
    try:
        hello = b""
        while len(hello) < 5:
            hello += await reader.readexactly(5 - len(hello))
        record_len = struct.unpack(">H", hello[3:5])[0]
        hello += await reader.readexactly(record_len)
        if mode == "close":
            writer.close()
            return
        writer.write(b"\x16\x03\x03\x00\x1d" + b"\x00" * 29)
        writer.write(b"\x14\x03\x03\x00\x01" + b"\x00")
        writer.write(b"\x17\x03\x03\x00\x00")
        await writer.drain()
        record = b""
        while len(record) < 5:
            record += await reader.readexactly(5 - len(record))
        if record[0] == 0x14:
            record += await reader.readexactly(struct.unpack(">H", record[3:5])[0])
            record = b""
            while len(record) < 5:
                record += await reader.readexactly(5 - len(record))
        record += await reader.readexactly(struct.unpack(">H", record[3:5])[0])
        payload = record[5:]
        init = bytes(payload[:56])
        enc_key = hashlib.sha256(init[8:40] + key).digest()
        enc_iv = init[40:56]
        rev = init[8:56][::-1]
        dec_key = hashlib.sha256(rev[:32] + key).digest()
        dec_iv = rev[32:48]
        server_dec = _ctr(enc_key, enc_iv).decryptor()
        server_dec.update(b"\x00" * 64)
        packet = server_dec.update(payload[64:])
        n = struct.unpack("<I", packet[:4])[0]
        mt = packet[4 : 4 + n]
        body = mt[20:]
        req_nonce = body[4:20]
        resp_nonce = req_nonce if mode == "ok" else os.urandom(16)
        resp_body = bytes([0x63, 0x24, 0x16, 0x05]) + resp_nonce + b"\x00" * 8
        resp_mt = struct.pack("<qqi", 0, int(time.time() * 2**32) & ~3, len(resp_body)) + resp_body
        resp_packet = struct.pack("<I", len(resp_mt) + 4) + resp_mt + b"\x00" * 4
        server_enc = _ctr(dec_key, dec_iv).encryptor()
        ct = server_enc.update(resp_packet)
        writer.write(b"\x17\x03\x03" + struct.pack(">H", len(ct)) + ct)
        await writer.drain()
    except (EOFError, OSError):
        pass
    finally:
        writer.close()


async def _start_fake_mtg(key: bytes, mode: str):
    server = await asyncio.start_server(
        lambda r, w: _fake_mtg(r, w, key, mode), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    return server, port


@pytest.mark.asyncio
async def test_full_path_ok():
    key = b"\x01" * 16
    domain = b"www.microsoft.com"
    server, port = await _start_fake_mtg(key, "ok")
    try:
        result = await mtg_probe.mtproto_probe(
            "127.0.0.1", port, _hex_secret(key, domain), dc=2, timeout=5.0
        )
    finally:
        server.close()
        await server.wait_closed()
    assert result.verdict == "ok"
    assert result.error is None
    assert result.tls_ms is not None
    assert result.total_ms is not None


@pytest.mark.asyncio
async def test_close_after_client_hello_tls_failed():
    key = b"\x01" * 16
    domain = b"www.microsoft.com"
    server, port = await _start_fake_mtg(key, "close")
    try:
        result = await mtg_probe.mtproto_probe(
            "127.0.0.1", port, _hex_secret(key, domain), dc=2, timeout=5.0
        )
    finally:
        server.close()
        await server.wait_closed()
    assert result.verdict == "tls_failed"
    assert result.error is not None


@pytest.mark.asyncio
async def test_wrong_nonce_mtproto_failed():
    key = b"\x01" * 16
    domain = b"www.microsoft.com"
    server, port = await _start_fake_mtg(key, "wrong_nonce")
    try:
        result = await mtg_probe.mtproto_probe(
            "127.0.0.1", port, _hex_secret(key, domain), dc=2, timeout=5.0
        )
    finally:
        server.close()
        await server.wait_closed()
    assert result.verdict == "mtproto_failed"
    assert result.error is not None
