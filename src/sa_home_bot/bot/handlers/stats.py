"""/stats — сводка прогонов сканера из job_runs."""

from __future__ import annotations

from datetime import datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from sa_home_bot.bot import commands
from sa_home_bot.db.store import Store

router = Router(name="stats")


def _fmt_run(run: dict) -> str:
    icon = {"ok": "✅", "error": "❌", "running": "⏳"}.get(run["status"], "•")
    started = run["started_at"]
    try:
        started = datetime.fromisoformat(started).strftime("%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        pass
    return f"{icon} {run['job_type']} @ {started}"


@router.message(Command(commands.STATS.name))
async def cmd_stats(message: Message, store: Store) -> None:
    counts = await store.job_run_counts()
    runs = await store.recent_job_runs(limit=8)
    if not runs:
        await message.answer("Прогонов сканера ещё не было.")
        return

    total = sum(counts.values())
    lines = [
        "<b>Статистика сканера</b>",
        f"Всего прогонов: {total} (ok={counts.get('ok', 0)}, "
        f"error={counts.get('error', 0)}, running={counts.get('running', 0)})",
        "",
        "Последние:",
    ]
    lines.extend(_fmt_run(r) for r in runs)
    await message.answer("\n".join(lines))
