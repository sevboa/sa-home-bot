"""Сборка и жизненный цикл telegram-бота (ARCHITECTURE.md §8).

С этапа 13 бот — фронтенд: датчиками, порогами и планировщиком владеет
служба monitor (отдельный процесс, `--service monitor`). Бот держит одно
подключение к ней (ServiceLink), получает события и рассылает их в чаты;
/status и прочие данные — через get_state по протоколу. В БД бота остаются
только его вещи: app_state, message_id для reply-цепочек, лимит форс-сканов.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sa_home_bot.bot.ai_flow import RESTART_TEXT, ActiveAiChats
from sa_home_bot.bot.dispatch import TelegramEventDispatcher
from sa_home_bot.bot.lifecycle import (
    broadcast_system,
    render_link_restored,
    render_shutdown,
    render_startup,
)
from sa_home_bot.bot.link_watch import LinkWatchMiddleware
from sa_home_bot.bot.monitor_events import build_event_handler
from sa_home_bot.bot.node_events import build_node_event_handler
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.reminders import reminder_loop
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.setup import build_bot, build_dispatcher, set_bot_commands
from sa_home_bot.bot.torrent_pending import PendingTorrents
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.runtime import Runtime
from sa_home_bot.sensors.power import read_power_events_sync
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)

STATE_CLEAN_SHUTDOWN = "last_shutdown_clean"


async def run(settings: Settings) -> None:
    runtime = Runtime()

    # 1-2. Логирование уже настроено в CLI; БД бота + миграции.
    db = Database(settings.database.path)
    await db.open()
    await apply_migrations(db)
    store = Store(db)

    # Определяем характер прошлого завершения (clean/crash), затем помечаем "running".
    prev = await store.get_state(STATE_CLEAN_SHUTDOWN)
    started_clean = prev in (None, "1")
    await store.set_state(STATE_CLEAN_SHUTDOWN, "0")

    # 3. Подписки.
    book = SubscriptionBook.from_config(settings.subscriptions)

    # 4. Bot + Notifier + watchdog связи.
    bot = build_bot(settings.telegram.token)
    notifier = Notifier(bot)
    bot_username = (await bot.get_me()).username

    async def on_reconnect(downtime: float) -> None:
        await broadcast_system(book, notifier, render_link_restored(downtime))

    bot.session.middleware(LinkWatchMiddleware(on_reconnect))
    dp = build_dispatcher(book)

    # 5. Валидация подписок (пометка broken).
    await book.validate_on_startup(bot)

    # 6. Меню команд по правам чатов.
    await set_bot_commands(bot, book)

    # 7. Системное приветствие (clean/crash). После сбоя пробуем приложить
    #    детали последнего отключения, если это была потеря питания.
    last_outage = None
    if not started_clean:
        loop = asyncio.get_running_loop()
        events, _ = await loop.run_in_executor(None, read_power_events_sync, 0, 1)
        if events:
            last_outage = events[0]
    await broadcast_system(
        book, notifier, render_startup(clean=started_clean, last_outage=last_outage)
    )

    # 8. Связь со службами ноды: монитор (события → рассылка), сама нода
    #    (карточки нод/служб) и apps (скилы-приложения: команды меню).
    dispatcher = TelegramEventDispatcher(notifier, book, store)
    link = ServiceLink(
        settings.monitor.socket,
        token=settings.swarm.token,
        display_name="монитор",
        on_event=build_event_handler(dispatcher),
    )
    await link.start()
    node_link = ServiceLink(
        settings.node.socket,
        token=settings.swarm.token,
        display_name="нода",
        on_event=build_node_event_handler(book, notifier),
    )
    await node_link.start()

    async def refresh_menu() -> None:
        # Скилы-приложения появились/изменились — перестроить меню команд.
        await set_bot_commands(bot, book, await apps_link.actions())

    apps_link = ServiceLink(
        settings.apps.socket,
        token=settings.swarm.token,
        display_name="приложения",
        on_connected=refresh_menu,
    )
    await apps_link.start()

    torrents_link = ServiceLink(
        settings.torrents.socket,
        token=settings.swarm.token,
        display_name="торренты",
    )
    await torrents_link.start()
    pending_torrents = PendingTorrents()

    # 9. Тул remind (/ai, LLM_INTEGRATION_PLAN.md §8.5) — фоновый опрос своей
    #    очереди в БД бота, доставка через тот же Notifier.
    reminders_task = asyncio.create_task(reminder_loop(store, notifier), name="reminders")

    # Чаты с прямо сейчас идущим /ai-запросом — на останове (_shutdown)
    # известить их RESTART_TEXT'ом до закрытия сессии бота (см. докстринг
    # ActiveAiChats: думающий think_chat-ответ может идти 30-40с, за это
    # время бота вполне могут перезапустить деплоем).
    active_ai_chats = ActiveAiChats()

    # 10. Polling.
    polling_task = asyncio.create_task(
        dp.start_polling(
            bot,
            store=store,
            link=link,
            node_link=node_link,
            apps_link=apps_link,
            torrents_link=torrents_link,
            pending_torrents=pending_torrents,
            runtime=runtime,
            config=settings,
            notifier=notifier,
            book=book,
            bot_username=bot_username,
            active_ai_chats=active_ai_chats,
            handle_signals=False,
        ),
        name="polling",
    )

    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    log.info("Бот запущен (uptime-старт зафиксирован)")

    # 11. Ждать сигнала, затем остановить всё в обратном порядке.
    try:
        await lifespan.wait()
    finally:
        await _shutdown(
            dp=dp,
            polling_task=polling_task,
            reminders_task=reminders_task,
            active_ai_chats=active_ai_chats,
            link=link,
            node_link=node_link,
            apps_link=apps_link,
            torrents_link=torrents_link,
            book=book,
            notifier=notifier,
            store=store,
            bot=bot,
            db=db,
        )


async def _shutdown(
    *,
    dp,
    polling_task: asyncio.Task,
    reminders_task: asyncio.Task,
    active_ai_chats: ActiveAiChats,
    link: ServiceLink,
    node_link: ServiceLink,
    apps_link: ServiceLink,
    torrents_link: ServiceLink,
    book: SubscriptionBook,
    notifier: Notifier,
    store: Store,
    bot,
    db: Database,
) -> None:
    log.info("Останов приложения...")

    # Стоп фонового опроса напоминаний — до polling, не участвует в приёме
    # апдейтов Telegram, безопасно снять первым.
    reminders_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await reminders_task

    # Стоп связи со службами (новые события не принимаются).
    await link.stop()
    await node_link.stop()
    await apps_link.stop()
    await torrents_link.stop()

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

    # Живая находка 2026-07-24: think_chat растянул /ai-ответ до 30-40с —
    # закрытие сессии бота ниже посреди такого запроса роняет его голой
    # TelegramNetworkError/"Connector is closed" пользователю в лицо. Пока
    # сессия ещё жива — известить чаты с активным запросом в характере
    # (RESTART_TEXT), а не оставлять их с сырой сетевой ошибкой. Сам
    # хендлер после этого всё равно упадёт при попытке отправить настоящий
    # ответ — это ожидаемо (see ActiveAiChats docstring), просто тихо.
    for chat_id in active_ai_chats.snapshot():
        try:
            await notifier.send_direct(chat_id, RESTART_TEXT)
        except Exception:  # noqa: BLE001 — сбой одного уведомления не должен рвать shutdown
            log.warning("Не удалось известить chat=%s о рестарте", chat_id, exc_info=True)

    # Дослать прощание, пока сессия бота жива.
    await broadcast_system(book, notifier, render_shutdown())

    # Флаг чистого завершения — до закрытия БД.
    await store.set_state(STATE_CLEAN_SHUTDOWN, "1")

    await bot.session.close()
    await db.close()
    log.info("Останов завершён чисто")
