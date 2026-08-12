"""Сборка и жизненный цикл telegram-бота (ARCHITECTURE.md §8).

С этапа 13 бот — фронтенд: датчиками, порогами и планировщиком владеет
служба monitor (отдельный процесс, `--service monitor`). Бот держит одно
подключение к ней (ServiceLink), получает события и рассылает их в чаты;
/status и прочие данные — через get_state по протоколу. В БД бота остаются
только его вещи: app_state, message_id для reply-цепочек, лимит форс-сканов.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramConflictError

from sa_home_bot.bot.ai_flow import RESTART_TEXT, ActiveAiChats
from sa_home_bot.bot.dispatch import TelegramEventDispatcher
from sa_home_bot.bot.invites import Gatekeeper
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
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.setup import build_bot, build_dispatcher, set_bot_commands
from sa_home_bot.bot.telegram_retry import REQUEST_TIMEOUT_S, call_with_network_retry
from sa_home_bot.bot.tool_debug import ToolCalls
from sa_home_bot.bot.torrent_pending import PendingTorrents
from sa_home_bot.bot.vpn_secrets import PendingVpnSecrets
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.node.instances import slot_key
from sa_home_bot.runtime import Runtime
from sa_home_bot.sensors.power import read_power_events_sync
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.guests import GuestStore
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)

STATE_CLEAN_SHUTDOWN = "last_shutdown_clean"


async def run(settings: Settings, *, instance: str = "") -> bool:
    """Запустить бота. True на выходе — нас вытеснил другой экземпляр того же
    бота (409 Conflict); cli.main превращает это в FENCED_EXIT_CODE.

    ``instance`` — имя инстанса (для slot_key/report_ready, см. ниже), то же,
    что уходит в ``--instance`` CLI и в аренду лидерства (node/lease.py) как
    ключ слота ``telegram-bot@<instance>``."""
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

    # 3. Подписки: владельческие из конфига + гости, впущенные инвайтами.
    book = SubscriptionBook.from_config(settings.subscriptions, settings.guest_subscriptions)

    # 4. Bot + Notifier + watchdog связи.
    bot = build_bot(settings.telegram.token)
    notifier = Notifier(bot)
    # Первый сетевой вызов Bot API на старте — после ребута сеть бывает
    # частично поднята (DNS уже резолвится, HTTP до Telegram ещё нет), см.
    # bounded retry ниже по причине живого инцидента 2026-08-12.
    bot_username = (
        await call_with_network_retry(
            lambda: bot.get_me(request_timeout=REQUEST_TIMEOUT_S),
            what="get_me() при старте бота",
        )
    ).username

    async def on_reconnect(downtime: float) -> None:
        await broadcast_system(book, notifier, render_link_restored(downtime))

    bot.session.middleware(LinkWatchMiddleware(on_reconnect))
    # Привратник: единственный способ для чужого чата что-то от бота получить
    # (bot/invites.py). Пишет гостей в реплицируемый пакет — тот же путь, что
    # у владельческих настроек, поэтому гость переживает переезд бота.
    gate = Gatekeeper(
        settings.invites, store, book, GuestStore(settings.guests_path), notifier
    )
    if not gate.enabled:
        log.info("Приглашения выключены (нет пакета инстанса или [invites].enabled=false)")
    dp = build_dispatcher(book, gate)

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
    # node_link читается из замыкания геттером ниже, а не захватывается
    # напрямую: сам callback передаётся в конструктор ServiceLink(node)
    # раньше, чем переменной присвоено значение (тот же приём, что
    # node_service в node/app.py::run_node) — к моменту первого реального
    # события (после await node_link.start()) переменная уже назначена.
    node_link: ServiceLink | None = None

    def _get_node_link() -> ServiceLink | None:
        return node_link

    # Вход/выход вызовов инструментов для дебаг-канала: короткое сообщение
    # «🔧 Alfred вызвал инструмент» разворачивается кнопкой (bot/tool_debug.
    # py). Заведён здесь, ДО node_link — тому нужен уже готовый склад, чтобы
    # кнопка «развернуть» работала и на self-scheduled remind (служба tasks,
    # см. build_node_event_handler), не только на живом /ai.
    tool_calls = ToolCalls()

    async def _report_bot_ready() -> None:
        # bot.get_me() (шаг 4 выше) уже подтвердил живую сеть/DNS/Telegram —
        # это лучшее доказательство готовности, которое у нас вообще есть, и
        # именно там падал процесс в живых инцидентах 2026-08-08 и 2026-08-12.
        # С 2026-08-12 get_me() обёрнут в bounded retry (bot/telegram_retry.
        # py) — короткие сетевые блипы сразу после ребута он переживает сам;
        # если сеть не ожила и после ретраев, процесс всё равно падает
        # намеренно, и восстановлением занимается restart-loop супервизора
        # (node/supervisor.py) вместе с фиксом гонки в node/lease.py, из-за
        # которой раньше падение синглтон-службы иногда терялось. Отчёт —
        # best-effort: сбой здесь не должен ронять бота (get_me() уже прошёл,
        # это важнее), а node_link сам вызовет этот хук заново при каждом
        # следующем переподключении к ноде (см. bot/service_link.py
        # on_connected) — ни один разрыв не остаётся неотчитанным надолго.
        try:
            await node_link.command(
                "report_ready",
                {"name": slot_key("telegram-bot", instance), "ready": True},
            )
        except Exception:
            log.warning("Не удалось сообщить ноде о готовности бота", exc_info=True)

    node_link = ServiceLink(
        settings.node.socket,
        token=settings.swarm.token,
        display_name="нода",
        on_event=build_node_event_handler(
            book,
            notifier,
            store,
            get_node_link=_get_node_link,
            tool_calls=tool_calls,
            config=settings,
        ),
        on_connected=_report_bot_ready,
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
    # tool_calls заведён раньше (см. выше, до node_link) — здесь только
    # использование дальше по функции (polling kwargs).
    # Приватные ключи VPN между QR (сразу) и файлом .conf (по кнопке) —
    # только в памяти процесса, никогда в БД (bot/vpn_secrets.py).
    pending_vpn_secrets = PendingVpnSecrets()

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
            tool_calls=tool_calls,
            pending_vpn_secrets=pending_vpn_secrets,
            runtime=runtime,
            config=settings,
            notifier=notifier,
            book=book,
            gate=gate,
            bot_username=bot_username,
            active_ai_chats=active_ai_chats,
            handle_signals=False,
        ),
        name="polling",
    )

    lifespan = Lifespan()
    lifespan.install_signal_handlers()

    fenced = False

    def _on_polling_done(task: asyncio.Task) -> None:
        """409 Conflict — не сбой, а сообщение извне: этот же токен уже
        опрашивает другой экземпляр бота. Спорить с Telegram бессмысленно и
        вредно (два поллера отбирают апдейты друг у друга), поэтому выходим
        особым кодом: нода поймёт, что перезапускать нас не надо, и отдаст
        решение аренде лидерства (node/lease.py)."""
        nonlocal fenced
        if task.cancelled():
            return
        if isinstance(task.exception(), TelegramConflictError):
            fenced = True
            log.error("Этот же бот уже запущен где-то ещё (409 Conflict) — уступаю")
            lifespan.trigger()

    polling_task.add_done_callback(_on_polling_done)
    log.info("Бот запущен (uptime-старт зафиксирован)")

    # 11. Ждать сигнала, затем остановить всё в обратном порядке.
    try:
        await lifespan.wait()
    finally:
        await _shutdown(
            dp=dp,
            polling_task=polling_task,
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
    return fenced


async def _shutdown(
    *,
    dp,
    polling_task: asyncio.Task,
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

    # Живая находка 2026-07-24 (второй заход, живой баг на проде): раньше
    # это шло ПОСЛЕ link.stop()/node_link.stop() — осиротевшая задача
    # /ai-хендлера (aiogram не ждёт её в dp.stop_polling(), см. докстринг
    # ActiveAiChats) успевала упасть на разорванном node_link и слала голое
    # "что-то пошло не так" то до RESTART_TEXT, то после, то и то, и другое
    # разом. Теперь — СНАЧАЛА известить и отменить, пока node_link/сессия
    # бота ещё живы: CancelledError не Exception, try/finally в
    # bot/handlers/ai.py::_ask_and_reply снимет регистрацию молча, минуя
    # голый "что-то пошло не так" из except Exception.
    for chat_id, task in active_ai_chats.snapshot().items():
        try:
            await notifier.send_direct(chat_id, RESTART_TEXT)
        except Exception:  # noqa: BLE001 — сбой одного уведомления не должен рвать shutdown
            log.warning("Не удалось известить chat=%s о рестарте", chat_id, exc_info=True)
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — сбой одной /ai-задачи не должен рвать shutdown
            log.warning("/ai-задача chat=%s упала при остановке", chat_id, exc_info=True)

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

    # Дослать прощание, пока сессия бота жива.
    await broadcast_system(book, notifier, render_shutdown())

    # Флаг чистого завершения — до закрытия БД.
    await store.set_state(STATE_CLEAN_SHUTDOWN, "1")

    await bot.session.close()
    await db.close()
    log.info("Останов завершён чисто")
