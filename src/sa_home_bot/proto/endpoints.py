"""Endpoint'ы транспорта протокола v0: unix-сокет или TCP.

В конфиге и CLI endpoint задаётся строкой:

- ``./data/node.sock`` или ``unix:./data/node.sock`` — unix-сокет (Linux);
- ``tcp://127.0.0.1:8710`` — TCP (Windows и межнодовый канал), сервер на TCP
  требует токен (см. PROTOCOL.md, «Транспорт»).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

TCP_PREFIX = "tcp://"
UNIX_PREFIX = "unix:"


def _unix_endpoint(path: Path) -> UnixEndpoint:
    if sys.platform == "win32":
        raise ValueError(
            f"unix-сокет {str(path)!r} недоступен на Windows — "
            "укажите в конфиге tcp://127.0.0.1:<порт> (см. PROTOCOL.md, «Транспорт»)"
        )
    return UnixEndpoint(path)


@dataclass(frozen=True)
class UnixEndpoint:
    path: Path

    def __str__(self) -> str:
        return str(self.path)


@dataclass(frozen=True)
class TcpEndpoint:
    host: str
    port: int

    def __str__(self) -> str:
        return f"tcp://{self.host}:{self.port}"


Endpoint = UnixEndpoint | TcpEndpoint


def parse_endpoint(value: str | Path | Endpoint) -> Endpoint:
    """Строка/путь → endpoint. ValueError на непонятный формат."""
    if isinstance(value, (UnixEndpoint, TcpEndpoint)):
        return value
    if isinstance(value, Path):
        return _unix_endpoint(value)

    raw = value.strip()
    if not raw:
        raise ValueError("пустой endpoint")

    if raw.startswith(TCP_PREFIX):
        rest = raw[len(TCP_PREFIX) :]
        host, sep, port_str = rest.rpartition(":")
        if not sep or not host or not port_str.isdigit():
            raise ValueError(f"невалидный tcp-endpoint: {raw!r} (ожидается tcp://host:port)")
        port = int(port_str)
        if not 1 <= port <= 65535:
            raise ValueError(f"невалидный порт в endpoint: {raw!r}")
        return TcpEndpoint(host=host.strip("[]"), port=port)

    if raw.startswith(UNIX_PREFIX):
        rest = raw[len(UNIX_PREFIX) :]
        if rest.startswith("//"):  # url-форма unix:///abs/path
            rest = rest[2:]
        if not rest:
            raise ValueError(f"невалидный unix-endpoint: {raw!r}")
        return _unix_endpoint(Path(rest))

    return _unix_endpoint(Path(raw))


def resolve_endpoint(value: str | Path | Endpoint, base_dir: Path | None = None) -> Endpoint:
    """parse_endpoint + относительный unix-путь резолвится от ``base_dir``.

    Нужен фронтендам, читающим endpoint из чужого конфига (nodectl -c …):
    относительный путь в конфиге — относительно каталога конфига, а не CWD.
    """
    endpoint = parse_endpoint(value)
    if (
        isinstance(endpoint, UnixEndpoint)
        and base_dir is not None
        and not endpoint.path.is_absolute()
    ):
        return UnixEndpoint(base_dir / endpoint.path)
    return endpoint
