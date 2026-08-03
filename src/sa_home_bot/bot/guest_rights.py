"""Каталог прав, которые можно выдать гостю точечно (страница «Добавить
право» в /guests, bot/guests_view.py).

Список статический, а не собранный из describe служб: право гостя — это
конкретная, заранее известная строка (AUTHORIZATION.md §3.2/§3.3), а не то,
что случайно ответит на describe в момент открытия страницы (нужная нода
может в этот момент спать). Сознательно НЕ входят сюда:

- права ноды/питания/самообновления (`restart@node`, `poweroff@node`,
  `update@node`, …) и админские VPN-действия (`peers@vpn`,
  `resolve_request@vpn`, `set_quota@vpn`) — это управление инфраструктурой и
  другими гостями, а не личный доступ гостя; выдаётся, как и раньше, правкой
  config.toml;
- `*@apps` (скилы-приложения) — набор приложений специфичен для конкретной
  машины и меняется независимо от кода бота;
- `invite`/`guests` — право приглашать и управлять гостями делает гостя
  соадминистратором, точечная выдача такого через кнопку — отдельный вопрос
  доверия, не «дать доступ по мелочи»;
- голый `*` и `*@служба` — групповые права серьёзнее одной кнопки, выдаются
  только руками в конфиге.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GuestRight:
    right: str
    label: str


# Порядок — как в списке (группировка по смыслу, не алфавит) — так читается
# страница «Добавить право».
GUEST_RIGHTS: list[GuestRight] = [
    GuestRight("chat@llm", "💬 Разговор с Альфредом"),
    GuestRight("tell@llm", "📨 Передавать сообщения другим"),
    GuestRight("recall@memory", "🧠 Память: вспомнить"),
    GuestRight("remember@memory", "🧠 Память: запомнить"),
    GuestRight("forget@memory", "🧠 Память: забыть"),
    GuestRight("search@net", "🔎 Веб-поиск"),
    GuestRight("nodes", "🕸 Сводка роя"),
    GuestRight("status", "📟 Карточка ноды"),
    GuestRight("stats", "📈 Статистика сканера"),
    GuestRight("downtime", "⏻ История отключений"),
    GuestRight("scan_now@monitor", "🔍 Форс-скан датчиков"),
    GuestRight("wake", "🔌 Разбудить ПК"),
    GuestRight("usage@vpn", "📶 VPN: своя карточка"),
    GuestRight("issue@vpn", "📶 VPN: выпустить доступ"),
    GuestRight("reissue@vpn", "📶 VPN: перевыпустить"),
    GuestRight("revoke@vpn", "📶 VPN: отозвать устройство"),
    GuestRight("grant_extra@vpn", "📶 VPN: докупить +100 ГБ"),
    GuestRight("request_extra@vpn", "📶 VPN: заявка сверх лимита"),
    GuestRight("apk@vpn", "📶 VPN: приложение"),
    GuestRight("list@torrents", "🧲 Торренты: список"),
    GuestRight("space@torrents", "🧲 Торренты: свободное место"),
    GuestRight("search@torrents", "🧲 Торренты: поиск"),
    GuestRight("details@torrents", "🧲 Торренты: карточка раздачи"),
    GuestRight("add@torrents", "🧲 Торренты: добавить"),
    GuestRight("pause@torrents", "🧲 Торренты: пауза"),
    GuestRight("resume@torrents", "🧲 Торренты: возобновить"),
]

_BY_RIGHT = {r.right: r for r in GUEST_RIGHTS}


def label(right: str) -> str:
    """Человеческое название права — или само право, если оно не в каталоге
    (например, выдано руками в config.toml до появления этой страницы)."""
    known = _BY_RIGHT.get(right)
    return known.label if known else right
