"""История отключений машины из журнала загрузок (`last` / wtmp).

`last` уже классифицирует boot-сессии: сессия помечена `crash`, если перед
следующей загрузкой не было штатного shutdown → машину вырубило внезапно
(питание/зависание/reset). Если у сессии есть время конца — был штатный
shutdown в этот момент.

Парсинг вынесен в чистую `parse_last_reboots` (тестируется на фикстурах),
блокирующий запуск подпроцесса делает `read_power_events_sync` через executor
вызывающего кода (инвариант ARCHITECTURE.md §9.6 — не блокировать event loop).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime

from sa_home_bot.domain.models import POWER_CLEAN, POWER_UNEXPECTED, PowerEvent

log = logging.getLogger(__name__)

# -xR: reboot/shutdown-псевдопользователи, без hostname-колонки.
# --time-format iso: разбираемое время с offset вместо локализованного.
LAST_ARGS = ["last", "-xR", "--time-format", "iso", "reboot"]

_STILL_RUNNING = "still"  # last пишет "still running" для текущей сессии


def _parse_ts(token: str) -> datetime | None:
    """ISO-время last вида 2026-07-05T00:23:52+0500 → aware datetime."""
    try:
        return datetime.fromisoformat(token)
    except ValueError:
        return None


def parse_last_reboots(text: str, limit: int = 10) -> list[PowerEvent]:
    """Разобрать вывод `last -xR --time-format iso reboot` в список отключений.

    Строки идут от новых к старым (между reboot/boot есть колонка версии ядра):
        reboot system boot <KERNEL> <START>  - still running
        reboot system boot <KERNEL> <START>  - crash
        reboot system boot <KERNEL> <START>  - <END>  (dur)
    Каждая завершённая сессия → одно отключение. `still running` — текущая
    сессия, ещё не отключение (но её START = момент возврата после предыдущего
    краха). Возвращаются не более `limit` последних событий (новые первыми).

    START ищем как первый токен-ISO-дату (устойчиво к ширине колонки ядра),
    затем ожидаем разделитель `-` и токен конца сессии.
    """
    events: list[PowerEvent] = []
    # START более новой (уже обработанной) сессии — момент возврата машины
    # после отключения текущей строки.
    newer_boot_at: datetime | None = None

    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "reboot":
            continue
        # Первый токен-дата — START сессии; сразу за `-` следует токен конца.
        start = None
        idx = 0
        for i, tok in enumerate(parts):
            ts = _parse_ts(tok)
            if ts is not None:
                start, idx = ts, i
                break
        if start is None or idx + 2 >= len(parts) or parts[idx + 1] != "-":
            continue
        end_token = parts[idx + 2]

        if end_token.startswith(_STILL_RUNNING):
            # Текущая сессия — не отключение, но фиксируем момент возврата.
            newer_boot_at = start
            continue

        if end_token == "crash":
            events.append(
                PowerEvent(
                    kind=POWER_UNEXPECTED,
                    boot_at=start,
                    shutdown_at=None,
                    next_boot_at=newer_boot_at,
                )
            )
        else:
            end = _parse_ts(end_token)
            events.append(
                PowerEvent(
                    kind=POWER_CLEAN,
                    boot_at=start,
                    shutdown_at=end,
                    next_boot_at=newer_boot_at,
                )
            )
        newer_boot_at = start

    return events[:limit]


def read_power_events_sync(limit: int = 10) -> list[PowerEvent]:
    """Блокирующе запустить `last` и разобрать вывод. Вызывать через executor."""
    if shutil.which("last") is None:
        log.warning("Утилита `last` не найдена — история отключений недоступна")
        return []
    try:
        proc = subprocess.run(
            LAST_ARGS,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Не удалось выполнить `last`: %s", exc)
        return []
    if proc.returncode != 0:
        log.warning("`last` вернул код %s: %s", proc.returncode, proc.stderr.strip())
        return []
    return parse_last_reboots(proc.stdout, limit=limit)
