"""Тексты и кнопки VPN-уведомлений (этап 57.7).

Единый вид: страна (флаг, а в длинных фразах и название) в каждом сообщении гостю
и владельцу. Страна определяется по ноде-источнику события: служба vpn кладёт в
событие ``node`` и ``location`` ([vpn].location, «🇳🇱 Нидерланды»); у ноды старее
57.7 ``location`` нет — тогда берём запомненную подпись ноды, а нет и её — пишем
без страны. Тексты на «вы», без пола; пробник ``chat_id=0`` сюда не попадает.
"""

from __future__ import annotations

import html
from collections.abc import Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands, vpn_nodes, vpn_settings, vpn_wizard
from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.vpn import protocol as vpn_protocol

SERVICE = vpn_protocol.SERVICE_NAME
GB = 1_000_000_000

# «в Нидерландах» — страну в предложном падеже по названию из [vpn].location.
_IN_COUNTRY = {
    "Нидерланды": "в Нидерландах",
    "США": "в США",
    "Германия": "в Германии",
    "Финляндия": "в Финляндии",
    "Франция": "во Франции",
    "Швеция": "в Швеции",
    "Польша": "в Польше",
    "Великобритания": "в Великобритании",
    "Казахстан": "в Казахстане",
    "Россия": "в России",
    "Сингапур": "в Сингапуре",
    "Япония": "в Японии",
    "Швейцария": "в Швейцарии",
    "Латвия": "в Латвии",
    "Литва": "в Литве",
    "Эстония": "в Эстонии",
    "Турция": "в Турции",
    "Канада": "в Канаде",
}


def country_of(data: dict) -> vd.Country:
    """Страна по ноде-источнику события."""
    node = str(data.get("node") or "")
    label = str(data.get("location") or "")
    if not label and node:
        label = vpn_nodes._load_known().get(node, "")  # noqa: SLF001 — запомненная подпись
    return vd.Country(node, label)


def tag(country: vd.Country) -> str:
    """«VPN 🇳🇱» / «VPN» — заголовок любого уведомления."""
    return f"VPN {country.flag}" if country.flag else "VPN"


def in_country(country: vd.Country) -> str:
    """«в Нидерландах»; страна неизвестна — пусто."""
    return _IN_COUNTRY.get(country.name, "")


def _gb(bytes_: int) -> str:
    return f"{max(bytes_, 0) / GB:.1f}"


# --- квота -------------------------------------------------------------------


def quota_warning_text(country: vd.Country, remaining_bytes: int) -> str:
    return f"📶 {tag(country)}: осталось {_gb(remaining_bytes)} ГБ до конца месяца."


def quota_exceeded_text(country: vd.Country, *, others_work: bool) -> str:
    place = in_country(country)
    text = f"⛔️ {tag(country)}: лимит месяца исчерпан"
    text += f", {place} связь приостановлена." if place else ", связь приостановлена."
    if others_work:
        text += " Другие страны работают."
    return text


def access_restored_text(country: vd.Country) -> str:
    return f"✅ {tag(country)}: снова работает."


def grant_keyboard(country: vd.Country) -> InlineKeyboardMarkup:
    suffix = f" {country.flag}" if country.flag else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"➕ 100 ГБ{suffix}",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_GRANT_EXTRA,
                        service=SERVICE,
                        node_id=country.node or None,
                    ),
                )
            ]
        ]
    )


def others_with_quota(servers: Iterable[dict], node: str) -> bool:
    """Есть ли у человека другие открытые страны с остатком (ответы usage)."""
    return any(
        s.get("node") != node
        and s.get("allowed", True)
        and not s.get("blocked")
        and int(s.get("remaining_bytes") or 0) > 0
        for s in servers
    )


def extra_resolved_text(country: vd.Country, approved: bool) -> str:
    if approved:
        return f"✅ {tag(country)}: заявка на доп. трафик одобрена."
    return f"🚫 {tag(country)}: заявка на доп. трафик отклонена."


# --- владельцу ---------------------------------------------------------------


def _where_admin(country: vd.Country) -> str:
    """«🇳🇱 Нидерланды» — для владельца с названием; нет подписи — id ноды."""
    return html.escape(country.label or country.node or "?")


