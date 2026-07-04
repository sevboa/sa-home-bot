"""История отключений машины: `last` (wtmp) + `journalctl` (systemd-журнал).

`last` классифицирует boot-сессии: сессия помечена `crash`, если перед
следующей загрузкой не было штатного shutdown → машину вырубило внезапно
(питание/зависание/reset). Если у сессии есть время конца — был штатный
shutdown в этот момент (точный `down_at`).

Для внезапного обрыва точного момента в wtmp нет, поэтому его оцениваем по
последнему событию systemd-журнала той сессии (`journalctl --list-boots`):
boot-сессии сопоставляются по времени старта (last START ≈ journal first_entry
с точностью до секунд), и `last_entry` крашнувшейся сессии ≈ момент обрыва.

Парсинг вынесен в чистые функции (тестируются на фикстурах), блокирующий
запуск подпроцессов делает `read_power_events_sync` через executor вызывающего
кода (инвариант ARCHITECTURE.md §9.6 — не блокировать event loop).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sa_home_bot.domain.models import POWER_CLEAN, POWER_UNEXPECTED, PowerEvent

log = logging.getLogger(__name__)

# -xR: reboot-псевдопользователь, без hostname-колонки.
# --time-format iso: разбираемое время с offset вместо локализованного.
LAST_ARGS = ["last", "-xR", "--time-format", "iso", "reboot"]
JOURNAL_ARGS = ["journalctl", "--list-boots", "-o", "json"]

_STILL_RUNNING = "still"  # last пишет "still running" для текущей сессии
# Макс. расхождение START (last) и first_entry (journal) при сопоставлении сессий.
_MATCH_TOLERANCE = timedelta(minutes=2)


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
    краха). Для внезапных `down_at` пока None — заполняется журналом отдельно.

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
                    down_at=None,  # оценим по journal-у
                    up_at=newer_boot_at,
                    down_approx=True,
                )
            )
        else:
            end = _parse_ts(end_token)
            events.append(
                PowerEvent(
                    kind=POWER_CLEAN,
                    boot_at=start,
                    down_at=end,  # точный момент штатного выключения
                    up_at=newer_boot_at,
                )
            )
        newer_boot_at = start

    return events[:limit]


def parse_journal_boots(text: str) -> list[tuple[datetime, datetime]]:
    """Разобрать `journalctl --list-boots -o json` в (first_entry, last_entry).

    Времена в JSON — микросекунды CLOCK_REALTIME; возвращаем aware UTC-datetime.
    """
    try:
        boots = json.loads(text)
    except (ValueError, TypeError):
        return []
    out: list[tuple[datetime, datetime]] = []
    for b in boots:
        first, last = b.get("first_entry"), b.get("last_entry")
        if not isinstance(first, int) or not isinstance(last, int):
            continue
        out.append(
            (
                datetime.fromtimestamp(first / 1e6, tz=UTC),
                datetime.fromtimestamp(last / 1e6, tz=UTC),
            )
        )
    return out


def enrich_unexpected(
    events: list[PowerEvent], boots: list[tuple[datetime, datetime]]
) -> list[PowerEvent]:
    """Заполнить `down_at` внезапных отключений last_entry-ем journal-сессии.

    Сессия сопоставляется по времени старта: last `boot_at` ≈ journal
    `first_entry`. Если пары в пределах допуска нет — событие остаётся без
    `down_at` (простой посчитать нельзя).
    """
    out: list[PowerEvent] = []
    for ev in events:
        if ev.kind != POWER_UNEXPECTED or ev.down_at is not None:
            out.append(ev)
            continue
        match = _nearest_boot(ev.boot_at, boots)
        if match is None:
            out.append(ev)
            continue
        out.append(replace(ev, down_at=match[1]))  # last_entry ≈ момент обрыва
    return out


def _nearest_boot(
    boot_at: datetime, boots: list[tuple[datetime, datetime]]
) -> tuple[datetime, datetime] | None:
    best = None
    best_delta = _MATCH_TOLERANCE
    for first, last in boots:
        delta = abs(first - boot_at)
        if delta <= best_delta:
            best, best_delta = (first, last), delta
    return best


def _run(args: list[str]) -> str | None:
    """Запустить подпроцесс, вернуть stdout или None при любой ошибке."""
    if shutil.which(args[0]) is None:
        log.warning("Утилита `%s` не найдена", args[0])
        return None
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Не удалось выполнить `%s`: %s", args[0], exc)
        return None
    if proc.returncode != 0:
        log.warning("`%s` вернул код %s: %s", args[0], proc.returncode, proc.stderr.strip())
        return None
    return proc.stdout


def read_power_events_sync(limit: int = 10) -> list[PowerEvent]:
    """Блокирующе собрать историю отключений. Вызывать через executor.

    `last` обязателен (классификация и времена). `journalctl` опционален —
    без него внезапные события остаются без точного `down_at`.
    """
    last_out = _run(LAST_ARGS)
    if last_out is None:
        return []
    events = parse_last_reboots(last_out, limit=limit)

    journal_out = _run(JOURNAL_ARGS)
    if journal_out is not None:
        events = enrich_unexpected(events, parse_journal_boots(journal_out))
    return events
