"""Сборка и жизненный цикл приложения (ARCHITECTURE.md §8)."""

from __future__ import annotations

import asyncio
import logging

from sentinel_bot.bot.lifecycle import (
    broadcast_system,
    render_link_restored,
    render_shutdown,
    render_startup,
)
from sentinel_bot.bot.link_watch import LinkWatchMiddleware
from sentinel_bot.bot.notifier import Notifier
from sentinel_bot.bot.setup import build_bot, build_dispatcher, set_bot_commands
from sentinel_bot.config import Settings
from sentinel_bot.db.connection import Database
from sentinel_bot.db.migrations import apply_migrations
from sentinel_bot.db.store import Store
from sentinel_bot.jobs.base import JobContext
from sentinel_bot.runtime import Runtime
from sentinel_bot.scheduler.setup import build_scheduler, register_jobs
from sentinel_bot.sensors.source import SensorSource
from sentinel_bot.subscriptions.book import SubscriptionBook
from sentinel_bot.utils.lifespan import Lifespan
from sentinel_bot.worker.queue import DedupQueue
from sentinel_bot.worker.worker import JobWorker

log = logging.getLogger(__name__)

STATE_CLEAN_SHUTDOWN = "last_shutdown_clean"


async def run(settings: Settings) -> None:
    runtime = Runtime()

    # 1-2. Логирование уже настроено в CLI; БД + миграции.
    db = Database(settings.database.path)
    await db.open()
    await apply_migrations(db)
    store = Store(db)

    # Определяем характер прошлого завершения (clean/crash), затем помечаем "running".
    prev = await store.get_state(STATE_CLEAN_SHUTDOWN)
    started_clean = prev in (None, "1")
    await store.set_state(STATE_CLEAN_SHUTDOWN, "0")

    # 3-5. Подписки, датчики, очередь.
    book = SubscriptionBook.from_config(settings.subscriptions)
    sensors = SensorSource(settings.sensors)
    queue = DedupQueue()

    # 6. Bot + Notifier + watchdog связи.
    bot = build_bot(settings.telegram.token)
    notifier = Notifier(bot)

    async def on_reconnect(downtime: float) -> None:
        await broadcast_system(book, notifier, render_link_restored(downtime))

    bot.session.middleware(LinkWatchMiddleware(on_reconnect))
    dp = build_dispatcher(book)

    # 7. Валидация подписок (пометка broken).
    await book.validate_on_startup(bot)

    # 8. Меню команд по правам чатов.
    await set_bot_commands(bot, book)

    # 9. Системное приветствие (clean/crash).
    await broadcast_system(book, notifier, render_startup(clean=started_clean))

    # 10. JobContext + worker.
    ctx = JobContext(
        store=store,
        sensors=sensors,
        notifier=notifier,
        subscriptions=book,
        config=settings,
    )
    worker = JobWorker(queue, ctx)
    worker_task = asyncio.create_task(worker.run(), name="job-worker")

    # 11. Scheduler.
    scheduler = build_scheduler()
    register_jobs(scheduler, queue, settings)
    scheduler.start()

    # 12. Polling.
    polling_task = asyncio.create_task(
        dp.start_polling(
            bot,
            store=store,
            queue=queue,
            runtime=runtime,
            config=settings,
            notifier=notifier,
            book=book,
            handle_signals=False,
        ),
        name="polling",
    )

    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    log.info("Бот запущен (uptime-старт зафиксирован)")

    # 13. Ждать сигнала, затем остановить всё в обратном порядке.
    try:
        await lifespan.wait()
    finally:
        await _shutdown(
            scheduler=scheduler,
            dp=dp,
            polling_task=polling_task,
            queue=queue,
            worker_task=worker_task,
            book=book,
            notifier=notifier,
            store=store,
            bot=bot,
            db=db,
        )


async def _shutdown(
    *,
    scheduler,
    dp,
    polling_task: asyncio.Task,
    queue: DedupQueue,
    worker_task: asyncio.Task,
    book: SubscriptionBook,
    notifier: Notifier,
    store: Store,
    bot,
    db: Database,
) -> None:
    log.info("Останов приложения...")

    # Стоп scheduler (новые тики не ставятся).
    scheduler.shutdown(wait=False)

    # Стоп polling. stop_polling кидает RuntimeError, если polling ещё не успел
    # запуститься (быстрый SIGINT) или упал на старте (например, бэд-токен).
    stopped = False
    try:
        await dp.stop_polling()
        stopped = True
    except RuntimeError:
        log.debug("polling не был запущен — нечего останавливать")
    if not stopped and not polling_task.done():
        polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — ошибку polling не даём сорвать shutdown
        log.warning("polling завершился с ошибкой", exc_info=True)

    # Дослать прощание, пока сессия бота жива.
    await broadcast_system(book, notifier, render_shutdown())

    # Worker дорабатывает текущий job и завершается по sentinel.
    await queue.stop()
    await worker_task

    # Флаг чистого завершения — до закрытия БД.
    await store.set_state(STATE_CLEAN_SHUTDOWN, "1")

    await bot.session.close()
    await db.close()
    log.info("Останов завершён чисто")
