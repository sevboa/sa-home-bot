from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from sa_home_bot.bot import (
    commands,
    vpn_admin_view,
    vpn_callbacks,
    vpn_check_view,
    vpn_delivery,
    vpn_faq,
    vpn_legacy_card,
    vpn_nodes,
    vpn_proxy_screen,
    vpn_report,
    vpn_rights,
    vpn_screens,
    vpn_settings,
    vpn_wizard,
)
from sa_home_bot.bot.handlers.vpn import admin, delivery, manage, reports, wizard
from sa_home_bot.bot.handlers.vpn.context import (
    SERVICE_UNAVAILABLE_NOTICE,
    Ctx,
    live_servers_for,
)
from sa_home_bot.bot.handlers.vpn.screens import (
    redraw_card,
    show_screen,
)
from sa_home_bot.bot.invites import Gatekeeper
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.vpn_apk import deliver_apk
from sa_home_bot.bot.vpn_secrets import PendingVpnSecrets
from sa_home_bot.config import Settings
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

_cb = vpn_callbacks
_EXPERT = _cb.EXPERT_PREFIX

Handler = Callable[[Ctx, str | None, str | None], Awaitable[None]]


def _is_expert(value: str | None, kind: str = "") -> bool:
    return bool(value) and value.startswith(f"{_EXPERT}{kind}")


