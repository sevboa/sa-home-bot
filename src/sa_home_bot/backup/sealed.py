"""Sealed box для бэкапов: зашифровать может любой с публичным ключом, расшифровать — только alfred.

На vpn-нодах (jeeves/wooster) лежит лишь публичный ключ получателя, приватный — только
у alfred, поэтому компрометация VPS с копией бэкапа его не раскрывает.

Схема: эфемерная пара X25519 → ECDH с публичным ключом получателя → HKDF-SHA256
(salt = epk || recipient_pk, info = метка формата) → ChaCha20-Poly1305. В AAD уходит
заголовок (магия + epk), так что подмена любой части блоба ловится тегом.

Формат блоба v1: ``b"SAHB1"`` + epk(32) + nonce(12) + ciphertext(с тегом 16 байт).
Меняя схему, поднимать цифру в магии: ``open_sealed`` отвечает ``SealedError`` на
незнакомую версию, а не пытается гадать.

Ключи в тексте — стандартный base64 (с ``=``, 44 символа для 32 байт).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"SAHB1"
_INFO = b"sa-home-bot backup sealed box v1"
_KEY_LEN = 32
_NONCE_LEN = 12
_TAG_LEN = 16
_HEADER_LEN = len(MAGIC) + _KEY_LEN
_MIN_BLOB = _HEADER_LEN + _NONCE_LEN + _TAG_LEN


class SealedError(Exception):
    """Любая ошибка ключей/формата/расшифровки (чужой ключ, порча, версия)."""


def _raw_public(key: X25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _raw_private(key: X25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def generate_keypair() -> tuple[bytes, bytes]:
    """Новая пара ``(private, public)``, по 32 сырых байта."""
    private = X25519PrivateKey.generate()
    return _raw_private(private), _raw_public(private.public_key())


def public_from_private(private_key: bytes) -> bytes:
    return _raw_public(_load_private(private_key).public_key())


def dump_key(raw: bytes) -> str:
    """32 сырых байта ключа → base64-строка."""
    if len(raw) != _KEY_LEN:
        raise SealedError(f"ключ должен быть {_KEY_LEN} байт, а не {len(raw)}")
    return base64.b64encode(raw).decode("ascii")


def load_key(text: str) -> bytes:
    """base64-строка (пробелы по краям допустимы) → 32 сырых байта ключа."""
    try:
        raw = base64.b64decode(text.strip().encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise SealedError("ключ не является корректным base64") from exc
    if len(raw) != _KEY_LEN:
        raise SealedError(f"ключ должен быть {_KEY_LEN} байт, а не {len(raw)}")
    return raw


def _load_private(raw: bytes) -> X25519PrivateKey:
    try:
        return X25519PrivateKey.from_private_bytes(raw)
    except ValueError as exc:
        raise SealedError("некорректный приватный ключ") from exc


def _load_public(raw: bytes) -> X25519PublicKey:
    try:
        return X25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise SealedError("некорректный публичный ключ") from exc


def _derive(shared: bytes, epk: bytes, recipient_pk: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=epk + recipient_pk, info=_INFO
    ).derive(shared)


def seal(recipient_public_key: bytes, plaintext: bytes) -> bytes:
    """Зашифровать ``plaintext`` для владельца приватного ключа (сырые 32 байта публичного)."""
    recipient = _load_public(recipient_public_key)
    eph = X25519PrivateKey.generate()
    epk = _raw_public(eph.public_key())
    try:
        shared = eph.exchange(recipient)
    except ValueError as exc:  # малый порядок / нулевой общий секрет
        raise SealedError("непригодный публичный ключ получателя") from exc
    key = _derive(shared, epk, recipient_public_key)
    nonce = os.urandom(_NONCE_LEN)
    header = MAGIC + epk
    return header + nonce + ChaCha20Poly1305(key).encrypt(nonce, plaintext, header)


def open_sealed(private_key: bytes, blob: bytes) -> bytes:
    """Расшифровать блоб из :func:`seal`; ``SealedError`` на любую неудачу."""
    if blob[: len(MAGIC) - 1] != MAGIC[:-1]:
        raise SealedError("это не бэкап sa-home-bot (неверная магия)")
    if blob[: len(MAGIC)] != MAGIC:
        raise SealedError(f"неизвестная версия формата: {blob[len(MAGIC) - 1:len(MAGIC)]!r}")
    if len(blob) < _MIN_BLOB:
        raise SealedError("блоб обрезан")
    priv = _load_private(private_key)
    header = blob[:_HEADER_LEN]
    epk = blob[len(MAGIC):_HEADER_LEN]
    nonce = blob[_HEADER_LEN:_HEADER_LEN + _NONCE_LEN]
    ct = blob[_HEADER_LEN + _NONCE_LEN:]
    try:
        shared = priv.exchange(_load_public(epk))
        key = _derive(shared, epk, _raw_public(priv.public_key()))
        return ChaCha20Poly1305(key).decrypt(nonce, ct, header)
    except (InvalidTag, ValueError) as exc:
        raise SealedError("не удалось расшифровать: чужой ключ или данные повреждены") from exc


def write_private_key(path: Path, private_key: bytes) -> None:
    """Записать приватный ключ (base64 + перевод строки) в файл 0600; существующий не затирать."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(dump_key(private_key) + "\n")
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()
        raise


def read_private_key(path: Path) -> bytes:
    """Прочитать приватный ключ из файла, записанного :func:`write_private_key`."""
    try:
        text = Path(path).expanduser().read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise SealedError(f"не удалось прочитать файл ключа {path}: {exc}") from exc
    return load_key(text)
