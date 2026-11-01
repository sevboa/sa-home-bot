from __future__ import annotations

import asyncio
import contextlib
import logging
import socket

from aiogram.exceptions import TelegramConflictError

from sa_home_bot.bot.ai_flow import ActiveAiChats
from sa_home_bot.bot.ai_lines import RESTART_TEXT
from sa_home_bot.bot.away import AwayService
from sa_home_bot.bot.away_return import AwayReturn, AwayRunner
from sa_home_bot.bot.devlab_notify import DevlabNotifier
from sa_home_bot.bot.dispatch import TelegramEventDispatcher
from sa_home_bot.bot.egress import EgressManager, make_fetch_candidates
from sa_home_bot.bot.egress_app import (
    EGRESS_STATE_PATH,
    OutboxFlusher,
    SwitchNotices,
    pick_startup_route,
    startup_routes,
)
from sa_home_bot.bot.interactives.engine import Interactives
from sa_home_bot.bot.invites import Gatekeeper
from sa_home_bot.bot.lifecycle import (
    broadcast_system,
    render_link_restored,
    render_shutdown,
    render_startup,
)
from sa_home_bot.bot.link_watch import LinkWatchMiddleware
from sa_home_bot.bot.menu import set_bot_commands
from sa_home_bot.bot.monitor_events import build_event_handler
from sa_home_bot.bot.node_events import build_node_event_handler
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.pending_actions import PendingActions
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.setup import build_bot, build_dispatcher
from sa_home_bot.bot.telegram_retry import REQUEST_TIMEOUT_S, call_with_network_retry
from sa_home_bot.bot.tool_debug import ToolCalls
from sa_home_bot.bot.torrent_pending import PendingTorrents
from sa_home_bot.bot.vpn_secrets import PendingVpnSecrets
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.node.events import ACTION_LLM_SET_NIGHT_MODE
from sa_home_bot.node.instances import slot_key
from sa_home_bot.people import seed as people_seed
from sa_home_bot.proto.addresses import LLM_ADDRESS
from sa_home_bot.runtime import Runtime
from sa_home_bot.sensors.power import read_power_events_sync
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.guests import GuestStore
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)

STATE_CLEAN_SHUTDOWN = "last_shutdown_clean"


