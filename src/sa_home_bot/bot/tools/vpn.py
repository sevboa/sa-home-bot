from __future__ import annotations

import json
from typing import Any

from sa_home_bot.bot import (
    recipients,
    vpn_facts,
    vpn_nodes,
)
from sa_home_bot.bot.tools.core import (
    NO_SWARM_LINK,
    ActionRight,
    ToolContext,
    ToolSpec,
    VariantRights,
    call_service,
    people_of,
    text_arg,
)
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol

_VPN_ACTION_SETTINGS = "settings"
_VPN_ACTION_REPORT = "report"
_VPN_NO_PRIVATE = "недоступно: настройки доступа отдаю только в личном чате, не в группе"


async def _vpn_recipient(ctx: ToolContext, who: str) -> tuple[int | None, str]:
    is_admin = ctx.subscription is not None and ActionRight(
        vpn_protocol.ACTION_PEERS, _VPN_SERVICE
    ).granted(ctx.subscription)
    if not is_admin:
        return None, (
            "недоступно: подключать другого человека может только админ — пусть он "
            "попросит меня об этом сам, в своём чате"
        )
    if ctx.book is None:
        return None, "недоступно: сейчас не могу искать получателей по имени"
    found = recipients.find_recipients(who, ctx.book, people_of(ctx))
    if not found:
        return None, (
            f"не получилось: «{who}» я не знаю — подключать могу только тех, "
            "кто уже говорит со мной в личном чате"
        )
    if len(found) > 1:
        names = ", ".join(f"{r.display} ({r.chat_id})" for r in found)
        return None, f"уточни, кому именно: под «{who}» подходят {names}"
    return found[0].chat_id, ""


async def _vpn_person_action(ctx: ToolContext, action: str, args: dict[str, Any]) -> str:
    from sa_home_bot.bot import vpn_agent

    if ctx.chat_id is None or ctx.chat_id == vpn_protocol.PROBE_CHAT_ID:
        return "недоступно: VPN привязан к человеку, а сейчас его нет"
    chat_id = ctx.chat_id
    thread_id = ctx.message_thread_id
    private = chat_id > 0

    if action == vpn_protocol.ACTION_USAGE:
        servers, _answered = await vpn_agent.servers_for(ctx.node_link, chat_id)
        return vpn_agent.status_text(servers)

    if action == vpn_protocol.ACTION_ISSUE:
        who = text_arg(args, "recipient")
        target, thread = chat_id, thread_id
        target_name = ""
        if who:
            target, refusal = await _vpn_recipient(ctx, who)
            if target is None:
                return refusal
            thread = None
            target_name = f" {who}"
        elif not private:
            return _VPN_NO_PRIVATE
        if ctx.notifier is None:
            return "недоступно: сейчас не могу отправить сообщение"
        servers, _answered = await vpn_agent.servers_for(ctx.node_link, target)
        if not vpn_agent.wizard_available(servers):
            return "недоступно: этому человеку сейчас нечего подключать — VPN ему не открыт"
        sent = await vpn_agent.send_wizard(ctx.notifier, target, thread)
        if sent is None:
            return "не вышло: сообщение не доставилось"
        return (
            f"готово: человеку{target_name} ушло сообщение с кнопкой «📱 Подключить по шагам» "
            "— дальше он проходит настройку кнопками (ключей и ссылок я не выдаю). "
            "Скажи коротко, что нужно нажать эту кнопку."
        )

    if action == _VPN_ACTION_SETTINGS:
        if not private:
            return _VPN_NO_PRIVATE
        if ctx.notifier is None:
            return "недоступно: сейчас не могу отправить сообщение"
        servers, _answered = await vpn_agent.servers_for(ctx.node_link, chat_id)
        label = str(args.get("device_label") or "")
        device = vpn_agent.find_device(servers, label)
        if device is None:
            names = ", ".join(f"«{d.label}»" for d in vpn_agent.vd.build_devices(servers))
            what = f"не нашёл устройство «{label}»" if label else "уточни, какое устройство"
            return what + (f" — есть: {names}" if names else " — устройств нет")
        sent = await vpn_agent.send_settings(ctx.notifier, chat_id, thread_id, device)
        if sent is None:
            return "не вышло: сообщение не доставилось"
        return (
            f"готово: экран «📥 Получить настройки» для «{device.label}» ушёл личным "
            "сообщением — человек выбирает способ кнопками (ключей и ссылок я не показываю)."
        )

    reason = text_arg(args, "reason")
    if not reason:
        return "ошибка: не указана причина (reason) — что случилось, словами человека"
    if ctx.notifier is None or ctx.book is None:
        return "недоступно: сейчас не могу передать сообщение владельцу"
    servers, answered = await vpn_agent.servers_for(ctx.node_link, chat_id)
    device = (
        vpn_agent.find_device(servers, str(args.get("device_label") or ""))
        if args.get("device_label")
        else None
    )
    name = ctx.author or str(chat_id)
    username = ctx.people.username(chat_id) if ctx.people is not None else None
    outcome = await vpn_agent.submit_report(
        ctx.node_link,
        ctx.notifier,
        ctx.book,
        chat_id=chat_id,
        name=name,
        username=username or None,
        reason=reason,
        servers=servers,
        answered=answered,
        device_key=device.key if device is not None else "",
    )
    if outcome == "sent":
        return "готово: владельцу передано, ответ придёт человеку сюда же"
    if outcome == "throttled":
        return "уже передавали меньше 10 минут назад — не дублирую, ждать ответа владельца"
    return "сейчас некому передать — пусть человек напишет владельцу напрямую"


