"""SmartScanJob — нечастый снимок SMART-счётчиков дисков + алерт на изменения.

Read-only опрос smartctl (``-H -A``), без self-test'ов — диск не изнашивается.
Дельта считается против последнего снимка в БД (baseline): рост сбойных
секторов / падение класса здоровья → уведомление о деградации; обратное
изменение → о восстановлении.

Идемпотентность: baseline сдвигается только после того, как алерт доставлен
хотя бы одному подписчику (или слать некому). Если подписчики есть, но доставка
целиком провалилась (бот офлайн), baseline НЕ двигаем — деградацию повторим на
следующем прогоне, а не потеряем.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sa_home_bot.domain.models import SmartChange
from sa_home_bot.domain.render import render_smart_change
from sa_home_bot.domain.smart import diff_smart
from sa_home_bot.jobs.base import JobContext, JobResult

log = logging.getLogger(__name__)

DEDUP_KEY = "smart-scan"
JOB_TYPE = "smart_scan"


def _now() -> datetime:
    return datetime.now(tz=UTC)


class SmartScanJob:
    @property
    def dedup_key(self) -> str:
        return DEDUP_KEY

    @property
    def job_type(self) -> str:
        return JOB_TYPE

    async def run(self, ctx: JobContext) -> JobResult:
        snapshots = await ctx.sensors.read_smart_snapshots()
        sent = 0
        changes = 0
        for curr in snapshots:
            prev = await ctx.store.get_smart_snapshot(curr.component_id)
            change = diff_smart(prev, curr)
            if change is None:
                await ctx.store.save_smart_snapshot(curr)
                continue
            changes += 1
            delivered, attempted = await self._dispatch(ctx, change)
            if attempted and not delivered:
                # Подписчики есть, но никому не дошло — не сдвигаем baseline,
                # чтобы повторить деградацию на следующем прогоне.
                log.warning(
                    "SMART-алерт по %s не доставлен — повтор на следующем прогоне",
                    change.component_id,
                )
                continue
            await ctx.store.save_smart_snapshot(curr)
            if delivered:
                sent += 1
        return JobResult(
            components_scanned=len(snapshots),
            transitions=changes,
            alerts_sent=sent,
        )

    async def _dispatch(self, ctx: JobContext, change: SmartChange) -> tuple[bool, bool]:
        """Разослать событие; вернуть (доставлено_хоть_кому, были_подписчики)."""
        text = render_smart_change(change)
        subs = ctx.subscriptions.accepting(change.event_type)
        delivered = False
        for sub in subs:
            message_id = await ctx.notifier.send_direct(sub.chat_id, text)
            if message_id is not None:
                delivered = True
        return delivered, bool(subs)