async def _apk(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    callback = ctx.callback
    if not ctx.is_private:
        await ctx.alert("Напишите мне в личку — там и пришлю.")
        return
    if value == vpn_settings.APK_NO_STORE:
        await callback.answer()
        await callback.message.answer(
            vpn_settings.NO_STORE_TEXT, reply_markup=vpn_settings.no_store_keyboard()
        )
        return
    if value == vpn_settings.APK_SEND:
        dst = await ctx.need_dst(node_id)
        if dst is None:
            return
        await callback.answer("Отправляю…")
        text = await deliver_apk(
            ctx.node_link, ctx.notifier, ctx.chat_id, dst, message_thread_id=ctx.thread_id
        )
        await callback.message.answer(text)
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
        return
    await callback.answer()
    code = vpn_faq.parse_question(value)
    if code is None:
        text = vpn_faq.LIST_TEXT
        keyboard = vpn_faq.list_keyboard(
            home_cb=_cb.home_cb(), report=vpn_report.can_report(ctx.subscription)
        )
    else:
        text = vpn_faq.answer_text(code)
        keyboard = vpn_faq.answer_keyboard(
            code,
            ctx.config.vpn,
            can_wizard=vpn_rights.allows(ctx.subscription, vpn_protocol.ACTION_ISSUE),
        )
    await ctx.edit(text, keyboard, disable_web_page_preview=True)


async def _grant_extra(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    dst = await ctx.need_dst(node_id)
    if dst is None:
        return
    try:
        await ctx.node_link.command(
            vpn_protocol.ACTION_GRANT_EXTRA, {"chat_id": ctx.chat_id}, dst=dst
        )
    except ProtoError as exc:
        if exc.code == vpn_protocol.ERR_QUOTA_CEILING:
            result = await ctx.node_link.command(
                vpn_protocol.ACTION_REQUEST_EXTRA, {"chat_id": ctx.chat_id}, dst=dst
            )
            await ctx.callback.answer()
            await ctx.callback.message.answer(
                f"✋ Потолок самообслуживания достигнут — заявка №{result['request_id']} "
                "отправлена админу."
            )
            return
        await ctx.alert(f"⚠️ {exc.message}")
        return
    except ServiceUnavailableError:
        await ctx.alert(SERVICE_UNAVAILABLE_NOTICE)
        return
    await ctx.callback.answer("Добавлено")
    await redraw_card(ctx)


async def _expert_issue(ctx: Ctx, value: str, node_id: str | None) -> None:
    kind, rest = value[1:2], value[2:]
    if kind == _cb.KIND_VLESS:
        await delivery.deliver_vless(ctx, rest)
    elif kind == _cb.KIND_FILE:
        await delivery.vless_file_again(ctx, rest)
    elif kind == _cb.KIND_AWG_COUNTRY:
        await delivery.awg_country(ctx, rest, node_id)
    elif kind == _cb.KIND_NEW:
        await wizard.create_device(ctx, rest[:1], expert=True)
    else:
        await ctx.callback.answer()


async def _pick_server_or_transport(ctx: Ctx, node_id: str | None) -> tuple[bool, str | None]:
    live = await live_servers_for(ctx.node_link, ctx.chat_id)
    if len(live) > 1:
        await ctx.callback.answer()
        await ctx.edit(vpn_screens.SERVER_PICKER_TEXT, vpn_screens.server_picker_keyboard(live))
        return True, node_id
    if len(live) == 1:
        node_id = live[0].get("node") or node_id
    return False, node_id


async def _pick_transport(ctx: Ctx, dst) -> bool:
    node_transports: list[str] = []
    transport_icons: dict[str, str] = {}
    with contextlib.suppress(ServiceUnavailableError, ProtoError):
        state = await ctx.node_link.get_state(dst=dst)
        node_transports = state.get("transports") or []
        transport_icons = vpn_check_view.transport_icons(state)
    if len(node_transports) <= 1:
        return False
    await ctx.callback.answer()
    await ctx.edit(
        vpn_screens.TRANSPORT_PICKER_TEXT,
        vpn_screens.transport_picker_keyboard(node_transports, dst.node, transport_icons),
    )
    return True


async def _issue_or_reissue(
    ctx: Ctx, action_id: str, value: str | None, node_id: str | None
) -> None:
    if not await ctx.require_private():
        return
    if value and value.startswith(_cb.SECRET_TOKEN_PREFIX):
        await vpn_delivery.reveal_secret(
            ctx.callback,
            ctx.notifier,
            ctx.pending,
            value[len(_cb.SECRET_TOKEN_PREFIX) :],
            ctx.ttl_s,
        )
        return

    chosen_transport: str | None = None
    if value and value.startswith(_cb.TRANSPORT_PICK_PREFIX):
        chosen_transport = value[len(_cb.TRANSPORT_PICK_PREFIX) :]
        value = None

    if action_id == vpn_protocol.ACTION_REISSUE and not value:
        await ctx.callback.answer()
        return

    is_bare_issue = (
        action_id == vpn_protocol.ACTION_ISSUE and chosen_transport is None and not value
    )
    if is_bare_issue and not node_id:
        handled, node_id = await _pick_server_or_transport(ctx, node_id)
        if handled:
            return

    dst = await ctx.need_dst(node_id)
    if dst is None:
        return

    if is_bare_issue and await _pick_transport(ctx, dst):
        return

    await ctx.callback.answer("Выпускаю…")
    payload: dict[str, object] = {"chat_id": ctx.chat_id}
    if value:
        payload["device_label"] = value
    if chosen_transport:
        payload["transport"] = chosen_transport
    result = await ctx.call_or_notice(action_id, payload, dst)
    if result is None:
        return
    await vpn_delivery.send_secret(
        ctx.notifier,
        ctx.pending,
        ctx.chat_id,
        action_id,
        result,
        ctx.ttl_s,
        message_thread_id=ctx.thread_id,
    )
    await redraw_card(ctx)


async def _issue(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    if _is_expert(value, vpn_wizard.PREFIX):
        await wizard.create_device(ctx, value[2:3])
    elif _is_expert(value):
        await _expert_issue(ctx, value, node_id)
    else:
        await _issue_or_reissue(ctx, vpn_protocol.ACTION_ISSUE, value, node_id)


async def _reissue(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    if _is_expert(value, _cb.KIND_MULTI):
        await manage.expert_reissue_multi(ctx, value[2:])
    elif _is_expert(value, _cb.KIND_AWG_COUNTRY):
        await delivery.awg_replace(ctx, value, node_id)
    elif _is_expert(value):
        await manage.expert_reissue(ctx, value, node_id)
    else:
        await _issue_or_reissue(ctx, vpn_protocol.ACTION_REISSUE, value, node_id)


async def _revoke(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    if _is_expert(value):
        await manage.expert_delete(ctx, value)
        return
    if not value:
        await ctx.callback.answer()
        return
    dst = await ctx.need_dst(node_id)
    if dst is None:
        return
    result = await ctx.call_or_alert(
        vpn_protocol.ACTION_REVOKE, {"chat_id": ctx.chat_id, "device_label": value}, dst
    )
    if result is None:
        return
    await ctx.callback.answer(f"Отозвано: «{value}»")
    await redraw_card(ctx)


async def _usage_all(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    dst = await ctx.need_dst(node_id)
    if dst is None:
        return
    summary = await ctx.call_or_alert(vpn_protocol.ACTION_USAGE, {}, dst)
    if summary is None:
        return
    await ctx.callback.answer()
    await ctx.callback.message.answer(vpn_legacy_card.summary_text(summary))


async def _peers(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    await admin.handle_admin_screen(ctx, value, node_id)


async def _set_access(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    await admin.handle_set_access(ctx, value, node_id)


async def _set_rights(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    await admin.handle_set_rights(ctx, value)


async def _report(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    await reports.handle_report(ctx, value)


async def _reply(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    await reports.handle_owner_reply_button(ctx, value)


async def _vpn_card(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    if not value:
        await ctx.callback.answer()
        await redraw_card(ctx)
    elif value.startswith(vpn_wizard.PREFIX):
        await wizard.show(ctx, value)
    elif value[0] == _cb.SCREEN_LINK:
        await delivery.send_settings_link(ctx, value[1:])
    elif value[0] == _cb.SCREEN_RESTORE:
        await manage.show_restore_select(ctx, value[1:])
    elif value[0] == _cb.SCREEN_REISSUE:
        await manage.show_reissue_select(ctx, value[1:], answered=False)
    else:
        await show_screen(ctx, value[0], value[1:], answered=False)


async def _check_status(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    dst = await ctx.need_dst(node_id)
    if dst is None:
        return
    result = await ctx.call_or_alert(vpn_protocol.ACTION_CHECK_STATUS, {}, dst)
    if result is None:
        return
    await ctx.callback.answer()
    await ctx.edit(
        vpn_check_view.status_text(result.get("states") or [], rollup=result.get("rollup") or []),
        vpn_check_view.status_keyboard(),
    )


async def _check_now(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    dst = await ctx.need_dst(node_id)
    if dst is None:
        return
    result = await ctx.call_or_alert(vpn_protocol.ACTION_CHECK_NOW, {}, dst)
    if result is None:
        return
    dispatched = result.get("dispatched_to") or []
    await ctx.callback.answer(
        f"Запущено на: {', '.join(dispatched)}" if dispatched else "Разослать не удалось",
        show_alert=not dispatched,
    )
    states: list[dict] = []
    rollup: list[dict] = []
    with contextlib.suppress(ProtoError, ServiceUnavailableError):
        status = await ctx.node_link.command(vpn_protocol.ACTION_CHECK_STATUS, {}, dst=dst)
        states = status.get("states") or []
        rollup = status.get("rollup") or []
    await ctx.edit(
        vpn_check_view.status_text(states, rollup=rollup, pending=True),
        vpn_check_view.status_keyboard(),
    )


async def _proxy_qr(ctx: Ctx, node_id: str | None) -> None:
    dst = await ctx.need_dst(node_id)
    if dst is None:
        return
    result = await ctx.call_or_alert(vpn_protocol.ACTION_PROXY_LINK, {}, dst)
    if result is None:
        return
    qr_b64 = result.get("qr_png_b64")
    if not qr_b64:
        await ctx.alert("⚠️ QR сейчас недоступен.")
        return
    await ctx.callback.answer()
    country = vpn_proxy_screen.country_of(result)
    await vpn_delivery.send_qr(
        ctx.notifier,
        ctx.chat_id,
        qr_b64,
        caption=f"✈️ QR прокси Telegram {country.short}\nОтсканируйте в Telegram.",
        thread_id=ctx.thread_id,
        filename="proxy-qr.png",
    )


async def _proxy_link(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    can_rotate = vpn_rights.allows(ctx.subscription, vpn_protocol.ACTION_PROXY_ROTATE_SECRET)
    if value == vpn_proxy_screen.VALUE_QR:
        await _proxy_qr(ctx, node_id)
        return
    results = await vpn_nodes.fanout(ctx.node_link, vpn_protocol.ACTION_PROXY_LINK, {})
    if not results:
        await ctx.alert("⚠️ Прокси Telegram сейчас не настроен ни на одном сервере.")
        return
    if value == vpn_proxy_screen.VALUE_SECRET:
        if not can_rotate:
            await ctx.alert("⛔️ Недоступно.")
            return
        await ctx.callback.answer()
        for result in results:
            await ctx.callback.message.answer(
                vpn_proxy_screen.proxy_text(result, admin=True),
                reply_markup=vpn_proxy_screen.rotate_keyboard(result.get("node"), can_rotate=True),
            )
        return
    await ctx.callback.answer()
    await ctx.edit(
        vpn_proxy_screen.TEXT,
        vpn_proxy_screen.keyboard(results, can_secret=can_rotate, home_cb=_cb.home_cb()),
    )


async def _proxy_rotate_secret(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    dst = await ctx.need_dst(node_id)
    if dst is None:
        return
    result = await ctx.call_or_alert(vpn_protocol.ACTION_PROXY_ROTATE_SECRET, {}, dst)
    if result is None:
        return
    await ctx.callback.answer("Секрет сменён")
    await ctx.callback.message.answer(
        "🔁 Старая ссылка перестала работать.\n\n"
        + vpn_proxy_screen.proxy_text(result, admin=True),
        reply_markup=vpn_proxy_screen.rotate_keyboard(result.get("node")),
    )


async def _resolve_request(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    if not value or "_" not in value:
        await ctx.callback.answer()
        return
    raw_id, _, decision = value.partition("_")
    approve = decision == "approve"
    dst = await ctx.need_dst(node_id)
    if dst is None:
        return
    try:
        result = await ctx.node_link.command(
            vpn_protocol.ACTION_RESOLVE_REQUEST,
            {"request_id": int(raw_id), "approve": approve},
            dst=dst,
        )
    except (ProtoError, ServiceUnavailableError, ValueError) as exc:
        await ctx.alert(f"⚠️ {exc}")
        return
    await ctx.callback.answer("Одобрено" if approve else "Отклонено")
    await ctx.callback.message.answer(
        f"Заявка №{result['request_id']} — {'✅ одобрена' if approve else '🚫 отклонена'}."
    )


HANDLERS: dict[str, Handler] = {
    vpn_report.REPORT_ACTION: _report,
    vpn_report.REPLY_ACTION: _reply,
    _cb.ACTION_APK: _apk,
    vpn_protocol.ACTION_GRANT_EXTRA: _grant_extra,
    vpn_protocol.ACTION_REISSUE: _reissue,
    vpn_protocol.ACTION_ISSUE: _issue,
    vpn_protocol.ACTION_REVOKE: _revoke,
    _cb.ACTION_USAGE_ALL: _usage_all,
    vpn_protocol.ACTION_PEERS: _peers,
    vpn_protocol.ACTION_SET_ACCESS: _set_access,
    vpn_admin_view.ACTION_SET_RIGHTS: _set_rights,
    _cb.ACTION_VPN_CARD: _vpn_card,
    vpn_protocol.ACTION_CHECK_STATUS: _check_status,
    vpn_protocol.ACTION_CHECK_NOW: _check_now,
    vpn_protocol.ACTION_PROXY_LINK: _proxy_link,
    vpn_protocol.ACTION_PROXY_ROTATE_SECRET: _proxy_rotate_secret,
    vpn_protocol.ACTION_RESOLVE_REQUEST: _resolve_request,
}


async def handle_action(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    subscription: Subscription,
    pending_vpn_secrets: PendingVpnSecrets,
    book: SubscriptionBook | None = None,
    gate: Gatekeeper | None = None,
    bot: Bot | None = None,
    store=None,
) -> None:
    parsed = commands.parse_action_callback(callback.data)
    if parsed is None or callback.message is None:
        await callback.answer()
        return
    _service, action_id, value, node_id = parsed
    handler = HANDLERS.get(action_id)
    if handler is None:
        await callback.answer()
        return
    ctx = Ctx(
        callback=callback,
        node_link=node_link,
        notifier=notifier,
        config=config,
        subscription=subscription,
        pending=pending_vpn_secrets,
        book=book,
        gate=gate,
        bot=bot,
        store=store,
    )
    await handler(ctx, value, node_id)