def extra_requested_text(country: vd.Country, chat_id: int, gb: float, request_id: object) -> str:
    return (
        f"✋ {tag(country)}: гость <code>{chat_id}</code> просит ещё {gb:.0f} ГБ "
        f"(заявка №{request_id})."
    )


def node_quota_text(country: vd.Country, used_bytes: int, limit_bytes: int) -> str:
    return (
        f"⚠️ VPN {_where_admin(country)}: канал близок к месячному лимиту тарифа — "
        f"{used_bytes / GB:.0f} / {limit_bytes / GB:.0f} ГБ."
    )


def peer_issued_text(country: vd.Country, chat_id: int, device_label: str) -> str:
    return (
        f"🔐 {tag(country)}: выдан доступ гостю <code>{chat_id}</code> "
        f"({html.escape(device_label)})."
    )


# --- новый доступ ------------------------------------------------------------


def access_opened_text(country: vd.Country, base_gb: float, *, has_devices: bool | None) -> str:
    where = html.escape(country.label or country.node or "VPN")
    text = f"📶 Вам открыт VPN: {where}, {base_gb:.0f} ГБ в месяц."
    if has_devices:
        text += (
            "\nСтрана появится в Hiddify сама; для AmneziaWG — «📥 Получить настройки» "
            "в «⚙️ Управление»."
        )
    return text


def access_closed_text(country: vd.Country) -> str:
    return f"📶 {tag(country)}: доступ закрыт."


def access_keyboard(*, has_devices: bool | None) -> InlineKeyboardMarkup:
    if has_devices is False:
        button = vpn_wizard.wizard_button()
    else:
        button = InlineKeyboardButton(text="📶 Открыть VPN", callback_data=vpn_wizard.home_cb())
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


# --- сервер переустановлен ---------------------------------------------------


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def real_affected(affected: object) -> list[dict]:
    """Задетые без пробника ``chat_id=0`` и без мусора."""
    out: list[dict] = []
    for item in affected if isinstance(affected, list) else []:
        if not isinstance(item, dict):
            continue
        chat_id = item.get("chat_id")
        if isinstance(chat_id, int) and chat_id != 0 and item.get("device_label"):
            out.append(item)
    return out


def group_by_chat(items: list[dict]) -> dict[int, dict[str, list[str]]]:
    """chat_id → {device_label → [transport…]} в порядке появления."""
    grouped: dict[int, dict[str, list[str]]] = {}
    for item in items:
        transports = grouped.setdefault(item["chat_id"], {}).setdefault(item["device_label"], [])
        transport = item.get("transport") or vpn_protocol.TRANSPORT_AWG
        if transport not in transports:
            transports.append(transport)
    return grouped


def _devices_phrase(devices: dict[str, list[str]]) -> str:
    parts = []
    for label, transports in devices.items():
        ordered = [t for t in vd.TRANSPORT_ORDER if t in transports]
        names = ", ".join(vd.TRANSPORT_NAME[t] for t in ordered)
        parts.append(f"{html.escape(label)} ({names})" if names else html.escape(label))
    return ", ".join(parts)


def restored_guest_text(country: vd.Country, devices: dict[str, list[str]]) -> str:
    text = (
        f"🔧 {tag(country)}: сервер переустановлен, настройки нужно обновить: "
        f"{_devices_phrase(devices)}."
    )
    if any(vd.REALITY in t for t in devices.values()):
        text += "\nЕсли подключались кнопкой «🔌 Подключить», Hiddify обновится сам."
    return text


def restored_guest_keyboard(
    country: vd.Country, devices: dict[str, list[str]]
) -> InlineKeyboardMarkup:
    if len(devices) == 1 and country.node:
        [(label, transports)] = devices.items()
        callback = vpn_settings.restore_cb(vd.device_key(label), country.node, transports)
    else:
        callback = vpn_settings.card_cb("m")
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Обновить", callback_data=callback)]]
    )


def restored_owner_text(country: vd.Country, items: list[dict]) -> str:
    if not items:
        return f"🔧 {tag(country)}: ключ сервера сменился, задетых нет."
    people = len({i["chat_id"] for i in items})
    n = len(items)
    return (
        f"🔧 {tag(country)}: сервер переустановлен, задето {n} "
        f"{_plural(n, 'подключение', 'подключения', 'подключений')} у {people} "
        f"{_plural(people, 'человека', 'человек', 'человек')}."
    )
