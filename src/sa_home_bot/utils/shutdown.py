"""Останов с потолком времени: ни один шаг не имеет права висеть вечно.

Повод — этап 29 (живой сбой на jeeves 2026-07-30). Нода приняла
``restart_node``, написала «Останов ноды...» и осталась работать: следующей
строки в логе не появилось ни через минуту, ни через три — процесс дожил до
``TimeoutStopSec`` и был добит SIGKILL. Разбор показал класс проблемы, а не
одну ошибку: последовательность останова состояла из голых ``await``, каждый
из которых полагался на добрую волю собеседника. Достаточно одного
полумёртвого сокета (данные в Send-Q не уходят, FIN не подтверждается) —
и останов не заканчивается никогда, а по логу не видно даже, на каком шаге.

Отсюда два правила, которые задаёт этот модуль:

- **у каждого шага останова есть потолок**, и просроченный шаг бросается с
  явной записью в лог — останов идёт дальше;
- **у процесса есть жёсткий предел**: если останов не уложился даже в общий
  бюджет (например, повис не наш код, а уборка задач в ``asyncio.run``),
  сторожевой поток добивает процесс сам. Нода, которая не может выключиться,
  хуже упавшей: её не обновить и не перезапустить штатно.
"""

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

# Потолок на обычный шаг останова (закрыть линк, снять фоновую задачу).
# Пять секунд — заведомо больше, чем нужно исправному пути, и заведомо
# меньше, чем терпит человек.
STEP_TIMEOUT_S = 5.0

# Код выхода при принудительном добивании себя же. Ненулевой намеренно:
# systemd (`Restart=on-failure`) и WinSW (`<onfailure action="restart"/>`)
# поднимут ноду заново — зависший останов не должен оставлять машину без ноды.
HARD_EXIT_CODE = 10


def dump_pending_tasks(limit: int = 25) -> None:
    """Выписать в лог незавершённые задачи цикла — кто именно не отпускает.

    Ровно этой строчки не хватало при разборе на jeeves: в логе было «Останов
    ноды...» и тишина, а дальше оставалось только гадать. Имена задач у нас
    осмысленные (`peer-link-alfred`, `lease-manager`, ...), поэтому одного
    дампа достаточно, чтобы понять, где висим, — без DEBUG и без повторов.
    """
    try:
        current = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks() if not t.done() and t is not current]
    except RuntimeError:  # цикла уже нет — дампить нечего
        return
    log.warning("Останов: незавершённых задач — %d", len(tasks))
    for task in tasks[:limit]:
        stack = task.get_stack(limit=3)
        where = " ← ".join(f"{f.f_code.co_name}:{f.f_lineno}" for f in reversed(stack))
        log.warning("  задача %s: %s", task.get_name(), where or "стек недоступен")


class ShutdownBudget:
    """Общий бюджет останова: потолок на шаг + общий дедлайн.

    Потолок на шаг не даёт зависнуть одному собеседнику; общий дедлайн не даёт
    сложиться десятку честных, но небыстрых шагов (пиров в рое может быть
    много, и каждый по 5 с — это уже минуты).
    """

    def __init__(self, total_s: float, *, step_s: float = STEP_TIMEOUT_S) -> None:
        self._deadline = time.monotonic() + total_s
        self._step_s = step_s
        self.total_s = total_s
        # Шаги, не уложившиеся в свой потолок: по ним останов считается
        # грязным, и об этом говорится одной строкой в конце.
        self.overdue: list[str] = []
        self._dumped = False

    @property
    def remaining_s(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    async def step(
        self, name: str, awaitable: Awaitable[Any], *, timeout: float | None = None
    ) -> bool:
        """Выполнить шаг останова под потолком. True — уложился.

        Просрочка не выбрасывает исключение наружу: останов обязан пройти все
        шаги (соседей предупредить, детей погасить, сокеты закрыть), даже если
        один из них безнадёжен.
        """
        cap = self._step_s if timeout is None else timeout
        # Нижняя граница: даже за дедлайном шагу даётся мгновение — уже
        # готовый шаг завершится сразу, а зависший честно попадёт в overdue.
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
        except Exception:  # noqa: BLE001 — сбойный шаг не отменяет остальные
            log.exception("Останов: шаг «%s» завершился ошибкой", name)
            return False


def arm_hard_exit(delay_s: float, exit_code: int = HARD_EXIT_CODE) -> None:
    """Взвести сторожа: не выключились за ``delay_s`` — уходим принудительно.

    Поток демонский и не снимается: если процесс успевает выйти сам (обычный
    случай), сторож умирает вместе с ним. Это последний рубеж, за нашим кодом:
    ``asyncio.run()`` в конце сам отменяет оставшиеся задачи и ждёт их
    **без потолка**, и там уже нечего ограничивать изнутри.
    """

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