async def tool_vpn(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.node_link is None:
        return NO_SWARM_LINK
    action = text_arg(args, "action")
    refusal = _VPN_VARIANTS.refusal_for(ctx.subscription, action)
    if refusal is not None:
        return refusal
    dst = await vpn_nodes.resolve_vpn_dst(
        ctx.node_link, server=str(args.get("server") or "") or None
    )
    if dst is None:
        return "недоступно: VPN сейчас не поднят ни на одной ноде роя"

    if action == vpn_protocol.ACTION_PROXY_LINK and not args.get("server"):
        results = await vpn_nodes.fanout(ctx.node_link, action, {})
        if not results:
            return "недоступно: прокси Telegram сейчас не настроен ни на одном сервере"
        return json.dumps(results, ensure_ascii=False)

    if action == vpn_protocol.ACTION_USAGE and args.get("all_guests"):
        is_admin = ctx.subscription is not None and ActionRight(
            vpn_protocol.ACTION_PEERS, _VPN_SERVICE
        ).granted(ctx.subscription)
        if not is_admin:
            return "недоступно: сводка по всем гостям — только у админа"
        payload: dict[str, Any] = {}
    elif action in (
        vpn_protocol.ACTION_USAGE,
        vpn_protocol.ACTION_ISSUE,
        _VPN_ACTION_SETTINGS,
        _VPN_ACTION_REPORT,
    ):
        return await _vpn_person_action(ctx, action, args)
    elif action == vpn_protocol.ACTION_VPN_APK:
        return await _vpn_apps(ctx, dst, str(args.get("app") or "hiddify"))
    elif action == vpn_protocol.ACTION_RESOLVE_REQUEST:
        raw_id = args.get("request_id")
        if raw_id is None:
            return "ошибка: не указан request_id"
        payload = {"request_id": raw_id, "approve": bool(args.get("approve"))}
    elif action == vpn_protocol.ACTION_PEERS:
        payload = {}
    elif action in (
        vpn_protocol.ACTION_PROXY_LINK,
        vpn_protocol.ACTION_PROXY_ROTATE_SECRET,
        vpn_protocol.ACTION_PROXY_USAGE,
    ):
        payload = {}
    else:
        if ctx.chat_id is None or ctx.chat_id == vpn_protocol.PROBE_CHAT_ID:
            return "недоступно: VPN привязан к человеку, а сейчас его нет"
        payload = {"chat_id": ctx.chat_id}
        if action == vpn_protocol.ACTION_REQUEST_EXTRA:
            gb = args.get("gb")
            if gb:
                payload["bytes"] = int(float(gb) * vpn_protocol.GB)

    result = await call_service(
        ctx,
        action,
        payload,
        dst,
        unavailable_subject="VPN-служба не отвечает",
        proto_refusal=_quota_ceiling_refusal,
    )
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def _quota_ceiling_refusal(exc: ProtoError) -> str | None:
    if exc.code != vpn_protocol.ERR_QUOTA_CEILING:
        return None
    return (
        "потолок самообслуживания достигнут — вызови ещё раз с "
        "action=«request_extra», чтобы отправить заявку админу"
    )


async def _vpn_apps(ctx: ToolContext, dst: Address, app: str) -> str:
    if ctx.notifier is None or ctx.chat_id is None or ctx.chat_id <= 0:
        return "недоступно: сейчас не могу отправить сообщение (нужен личный чат)"
    from sa_home_bot.bot import vpn_faq

    if app == "amnezia_apk":
        from sa_home_bot.bot.vpn_apk import deliver_apk

        text = await deliver_apk(
            ctx.node_link, ctx.notifier, ctx.chat_id, dst, message_thread_id=ctx.message_thread_id
        )
        return text if text.startswith(("не вышло", "недоступно")) else f"готово: {text}"
    code = vpn_faq.Q_AWG if app == "amnezia" else vpn_faq.Q_HIDDIFY
    sent = await ctx.notifier.send_direct(
        ctx.chat_id,
        vpn_faq.answer_text(code),
        reply_markup=vpn_faq.answer_keyboard(code, ctx.settings.vpn, can_wizard=False),
        message_thread_id=ctx.message_thread_id,
    )
    if sent is None:
        return "не вышло: сообщение не доставилось"
    return "готово: ссылки на приложение ушли личным сообщением"


_VPN_SERVICE = vpn_protocol.SERVICE_NAME
_VPN_FACTS = vpn_facts.ANSWERS
_VPN_Q_HIDDIFY, _VPN_Q_AWG = vpn_facts.Q_HIDDIFY, vpn_facts.Q_AWG
_VPN_Q_TROUBLE, _VPN_Q_DEVICE = vpn_facts.Q_TROUBLE, vpn_facts.Q_DEVICE
_VPN_APK_NOTE, _VPN_RENAME_NOTE = vpn_facts.APK_NOTE, vpn_facts.RENAME_NOTE
_VPN_VARIANTS = VariantRights(
    param="action",
    rights=(
        (vpn_protocol.ACTION_USAGE, ActionRight(vpn_protocol.ACTION_USAGE, _VPN_SERVICE)),
        (vpn_protocol.ACTION_ISSUE, ActionRight(vpn_protocol.ACTION_ISSUE, _VPN_SERVICE)),
        (_VPN_ACTION_SETTINGS, ActionRight(vpn_protocol.ACTION_VPN_CARD, _VPN_SERVICE)),
        (_VPN_ACTION_REPORT, ActionRight(vpn_protocol.ACTION_USAGE, _VPN_SERVICE)),
        (
            vpn_protocol.ACTION_GRANT_EXTRA,
            ActionRight(vpn_protocol.ACTION_GRANT_EXTRA, _VPN_SERVICE),
        ),
        (
            vpn_protocol.ACTION_REQUEST_EXTRA,
            ActionRight(vpn_protocol.ACTION_REQUEST_EXTRA, _VPN_SERVICE),
        ),
        (vpn_protocol.ACTION_VPN_APK, ActionRight(vpn_protocol.ACTION_VPN_APK, _VPN_SERVICE)),
        (vpn_protocol.ACTION_PEERS, ActionRight(vpn_protocol.ACTION_PEERS, _VPN_SERVICE)),
        (
            vpn_protocol.ACTION_RESOLVE_REQUEST,
            ActionRight(vpn_protocol.ACTION_RESOLVE_REQUEST, _VPN_SERVICE),
        ),
        (
            vpn_protocol.ACTION_PROXY_LINK,
            ActionRight(vpn_protocol.ACTION_PROXY_LINK, _VPN_SERVICE),
        ),
        (
            vpn_protocol.ACTION_PROXY_ROTATE_SECRET,
            ActionRight(vpn_protocol.ACTION_PROXY_ROTATE_SECRET, _VPN_SERVICE),
        ),
        (
            vpn_protocol.ACTION_PROXY_USAGE,
            ActionRight(vpn_protocol.ACTION_PROXY_USAGE, _VPN_SERVICE),
        ),
    ),
)

_DECL_VPN: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "vpn",
        "description": (
            "Личный VPN (обход блокировок) на серверах в нескольких странах. Устройство — "
            "это один телефон или компьютер: в нём сразу все страны, отдельных ключей "
            "«на технологию» человеку не выдают. Ключи, ссылки vless, QR и файлы я НЕ "
            "показываю и сам не выпускаю — они приходят только отдельными сообщениями "
            "бота по кнопкам (и удаляются через время).\n"
            "usage — статус: страны (✅ работает / ⚠️ может не работать), остаток квоты "
            "до 1 числа, устройства с трафиком и временем последней связи; расскажи "
            "своими словами (у админа с all_guests=true — сводка по всем гостям и "
            "свободный резерв ноды); issue — ПОДКЛЮЧИТЬ новое устройство: человеку "
            "приходит сообщение с кнопкой «📱 Подключить по шагам», дальше он идёт по "
            "шагам сам; имён и технологий не спрашивай; settings — дать настройки "
            "СУЩЕСТВУЮЩЕГО устройства (device_label — имя из usage): человеку приходит "
            "экран «📥 Получить настройки», там выбор «VLESS · Hiddify» или AmneziaVPN, "
            "перевыпуск ключей — «⚙️ Управление устройствами» → устройство → «🔄 Перевыпустить "
            "ключи»; report — "
            "сообщить владельцу о проблеме (reason — со слов человека, коротко; не "
            "выдумывай причину; раз в 10 минут на человека); grant_extra — добавить "
            "трафика самому (только когда осталось мало; потолок — тогда request_extra, "
            "заявка админу, gb — сколько ГБ); apk — прислать приложение: app="
            "'hiddify' (по умолчанию, для «VLESS · Hiddify»), 'amnezia' (AmneziaVPN), "
            "'amnezia_apk' (файл AmneziaWG — только запасной вариант, если магазин "
            "недоступен); proxy_link — ссылки на прокси Telegram (mtg — НЕ VPN, только "
            "сам Telegram, ставится в его настройках; без server — со всех серверов, "
            "покажи все) плюс SOCKS5 для ботов; proxy_rotate_secret — сменить секрет "
            "прокси (старая ссылка сразу перестаёт работать у ВСЕХ — только по явной "
            "просьбе); proxy_usage — расход прокси за месяц.\n"
            "issue БЕЗ recipient — себе. «Подключи Наталью» (ДРУГОМУ человеку) — "
            "recipient=«Наталья», только админу; без права тул откажет, не выдумывай, "
            "что получилось. Сообщение уйдёт в личку получателя — не говори, что "
            "кнопку нажал ты или собеседник.\n"
            "Справка — отвечай коротко, в своём образе, на «вы», без упоминаний пола, "
            "бабушек и родственников, по этим фактам, ничего не выдумывая:\n"
            f"• Чем отличается AmneziaVPN: {_VPN_FACTS[_VPN_Q_AWG]} {_VPN_APK_NOTE}\n"
            f"• Что такое Hiddify: {_VPN_FACTS[_VPN_Q_HIDDIFY]}\n"
            f"• Не подключается: {_VPN_FACTS[_VPN_Q_TROUBLE].rsplit(chr(10), 1)[0]}\n"
            "  Если не помогло — report (ты и есть «Альфред» из справки).\n"
            f"• Ещё одно устройство: {_VPN_FACTS[_VPN_Q_DEVICE]} Если просят подключить — issue.\n"
            f"• {_VPN_RENAME_NOTE}\n"
            "Значения action перечислены в enum: чего там нет — не умеешь."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [v for v, _ in _VPN_VARIANTS.rights],
                    "description": "какое действие выполнить",
                },
                "device_label": {
                    "type": "string",
                    "description": (
                        "settings: имя существующего устройства (из usage); report: "
                        "необязательно — о каком устройстве речь"
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "report: что случилось — со слов человека, коротко",
                },
                "app": {
                    "type": "string",
                    "enum": ["hiddify", "amnezia", "amnezia_apk"],
                    "description": "apk: какое приложение прислать (по умолчанию hiddify)",
                },
                "recipient": {
                    "type": "string",
                    "description": (
                        "issue: имя/ник ДРУГОГО человека, которого подключить (не себя) "
                        "— только у админа. Без этого параметра — всегда себе"
                    ),
                },
                "all_guests": {
                    "type": "boolean",
                    "description": (
                        "usage: true — сводка по всем гостям и свободный резерв "
                        "ноды (только у админа); без этого — свой статус"
                    ),
                },
                "gb": {
                    "type": "number",
                    "description": "request_extra: сколько ГБ попросить (по умолчанию — шаг)",
                },
                "request_id": {
                    "type": "integer",
                    "description": "resolve_request: номер заявки",
                },
                "approve": {
                    "type": "boolean",
                    "description": "resolve_request: одобрить (true) или отклонить (false)",
                },
            },
            "required": ["action"],
        },
    },
}


VPN = ToolSpec(
    name="vpn",
    handler=tool_vpn,
    declaration=_DECL_VPN,
    variants=_VPN_VARIANTS,
    telegram_only=True,
)
