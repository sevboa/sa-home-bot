"""Каталог прав, которые можно выдать гостю (страница «Добавить право» в
/guests, bot/guests_view.py).

Список статический, а не собранный из describe служб: право гостя — это
конкретная, заранее известная строка (AUTHORIZATION.md §3.2/§3.3), а не то,
что случайно ответит на describe в момент открытия страницы (нужная нода
может в этот момент спать). Сознательно НЕ входят сюда:

- права ноды/питания/самообновления (`restart@node`, `poweroff@node`,
  `update@node`, …) и админские VPN-действия (`peers@vpn`,
  `resolve_request@vpn`, `set_quota@vpn`, `set_access@vpn`) — это управление
  инфраструктурой и другими гостями, а не личный доступ гостя; выдаётся, как
  и раньше, правкой config.toml;
- `*@apps` (скилы-приложения) — набор приложений специфичен для конкретной
  машины и меняется независимо от кода бота;
- `invite`/`guests` — право приглашать и управлять гостями делает гостя
  соадминистратором, точечная выдача такого через кнопку — отдельный вопрос
  доверия, не «дать доступ по мелочи»;
- голый `*` и `*@служба` — групповые права серьёзнее одной кнопки, выдаются
  только руками в конфиге.

Группы (решение пользователя 2026-09-18). Умение службы редко бывает одним
правом: VPN — это семь строк подряд, торренты — тоже семь. Выдавать их по
одной кнопке значит листать страницы и каждый раз вспоминать, какие из них
вместе составляют рабочий комплект. Поэтому такие права собраны в `GuestRight
Group`, а страница оперирует группой целиком: «добавил в группу VPN» = выдан
весь её набор. Группа — это НЕ право `*@служба`: она разворачивается в
поимённый список (инвариант AUTHORIZATION.md §9), поэтому новое умение службы
гостю сама собой не достаётся.

Нюансы внутри группы настраиваются там, где им место: для VPN это допуск к
конкретной локации и её лимит в ГБ — в /vpn (bot/vpn_admin_view.py), а не
правами.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GuestRight:
    right: str
    label: str


@dataclass(frozen=True)
class RightGroup:
    """Набор прав одной службы, выдаваемый и снимаемый целиком."""

    key: str  # короткий id, едет в callback_data («vpn»)
    label: str
    members: tuple[GuestRight, ...]
    note: str = ""  # подсказка на странице выдачи — где крутить нюансы

    @property
    def rights(self) -> frozenset[str]:
        return frozenset(member.right for member in self.members)


# Порядок — как в списке (группировка по смыслу, не алфавит) — так читается
# страница «Добавить право».
GUEST_GROUPS: list[RightGroup] = [
    RightGroup(
        key="vpn",
        label="📶 VPN",
        members=(
            GuestRight("usage@vpn", "своя карточка"),
            # Кнопка «⬅️ Назад» пикеров локации и технологии (bot/handlers/
            # vpn.py) шлёт act:vpn:vpn_card — без этого права гость с issue@vpn
            # упирался в «⛔️ Недоступно» на ровном месте.
            GuestRight("vpn_card@vpn", "вернуться к карточке"),
            GuestRight("issue@vpn", "выпустить доступ"),
            GuestRight("reissue@vpn", "перевыпустить"),
            GuestRight("revoke@vpn", "отозвать устройство"),
            GuestRight("grant_extra@vpn", "докупить +100 ГБ"),
            GuestRight("request_extra@vpn", "заявка сверх лимита"),
            GuestRight("apk@vpn", "приложение"),
        ),
        note="Локации и лимит ГБ выдаются отдельно — в /vpn → «👥 Все гости».",
    ),
    RightGroup(
        key="torrents",
        label="🧲 Торренты",
        members=(
            GuestRight("list@torrents", "список"),
            GuestRight("space@torrents", "свободное место"),
            GuestRight("search@torrents", "поиск"),
            GuestRight("details@torrents", "карточка раздачи"),
            GuestRight("add@torrents", "добавить"),
            GuestRight("pause@torrents", "пауза"),
            GuestRight("resume@torrents", "возобновить"),
        ),
    ),
    RightGroup(
        key="memory",
        label="🧠 Память Альфреда",
        members=(
            GuestRight("recall@memory", "вспомнить"),
            GuestRight("remember@memory", "запомнить"),
            GuestRight("forget@memory", "забыть"),
        ),
    ),
]

# Права, которые группу не образуют — выдаются по одному, как и раньше.
GUEST_RIGHTS: list[GuestRight] = [
    GuestRight("chat@llm", "💬 Разговор с Альфредом"),
    GuestRight("tell@llm", "📨 Написать владельцу"),
    GuestRight("tell_guests@llm", "📨 Писать другим гостям"),
    GuestRight("search@net", "🔎 Веб-поиск"),
    GuestRight("nodes", "🕸 Сводка роя"),
    GuestRight("status", "📟 Карточка ноды"),
    GuestRight("stats", "📈 Статистика сканера"),
    GuestRight("downtime", "⏻ История отключений"),
    GuestRight("scan_now@monitor", "🔍 Форс-скан датчиков"),
    GuestRight("wake", "🔌 Разбудить ПК"),
]

_BY_GROUP = {group.key: group for group in GUEST_GROUPS}
# Метки членов группы короткие («перевыпустить»), поэтому в общем справочнике
# они живут с префиксом группы: строка права одна и та же и в списке группы, и
# в перечне прав, выданных руками в config.toml.
_BY_RIGHT = {r.right: r for r in GUEST_RIGHTS} | {
    member.right: GuestRight(member.right, f"{group.label}: {member.label}")
    for group in GUEST_GROUPS
    for member in group.members
}

# Все права, которые вообще можно выдать кнопкой — одиночные плюс члены групп.
CATALOG_RIGHTS: frozenset[str] = frozenset(_BY_RIGHT)


def label(right: str) -> str:
    """Человеческое название права — или само право, если оно не в каталоге
    (например, выдано руками в config.toml до появления этой страницы)."""
    known = _BY_RIGHT.get(right)
    return known.label if known else right


def group(key: str) -> RightGroup | None:
    return _BY_GROUP.get(key)


def group_of(right: str) -> RightGroup | None:
    """Группа, в которую входит право (None — одиночное или не из каталога)."""
    for item in GUEST_GROUPS:
        if right in item.rights:
            return item
    return None
