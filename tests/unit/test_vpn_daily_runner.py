from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from sa_home_bot.bot import vpn_daily_runner
from sa_home_bot.bot.away import AwayState
from sa_home_bot.bot.vpn_daily_runner import (
    STATE_KEY,
    VpnDailyReport,
    fire_time,
    is_due,
    plain_facts,
)
from sa_home_bot.config import Settings
from sa_home_bot.db.store import ALFRED_AWAY_KEY
from sa_home_bot.llm_chat import ACTION_CHAT
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.vpn import protocol as vpn_protocol
from tests.unit.support.factories import make_book, make_subscription

TZ = ZoneInfo("Asia/Almaty")
OWNER = 11
GROUP = -500
NOW = datetime(2026, 11, 12, 4, 30, tzinfo=UTC)
PNG_SIGNATURE = b"\x89PNG"


def history():
    start = datetime(2026, 11, 12, 4, 30, tzinfo=UTC)
    points = [
        {
            "at": (start - timedelta(minutes=15 * step)).isoformat(),
            "reports": 3,
            "down": 0,
            "partial": 0,
            "median_ms": 500,
        }
        for step in range(8)
    ]
    return {
        "hours": 24,
        "bucket_min": 15,
        "series": [
            {"node": "alfred", "server": "jeeves", "transport": "awg", "points": points},
            {"node": "osip", "server": "jeeves", "transport": "awg", "points": points},
        ],
    }


def scopes():
    return {
        "scopes": {"global": {"jeeves": "ok"}, "russia": {"jeeves": "ok"}},
        "russia": ["osip"],
        "ignored": [],
        "links": [],
    }


class Link:
    def __init__(self, *, dead=(), chat=None, chat_error=None):
        self.dead = set(dead)
        self.chat = chat
        self.chat_error = chat_error
        self.calls = []

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, dst.node if dst is not None else None))
        if action == ACTION_CHAT:
            if self.chat_error is not None:
                raise self.chat_error
            return self.chat
        if dst.node in self.dead:
            raise ServiceUnavailableError("нет связи")
        if action == vpn_protocol.ACTION_CHECK_HISTORY:
            return history()
        return scopes()


class Albums:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    async def send_media_group(self, chat_id, photos, *, caption=None, message_thread_id=None):
        self.sent.append((chat_id, list(photos), caption))
        return [1] if self.ok else None


@pytest.fixture
def nodes(monkeypatch):
    listed = ["jeeves", "wooster"]

    async def live(_link):
        return list(listed)

    monkeypatch.setattr(vpn_daily_runner, "live_vpn_nodes", live)
    monkeypatch.setattr(vpn_daily_runner, "known_server_label", lambda node: "🇳🇱 Нидерланды")
    return listed


def make(store, link, albums=None, *, subscribed=True):
    albums = albums or Albums()
    settings = Settings()
    subs = [make_subscription(OWNER, except_event_types=("vpn_daily_report",))]
    if subscribed:
        subs.append(
            make_subscription(GROUP, "group", "chat@llm", event_types=("vpn_daily_report",))
        )
    report = VpnDailyReport(
        get_node_link=lambda: link,
        store=store,
        config=settings,
        book=make_book(*subs),
        notifier=albums,
    )
    return report, albums


def test_fire_time_is_local():
    assert fire_time(NOW, "09:00", TZ) == datetime(2026, 11, 12, 9, 0, tzinfo=TZ)


@pytest.mark.parametrize(
    ("now", "last_sent", "expected"),
    [
        (datetime(2026, 11, 12, 3, 59, tzinfo=UTC), None, False),
        (datetime(2026, 11, 12, 4, 0, tzinfo=UTC), None, True),
        (datetime(2026, 11, 12, 10, 0, tzinfo=UTC), "2026-11-12", False),
        (datetime(2026, 11, 12, 10, 0, tzinfo=UTC), "2026-11-11", True),
        (datetime(2026, 11, 12, 20, 0, tzinfo=UTC), "2026-11-12", False),
    ],
)
def test_is_due(now, last_sent, expected):
    assert is_due(now, "09:00", TZ, last_sent) is expected


def test_plain_facts_strips_markup():
    assert plain_facts("📊 <b>VPN</b> &lt;ok&gt;") == "📊 VPN <ok>"


async def test_tick_sends_album_once_per_day(store, nodes):
    link = Link(chat={"response": "Сэр, всё спокойно."})
    report, albums = make(store, link)
    assert await report.tick(NOW) is True
    assert [chat for chat, _, _ in albums.sent] == [GROUP]
    _, photos, caption = albums.sent[0]
    assert len(photos) == 2
    assert all(photo.startswith(PNG_SIGNATURE) for photo in photos)
    assert caption.startswith("📊 <b>VPN за сутки</b>")
    assert caption.endswith("Сэр, всё спокойно.")
    assert await store.get_state(STATE_KEY) == "2026-11-12"
    assert await report.tick(NOW + timedelta(hours=1)) is False
    assert len(albums.sent) == 1


async def test_tick_before_time_sends_nothing(store, nodes):
    report, albums = make(store, Link())
    assert await report.tick(datetime(2026, 11, 12, 3, 0, tzinfo=UTC)) is False
    assert albums.sent == []


async def test_falls_back_to_second_node(store, nodes):
    link = Link(dead={"jeeves"})
    report, albums = make(store, link)
    assert await report.tick(NOW) is True
    assert (vpn_protocol.ACTION_CHECK_SCOPES, "wooster") in link.calls
    assert len(albums.sent) == 1


async def test_no_node_answers_keeps_day_open(store, nodes):
    report, albums = make(store, Link(dead={"jeeves", "wooster"}))
    assert await report.tick(NOW) is False
    assert albums.sent == []
    assert await store.get_state(STATE_KEY) is None


async def test_failed_delivery_keeps_day_open(store, nodes):
    report, _ = make(store, Link(), Albums(ok=False))
    assert await report.tick(NOW) is False
    assert await store.get_state(STATE_KEY) is None


async def test_no_subscribers_closes_day(store, nodes):
    report, albums = make(store, Link(), subscribed=False)
    assert await report.tick(NOW) is True
    assert albums.sent == []
    assert await store.get_state(STATE_KEY) == "2026-11-12"


async def test_report_without_conclusion_when_llm_fails(store, nodes):
    report, albums = make(store, Link(chat_error=ServiceUnavailableError("спит")))
    assert await report.tick(NOW) is True
    assert "\n\n" not in albums.sent[0][2]


async def test_report_without_conclusion_when_away(store, nodes):
    current = datetime.now(UTC)
    state = AwayState(until=current + timedelta(hours=3), started_at=current)
    await store.set_state(ALFRED_AWAY_KEY, state.to_json())
    link = Link(chat={"response": "Сэр."})
    report, albums = make(store, link)
    assert await report.tick(NOW) is True
    assert "\n\n" not in albums.sent[0][2]
    assert all(action != ACTION_CHAT for action, _ in link.calls)
