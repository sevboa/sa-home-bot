from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sa_home_bot.domain.models import POWER_CLEAN, POWER_UNEXPECTED, PowerEvent
from sa_home_bot.utils.requirements import (
    Requirement,
    RequirementStatus,
    looks_like_permission_error,
    requirements_registry,
)

log = logging.getLogger(__name__)

LAST_ARGS = ["last", "-xR", "--time-format", "iso", "reboot"]
JOURNAL_ARGS = ["journalctl", "--list-boots", "-o", "json"]

LAST_REQUIREMENT = Requirement(
    program="last",
    package="util-linux",
    platforms=("linux",),
    note="история отключений машины",
)
JOURNALCTL_REQUIREMENT = Requirement(
    program="journalctl",
    package="systemd",
    platforms=("linux",),
    note="точное время внезапных отключений",
)

_STILL_RUNNING = "still"
_MATCH_TOLERANCE = timedelta(minutes=2)


def _parse_ts(token: str) -> datetime | None:
    try:
        return datetime.fromisoformat(token)
    except ValueError:
        return None


def parse_last_reboots(text: str, limit: int = 10) -> list[PowerEvent]:
    events: list[PowerEvent] = []
    newer_boot_at: datetime | None = None

    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "reboot":
            continue
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
            newer_boot_at = start
            continue

        if end_token == "crash":
            events.append(
                PowerEvent(
                    kind=POWER_UNEXPECTED,
                    boot_at=start,
                    down_at=None,
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
                    down_at=end,
                    up_at=newer_boot_at,
                )
            )
        newer_boot_at = start

    return events[:limit]


def parse_journal_boots(text: str) -> list[tuple[datetime, datetime]]:
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
    out: list[PowerEvent] = []
    for ev in events:
        if ev.kind != POWER_UNEXPECTED or ev.down_at is not None:
            out.append(ev)
            continue
        match = _nearest_boot(ev.boot_at, boots)
        if match is None:
            out.append(ev)
            continue
        out.append(replace(ev, down_at=match[1]))
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


def _run(args: list[str], requirement: Requirement | None = None) -> str | None:
    if shutil.which(args[0]) is None:
        log.warning("Утилита `%s` не найдена", args[0])
        if requirement is not None:
            requirements_registry.report(requirement, RequirementStatus.MISSING_PROGRAM)
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
        if requirement is not None and looks_like_permission_error(proc.stderr):
            requirements_registry.report(requirement, RequirementStatus.NEEDS_PRIVILEGE)
        return None
    if requirement is not None:
        requirements_registry.report(requirement, RequirementStatus.OK)
    return proc.stdout


def read_uptime_sync() -> timedelta | None:
    try:
        import psutil

        boot = datetime.fromtimestamp(psutil.boot_time(), tz=UTC)
        return datetime.now(tz=UTC) - boot
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось получить аптайм: %s", exc)
        return None


def read_power_events_sync(
    offset: int = 0, limit: int = 10
) -> tuple[list[PowerEvent], bool]:
    if not LAST_REQUIREMENT.available():
        return [], False
    last_out = _run(LAST_ARGS, requirement=LAST_REQUIREMENT)
    if last_out is None:
        return [], False
    events = parse_last_reboots(last_out, limit=offset + limit + 1)

    journal_out = _run(JOURNAL_ARGS, requirement=JOURNALCTL_REQUIREMENT)
    if journal_out is not None:
        events = enrich_unexpected(events, parse_journal_boots(journal_out))

    page = events[offset : offset + limit]
    has_next = len(events) > offset + limit
    return page, has_next
