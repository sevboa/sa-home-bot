from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.snapshot import WAIT_INTERVAL_S, WAIT_TIMEOUT_S
from sa_home_bot.proto.messages import ProtoError

EXIT_ERROR = 1
EXIT_TIMEOUT = 124


def drain_command(
    park: Callable[[], dict[str, Any]],
    state: Callable[[], dict[str, Any]],
    restart: Callable[[], None],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    interval: float = WAIT_INTERVAL_S,
    timeout: float = WAIT_TIMEOUT_S,
) -> int:
    start = clock()
    try:
        park()
    except (OSError, ProtoError) as exc:
        _report(exc)
        return EXIT_ERROR
    while True:
        try:
            answer = state()
        except (OSError, ProtoError) as exc:
            _report(exc)
            return EXIT_ERROR
        if not answer[protocol.FIELD_BUSY]:
            restart()
            return 0
        sleep(interval)
        if clock() - start >= timeout:
            print(
                f"Исполнитель не освободился за {timeout:.0f} с — рестарт отложен.",
                file=sys.stderr,
            )
            return EXIT_TIMEOUT


def _report(exc: OSError | ProtoError) -> None:
    print(f"Исполнитель недоступен: {exc}", file=sys.stderr)