async def run(settings: Settings, *, instance: str = "") -> bool:
    runtime = Runtime()

    db = Database(settings.database.path)
    await db.open()
    await apply_migrations(db)
    store = Store(db)

    prev = await store.get_state(STATE_CLEAN_SHUTDOWN)
    started_clean = prev in (None, "1")
    await store.set_state(STATE_CLEAN_SHUTDOWN, "0")

    book = SubscriptionBook.from_config(settings.subscriptions, settings.guest_subscriptions)
    try:
        await people_seed.seed_local(store, settings, book)
    except Exception:
        log.exception("Карточки людей: перенос не удался, повтор при следующем старте")

    bot = build_bot(settings.telegram.token, settings.telegram.proxy)
    notifier = Notifier(bot, outbox=store)
    auto_egress = settings.telegram.proxy_mode == "auto"
    if auto_egress:
        await pick_startup_route(bot, startup_routes(settings.telegram))
    bot_username = (
        await call_with_network_retry(
            lambda: bot.get_me(request_timeout=REQUEST_TIMEOUT_S),
            what="get_me() при старте бота",
        )
    ).username

    async def on_reconnect(downtime: float) -> None:
        await broadcast_system(book, notifier, render_link_restored(downtime))

    bot.session.middleware(LinkWatchMiddleware(on_reconnect))
    gate = Gatekeeper(
        settings.invites, store, book, GuestStore(settings.guests_path), notifier
    )
    if not gate.enabled:
        log.info("Приглашения выключены (нет пакета инстанса или [invites].enabled=false)")
    dp = build_dispatcher(book, gate, store)

    await book.validate_on_startup(bot)

    await set_bot_commands(bot, book)

    last_outage = None
    if not started_clean:
        loop = asyncio.get_running_loop()
        events, _ = await loop.run_in_executor(None, read_power_events_sync, 0, 1)
        if events:
            last_outage = events[0]
    await broadcast_system(
        book, notifier, render_startup(clean=started_clean, last_outage=last_outage)
    )

    dispatcher = TelegramEventDispatcher(notifier, book, store)
    link = ServiceLink(
        settings.monitor.socket,
        token=settings.swarm.token,
        display_name="монитор",
        on_event=build_event_handler(dispatcher),
    )
    await link.start()
    node_link: ServiceLink | None = None

    def _get_node_link() -> ServiceLink | None:
        return node_link

    tool_calls = ToolCalls()

    pending_actions = PendingActions(store, notifier, settings, _get_node_link)
    interactives = Interactives(store, notifier, settings, _get_node_link)

    async def _report_bot_ready() -> None:
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
            pending_actions=pending_actions,
        ),
        on_connected=_report_bot_ready,
    )
    await node_link.start()

    outbox_flusher = OutboxFlusher(notifier, store)
    outbox_flusher.start()
    egress: EgressManager | None = None
    if auto_egress:
        observer = settings.node.id or socket.gethostname()
        egress = EgressManager(
            bot,
            settings.telegram,
            make_fetch_candidates(node_link, observer),
            observer,
            state_path=EGRESS_STATE_PATH,
        )
        egress.on_switched = SwitchNotices(book, notifier).on_switched
        egress.on_recovered = outbox_flusher.kick
        egress.start()
    try:
        await pending_actions.recover()
    except Exception:
        log.exception("pending_actions: восстановление на старте не удалось")
    try:
        await interactives.recover()
    except Exception:
        log.exception("interactives: восстановление на старте не удалось")

    async def refresh_menu() -> None:
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
    pending_vpn_secrets = PendingVpnSecrets()

    active_ai_chats = ActiveAiChats()

    async def _send_night_mode(night: bool) -> None:
        link_to_node = _get_node_link()
        if link_to_node is None:
            raise ServiceUnavailableError("нет связи с нодой")
        await link_to_node.command(
            ACTION_LLM_SET_NIGHT_MODE, {"night": night}, dst=LLM_ADDRESS, timeout=10.0
        )

    away = AwayService(store, settings)
    away_runner = AwayRunner(
        away,
        AwayReturn(
            away,
            get_node_link=_get_node_link,
            store=store,
            config=settings,
            book=book,
            notifier=notifier,
            active_ai_chats=active_ai_chats,
            tool_calls=tool_calls,
            pending_actions=pending_actions,
            interactives=interactives,
        ),
        book,
        notifier,
        night_sink=_send_night_mode,
    )
    away_task = asyncio.create_task(away_runner.run(), name="away")
    devlab_notify_task: asyncio.Task | None = None
    devlab_notifier: DevlabNotifier | None = None
    if settings.devlab_notify.enabled:
        devlab_notifier = DevlabNotifier(
            get_node_link=_get_node_link,
            store=store,
            config=settings,
            book=book,
            notifier=notifier,
        )
        devlab_notify_task = asyncio.create_task(devlab_notifier.run(), name="devlab-notify")
    people_seed_task = asyncio.create_task(
        people_seed.graph_seed_loop(store, book, node_link), name="people-seed"
    )

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
            pending_actions=pending_actions,
            interactives=interactives,
            pending_vpn_secrets=pending_vpn_secrets,
            runtime=runtime,
            config=settings,
            notifier=notifier,
            book=book,
            gate=gate,
            bot_username=bot_username,
            active_ai_chats=active_ai_chats,
            away=away,
            away_runner=away_runner,
            devlab_notifier=devlab_notifier,
            handle_signals=False,
        ),
        name="polling",
    )

    lifespan = Lifespan()
    lifespan.install_signal_handlers()

    fenced = False

    def _on_polling_done(task: asyncio.Task) -> None:
        nonlocal fenced
        if task.cancelled():
            return
        if isinstance(task.exception(), TelegramConflictError):
            fenced = True
            log.error("Этот же бот уже запущен где-то ещё (409 Conflict) — уступаю")
            lifespan.trigger()

    polling_task.add_done_callback(_on_polling_done)
    log.info("Бот запущен (uptime-старт зафиксирован)")

    try:
        await lifespan.wait()
    finally:
        await _shutdown(
            dp=dp,
            polling_task=polling_task,
            active_ai_chats=active_ai_chats,
            away_task=away_task,
            devlab_notify_task=devlab_notify_task,
            people_seed_task=people_seed_task,
            egress=egress,
            outbox_flusher=outbox_flusher,
            pending_actions=pending_actions,
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
    pending_actions: PendingActions,
    link: ServiceLink,
    node_link: ServiceLink,
    apps_link: ServiceLink,
    torrents_link: ServiceLink,
    book: SubscriptionBook,
    notifier: Notifier,
    store: Store,
    bot,
    db: Database,
    away_task: asyncio.Task | None = None,
    devlab_notify_task: asyncio.Task | None = None,
    people_seed_task: asyncio.Task | None = None,
    egress: EgressManager | None = None,
    outbox_flusher: OutboxFlusher | None = None,
) -> None:
    log.info("Останов приложения...")

    for chat_id, task in active_ai_chats.snapshot().items():
        try:
            await notifier.send_direct(chat_id, RESTART_TEXT)
        except Exception:
            log.warning("Не удалось известить chat=%s о рестарте", chat_id, exc_info=True)
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.warning("/ai-задача chat=%s упала при остановке", chat_id, exc_info=True)

    if egress is not None:
        await egress.stop()
    if outbox_flusher is not None:
        await outbox_flusher.stop()
    if away_task is not None:
        if not away_task.done():
            away_task.cancel()
        try:
            await away_task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.warning("away-задача упала при остановке", exc_info=True)

    if devlab_notify_task is not None:
        if not devlab_notify_task.done():
            devlab_notify_task.cancel()
        try:
            await devlab_notify_task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.warning("devlab-notify-задача упала при остановке", exc_info=True)

    if people_seed_task is not None and not people_seed_task.done():
        people_seed_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await people_seed_task

    await pending_actions.aclose()

    await link.stop()
    await node_link.stop()
    await apps_link.stop()
    await torrents_link.stop()

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
    except Exception:
        log.warning("polling завершился с ошибкой", exc_info=True)

    await broadcast_system(book, notifier, render_shutdown())

    await store.set_state(STATE_CLEAN_SHUTDOWN, "1")

    await bot.session.close()
    await db.close()
    log.info("Останов завершён чисто")
