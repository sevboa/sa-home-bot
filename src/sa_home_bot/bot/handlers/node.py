"""/swarm (алиас /nodes) — сводка роя; обработка динамических действий «act:…».

Права на callback уже проверены CallbackAuthorizationMiddleware
(`действие@служба`). Здесь только маршрутизация к нужному линку и рендер.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from sa_home_bot.bot import actions, apps_view, commands, node_view, swarm_view
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

router = Router(name="node")

# Имя собственной службы бота в назначениях ноды (см. supervisor.ASSIGNMENT_ARGS).
SELF_SERVICE_NAME = "telegram-bot"

SELF_RESTART_TEXT = "🔄 Перезапускаюсь, вернусь через минуту."
SELF_STOP_TEXT = (
    "⏹ Останавливаюсь. Запустить меня снова можно только с ноды: "
    "<code>nodectl start telegram-bot</code>."
)

# Ключ app_state с id последнего callback'а само-рестарта — защита от
# переигрыша: Telegram мог не подтвердить offset до смерти процесса, и после
# рестарта тот же callback приедет снова (иначе бот перезапускал бы себя в цикле).
SELF_RESTART_CB_KEY = "self_restart_cb"


def _is_self_shutdown(action_id: str, value: str | None, node_id: str | None) -> bool:
    """Действие, убивающее сам процесс бота: stop/restart своей службы
    telegram-bot или само-рестарт своей ноды (нода перезапускает и детей)."""
    if node_id is not None:
        return False  # чужой telegram-bot — обычная ветка
    if action_id == "restart_node":
        return True
    return action_id in ("stop", "restart") and value == SELF_SERVICE_NAME


async def _handle_self_shutdown(
    callback: CallbackQuery, store: Store, node_link: ServiceLink, action_id: str, value: str | None
) -> None:
    """Спец-кейс: бот просит ноду убить себя же.

    Ответ ноды не дождаться (нода ждёт нашей смерти) — прощаемся ДО команды,
    ошибки рвущегося линка глотаем, карточку не перерисовываем. После подъёма
    штатное «Сторож снова на посту» шлёт lifecycle.
    """
    if await store.get_state(SELF_RESTART_CB_KEY) == callback.id:
        log.warning("Повторный callback само-рестарта %s — игнорирую", callback.id)
        await callback.answer()
        return
    await store.set_state(SELF_RESTART_CB_KEY, callback.id)

    await callback.answer()
    await callback.message.answer(
        SELF_STOP_TEXT if action_id == "stop" else SELF_RESTART_TEXT
    )
    with contextlib.suppress(ServiceUnavailableError, ProtoError, TimeoutError):
        args = {"name": value} if value else {}
        await node_link.command(action_id, args)


@router.message(Command(commands.SWARM.name, commands.NODES.name))
async def cmd_swarm(
    message: Message,
    node_link: ServiceLink,
    config: Settings,
    subscription: Subscription | None = None,
) -> None:
    text, keyboard = await swarm_view.build_swarm_view(node_link, subscription, config.wake)
    await message.answer(text, reply_markup=keyboard)


async def _run_node_action(
    node_link: ServiceLink, action_id: str, value: str | None, node_id: str | None
) -> tuple[str | None, dict | None]:
    """Выполнить действие ноды (свою или пира); (текст ошибки, ответ команды).

    Ответ нужен действиям без побочного эффекта на карточке (`check_update`
    ничего не меняет в get_state() — редрайв карточки не покажет результат,
    его надо явно отрисовать вызывающему)."""
    dst = Address(node=node_id, service=node_view.NODE_SERVICE) if node_id else None
    action = await actions.find_action(node_link, action_id, dst=dst)
    if action is None:
        return "Действие недоступно.", None
    try:
        result = await node_link.command(action.id, actions.build_args(action, value), dst=dst)
    except ServiceUnavailableError:
        return (
            f"⚠️ Нода «{node_id}» недоступна (нет связи или она спит)."
            if node_id
            else node_view.NODE_DOWN_TEXT
        ), None
    except ProtoError as exc:
        return f"⚠️ Ошибка: {exc.message}", None
    return None, result


def _format_check_update(result: dict) -> str:
    running = result.get("running", "?")
    installed = result.get("installed") or "?"
    latest = str(result.get("latest", "?")).lstrip("v")
    if latest not in ("?", installed):
        return (
            f"⬆️ Доступно обновление: v{latest} (сейчас установлено v{installed}, "
            f"запущено v{running}) — нажмите «Обновить»"
        )
    return f"✅ Обновлений нет — установлена последняя версия (v{installed})"


@router.callback_query(F.data.startswith(f"{commands.ACTION_CALLBACK_PREFIX}:"))
async def on_dynamic_action(
    callback: CallbackQuery,
    store: Store,
    node_link: ServiceLink,
    apps_link: ServiceLink,
    config: Settings,
    subscription: Subscription | None = None,
) -> None:
    parsed = commands.parse_action_callback(callback.data)
    if parsed is None or callback.message is None:
        await callback.answer()
        return
    service, action_id, value, node_id = parsed

    if service == node_view.NODE_SERVICE and _is_self_shutdown(action_id, value, node_id):
        await _handle_self_shutdown(callback, store, node_link, action_id, value)
        return

    if service == node_view.NODE_SERVICE:
        error, result = await _run_node_action(node_link, action_id, value, node_id)
        if error is not None:
            await callback.message.answer(error)
        elif action_id == "check_update":
            # Без побочного эффекта на get_state() — карточку перерисовывать
            # незачем, результат некому больше показать, кроме как тут.
            await callback.message.answer(_format_check_update(result or {}))
        elif value is not None and action_id not in ("assign", "unassign"):
            # Действие над службой (своей или пира) — перерисовать её карточку.
            text, keyboard = await node_view.build_service_card_view(
                node_link, subscription, value, node_id
            )
            with contextlib.suppress(TelegramBadRequest):
                await callback.message.edit_text(text, reply_markup=keyboard)
        else:
            # Питание/назначение — перерисовать карточку самой ноды.
            text, keyboard = await node_view.build_node_card_view(
                node_link, subscription, node_id
            )
            with contextlib.suppress(TelegramBadRequest):
                await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer("Готово" if error is None else None)
        return

    if service == apps_view.APPS_SERVICE:
        # Кнопки act:apps из старых сообщений — тот же скилл, что команда.
        text, keyboard = await apps_view.run_app_skill(apps_link, subscription, action_id, value)
        await callback.message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)
        await callback.answer()
        return

    if service == "monitor":
        text = await actions.run_action(
            store, node_link, service, action_id, value, node_id=node_id
        )
        await callback.message.answer(text)
        await callback.answer()
        return

    log.warning("Callback для неизвестной службы: %s", callback.data)
    await callback.answer("Неизвестная служба", show_alert=True)
