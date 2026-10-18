from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GuestRight:
    right: str
    label: str


@dataclass(frozen=True)
class RightGroup:
    key: str
    label: str
    members: tuple[GuestRight, ...]
    note: str = ""

    @property
    def rights(self) -> frozenset[str]:
        return frozenset(member.right for member in self.members)


GUEST_GROUPS: list[RightGroup] = [
    RightGroup(
        key="vpn",
        label="📶 VPN",
        members=(
            GuestRight("usage@vpn", "своя карточка"),
            GuestRight("vpn_card@vpn", "вернуться к карточке"),
            GuestRight("issue@vpn", "выпустить доступ"),
            GuestRight("reissue@vpn", "перевыпустить"),
            GuestRight("revoke@vpn", "отозвать устройство"),
            GuestRight("grant_extra@vpn", "докупить +100 ГБ"),
            GuestRight("request_extra@vpn", "заявка сверх лимита"),
            GuestRight("apk@vpn", "приложение"),
        ),
        note="Локации, лимит ГБ и тонкая настройка прав — в /vpn → «👥 Все гости».",
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

GUEST_RIGHTS: list[GuestRight] = [
    GuestRight("chat@llm", "💬 Разговор с Альфредом"),
    GuestRight("tell@llm", "📨 Написать владельцу"),
    GuestRight("search@net", "🔎 Веб-поиск"),
    GuestRight("generate_image@llm", "🎨 Рисовать картинки"),
    GuestRight("nodes", "🕸 Сводка роя"),
    GuestRight("status", "📟 Карточка ноды"),
    GuestRight("stats", "📈 Статистика сканера"),
    GuestRight("downtime", "⏻ История отключений"),
    GuestRight("scan_now@monitor", "🔍 Форс-скан датчиков"),
    GuestRight("wake", "🔌 Разбудить ПК"),
]

FINE_RIGHTS: list[GuestRight] = [
    GuestRight("proxy_link@vpn", "📶 VPN: прокси Telegram"),
]

_BY_GROUP = {group.key: group for group in GUEST_GROUPS}
_BY_RIGHT = (
    {r.right: r for r in GUEST_RIGHTS}
    | {
        member.right: GuestRight(member.right, f"{group.label}: {member.label}")
        for group in GUEST_GROUPS
        for member in group.members
    }
    | {r.right: r for r in FINE_RIGHTS}
)

def label(right: str) -> str:
    known = _BY_RIGHT.get(right)
    return known.label if known else right


def group(key: str) -> RightGroup | None:
    return _BY_GROUP.get(key)
