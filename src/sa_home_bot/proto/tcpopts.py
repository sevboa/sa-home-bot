"""Настройка TCP-сокета на живучесть: keepalive + потолок ретрансмиссий.

Общая для обеих сторон соединения. Живая находка 2026-07-28: клиентские
сокеты получали keepalive с v0.42.0, а ПРИНЯТЫЕ сервером — нет: входящий
half-open (сосед умер без FIN) жил на сервере до tcp_retries2 (~15 минут)
или вечно на простое, и убирался только сменой инкарнации соседа — которая
случается, лишь если сосед реально перезапустился И смог доподключиться.
"""

from __future__ import annotations

import contextlib
import socket
import sys

# Живая находка 2026-07-20: TCP по умолчанию не замечает молча пропавшего
# собеседника (сон машины, обрыв сети без FIN/RST) — readline() висит
# неограниченно. Без явной настройки ОС-таймаут — часы, не годится.
TCP_KEEPALIVE_IDLE_S = 20
TCP_KEEPALIVE_INTERVAL_S = 10
TCP_KEEPALIVE_COUNT = 3

# Живая находка 2026-07-28: keepalive выше НЕ покрывает случай, когда в
# очереди отправки лежат неотправленные данные — пробы шлются только на
# ПРОСТАИВАЮЩЕМ соединении, а при непустом Send-Q ядро вместо них гоняет
# ретрансмиссии по tcp_retries2 (дефолт 15) ≈ 15 минут. Живой баг на проде:
# winpc перезагрузилась, alfred держал `ESTAB 0 13720` к ней, PeerLink.alive
# врал "жив", и запросы к ноде уходили в таймаут, пока её не перезапустили.
# TCP_USER_TIMEOUT ограничивает именно это: сколько данные могут оставаться
# неподтверждёнными, прежде чем ОС порвёт соединение.
TCP_USER_TIMEOUT_S = 30

# Windows-аналог TCP_USER_TIMEOUT: TCP_MAXRT (в секундах) — той же природы
# потолок на ретрансмиссии. В socket-модуле константы нет, номер опции из
# заголовков Winsock (mstcpip.h).
_WIN_TCP_MAXRT = 5


def enable_tcp_keepalive(sock: socket.socket) -> None:
    """Пробы каждые ``TCP_KEEPALIVE_INTERVAL_S`` после ``_IDLE_S`` простоя;
    ``_COUNT`` неудач подряд — ОС рвёт соединение сама, без участия
    приложения. Плюс ``TCP_USER_TIMEOUT``/``TCP_MAXRT`` — потолок на
    ретрансмиссии, единственное, что работает при непустом Send-Q (см.
    комментарий у константы). Настройка best-effort: не у всех платформ есть
    все опции, голый ``SO_KEEPALIVE`` (системные дефолты) — не хуже, чем было."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if sys.platform == "win32":
        with contextlib.suppress(OSError, AttributeError):
            sock.ioctl(
                socket.SIO_KEEPALIVE_VALS,
                (1, TCP_KEEPALIVE_IDLE_S * 1000, TCP_KEEPALIVE_INTERVAL_S * 1000),
            )
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, _WIN_TCP_MAXRT, TCP_USER_TIMEOUT_S)
    else:
        # TCP_USER_TIMEOUT — в миллисекундах, в отличие от keepalive-опций.
        user_timeout_opt = getattr(socket, "TCP_USER_TIMEOUT", None)
        if user_timeout_opt is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(
                    socket.IPPROTO_TCP, user_timeout_opt, TCP_USER_TIMEOUT_S * 1000
                )
        for opt_name, value in (
            ("TCP_KEEPIDLE", TCP_KEEPALIVE_IDLE_S),
            ("TCP_KEEPINTVL", TCP_KEEPALIVE_INTERVAL_S),
            ("TCP_KEEPCNT", TCP_KEEPALIVE_COUNT),
        ):
            opt = getattr(socket, opt_name, None)
            if opt is not None:
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.IPPROTO_TCP, opt, value)
