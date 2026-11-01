from __future__ import annotations

from sa_home_bot.bot import vpn_devices
from sa_home_bot.bot.handlers import vpn as handlers
from sa_home_bot.bot.vpn_secrets import PendingVpnSecrets
from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.subscriptions.models import Subscription
from tests.unit.support.fakes import FakeNotifier

ADMIN = Subscription(chat_id=1, name="admin", allowed_commands=frozenset({"*"}))
GUEST = Subscription(
    chat_id=777,
    name="guest",
    allowed_commands=frozenset(
        {"usage@vpn", "issue@vpn", "reissue@vpn", "grant_extra@vpn", "request_extra@vpn"}
    ),
)
GUEST_FULL = Subscription(
    chat_id=778,
    name="guest-full",
    allowed_commands=frozenset({"*@vpn"}),
)


class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(self, chat_id: int) -> None:
        self.chat = FakeChat(chat_id)
        self.answers: list[str] = []
        self.answer_markups: list[object] = []
        self.edits: list[str] = []
        self.edit_markups: list[object] = []
        self.message_thread_id: int | None = None
        self.reply_markup_cleared = False

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append(text)
        self.answer_markups.append(reply_markup)

    async def edit_text(self, text, reply_markup=None, **kwargs):
        self.edits.append(text)
        self.edit_markups.append(reply_markup)

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        if reply_markup is None:
            self.reply_markup_cleared = True


class FakeCallback:
    def __init__(self, data: str, chat_id: int, from_user=None) -> None:
        self.data = data
        self.from_user = from_user
        self.message = FakeMessage(chat_id)
        self.answered: list[tuple] = []

    async def answer(self, *args, **kwargs):
        self.answered.append((args, kwargs))


class FakeNodeLink:
    state: dict = {
        "node": "jeeves",
        "kind": "vps",
        "peers": [],
        "services": [{"name": "vpn", "service": "vpn", "status": "running"}],
    }

    def __init__(self, result=None, raises=None) -> None:
        self._result = result if result is not None else {}
        self._raises = raises
        self.calls: list[tuple[str, dict]] = []
        self.dsts: list[object] = []

    async def get_state(self, dst=None):
        return self.state

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}))
        self.dsts.append(dst)
        if self._raises is not None:
            raise self._raises
        return self._result


def make_config(ttl_s: float = 600.0) -> Settings:
    return Settings(vpn=VpnConfig(config_message_ttl_s=ttl_s))


def new_pending() -> PendingVpnSecrets:
    return PendingVpnSecrets()


GB = 10**9


def make_conn(transport, hs=None, *, broken=False, created="2026-10-01T10:00:00+00:00"):
    return {
        "transport": transport,
        "status": "active",
        "last_handshake_at": hs,
        "created_at": created,
        "broken": broken,
        "used_bytes": 0,
    }


def make_srv(node, label, entries, **over):
    return {
        "node": node,
        "label": label,
        "allowed": True,
        "limit_bytes": 100 * GB,
        "remaining_bytes": 87 * GB,
        "used_bytes": 13 * GB,
        "transports": ["reality", "awg"],
        "proxy_available": True,
        "device_usage": entries,
        "devices": [],
        "check": [],
    } | over


def all_buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def button_texts(markup):
    return [b.text for b in all_buttons(markup)]


def last_edit(callback):
    return callback.message.edits[-1], callback.message.edit_markups[-1]


async def press(
    data,
    *,
    sub=GUEST_FULL,
    link=None,
    chat_id=778,
    cfg=None,
    book=None,
    notifier=None,
    pend=None,
    user=None,
    store=None,
):
    link = link or FakeNodeLink()
    notifier = notifier or FakeNotifier(first_message_id=0)
    callback = FakeCallback(data, chat_id=chat_id)
    if user is not None:
        callback.from_user = user
    await handlers.handle_action(
        callback,
        link,
        notifier,
        cfg or make_config(),
        sub,
        pend or new_pending(),
        book,
        store=store,
    )
    return callback, link, notifier


LABEL = "📱 Очень длинное имя устройства для проверки лимита"
KEY = vpn_devices.device_key(LABEL)
