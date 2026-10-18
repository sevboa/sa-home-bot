from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import threading
import time
from collections.abc import Awaitable
from typing import Any

log = logging.getLogger(__name__)

STEP_TIMEOUT_S = 5.0

HARD_EXIT_CODE = 10


def dump_pending_tasks(limit: int = 25) -> None:
    try:
        current = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks() if not t.done() and t is not current]
    except RuntimeError:
        return
    log.warning("Останов: незавершённых задач — %d", len(tasks))
    for task in tasks[:limit]:
        stack = task.get_stack(limit=3)
        where = " ← ".join(f"{f.f_code.co_name}:{f.f_lineno}" for f in reversed(stack))
        log.warning("  задача %s: %s", task.get_name(), where or "стек недоступен")


class ShutdownBudget:
    def __init__(self, total_s: float, *, step_s: float = STEP_TIMEOUT_S) -> None:
        self._deadline = time.monotonic() + total_s
        self._step_s = step_s
        self.total_s = total_s
        self.overdue: list[str] = []
        self._dumped = False

    @property
    def remaining_s(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    async def step(
        self, name: str, awaitable: Awaitable[Any], *, timeout: float | None = None
    ) -> bool:
        cap = self._step_s if timeout is None else timeout
        budget = max(0.1, min(cap, self.remaining_s))
        try:
            await asyncio.wait_for(awaitable, timeout=budget)
            return True
        except TimeoutError:
            self.overdue.append(name)
            log.error("Останов: шаг «%s» не уложился в %.1f с — бросаю и иду дальше", name, budget)
            if not self._dumped:
                self._dumped = True
                dump_pending_tasks()
            return False
        except Exception:
            log.exception("Останов: шаг «%s» завершился ошибкой", name)
            return False


def arm_hard_exit(delay_s: float, exit_code: int = HARD_EXIT_CODE) -> None:
    def _watch() -> None:
        time.sleep(delay_s)
        log.error(
            "Останов не закончился за %.0f с — принудительный выход (код %d)", delay_s, exit_code
        )
        with contextlib.suppress(Exception):
            sys.stderr.flush()
            logging.shutdown()
        os._exit(exit_code)

    threading.Thread(target=_watch, name="shutdown-watchdog", daemon=True).start()
