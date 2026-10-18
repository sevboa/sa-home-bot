from __future__ import annotations

import contextlib
import socket
import sys

TCP_KEEPALIVE_IDLE_S = 20
TCP_KEEPALIVE_INTERVAL_S = 10
TCP_KEEPALIVE_COUNT = 3

TCP_USER_TIMEOUT_S = 30

WINDOWS_TCP_MAXRT_S = 5


def enable_tcp_keepalive(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if sys.platform == "win32":
        with contextlib.suppress(OSError, AttributeError):
            sock.ioctl(
                socket.SIO_KEEPALIVE_VALS,
                (1, TCP_KEEPALIVE_IDLE_S * 1000, TCP_KEEPALIVE_INTERVAL_S * 1000),
            )
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, WINDOWS_TCP_MAXRT_S, TCP_USER_TIMEOUT_S)
    else:
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
