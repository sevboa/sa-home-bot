"""Этап 52: сборка автовыбора маршрута — стартовые маршруты, оповещения."""

from __future__ import annotations

import json

from sa_home_bot.bot import egress_app
from sa_home_bot.bot.egress import DIRECT
from sa_home_bot.bot.egress_app import (
    NOTICE_MIN_INTERVAL_S,
    SwitchNotices,
    pick_startup_route,
    render_switch,
    startup_routes,
)
from sa_home_bot.config import TelegramConfig

J = "socks5://100.109.139.95:1080"
W = "socks5://100.93.141.23:1080"


def _cfg(**kw) -> TelegramConfig:
    return TelegramConfig(token="1:x", proxy_mode="auto", **kw)


def test_startup_routes_order_and_dedup(tmp_path):
    state = tmp_path / "egress.json"
    assert startup_routes(_cfg(extra_proxies=["100.93.141.23:1080"]), state) == [DIRECT, W]
    state.write_text(json.dumps({"route": J}), encoding="utf-8")
    assert startup_routes(_cfg(proxy=W, extra_proxies=[W, J]), state) == [J, W, DIRECT]


class _Session:
    def __init__(self) -> None:
        self.proxy = None
        self._proxy = None


class _Bot:
    def __init__(self, ok_routes: set[str]) -> None:
        self.session = _Session()
        self._ok = ok_routes
        self.route = DIRECT

    async def get_me(self, request_timeout=None):
        if self.route not in self._ok:
            raise TimeoutError
        return object()


async def test_pick_startup_route_falls_through(monkeypatch):
    bot = _Bot({W})
    monkeypatch.setattr(egress_app, "apply_route", lambda s, r: setattr(bot, "route", r))
    assert await pick_startup_route(bot, [J, DIRECT, W]) == W
    assert bot.route == W


async def test_pick_startup_route_none_keeps_first(monkeypatch):
    bot = _Bot(set())
    monkeypatch.setattr(egress_app, "apply_route", lambda s, r: setattr(bot, "route", r))
    assert await pick_startup_route(bot, [J, DIRECT]) is None
    assert bot.route == J


def test_render_switch_texts():
    cands = [
        {"route": DIRECT, "label": DIRECT, "ok": False, "ms": None},
        {"route": J, "label": "🇳🇱 Амстердам", "ok": True, "ms": 180},
    ]
    text = render_switch(DIRECT, J, "сбои запросов: direct недоступен", cands)
    assert "недоступен напрямую" in text and "🇳🇱 Амстердам" in text
    assert "✅ 🇳🇱 Амстердам 180 мс" in text and "✗ direct" in text
    assert "напрямую" in render_switch(J, DIRECT, "x", cands).splitlines()[0]


class _Sub:
    def __init__(self, chat_id: int, allowed: list[str]) -> None:
        self.chat_id = chat_id
        self.allowed_commands = allowed


class _Book:
    def all(self):
        return [_Sub(1, ["*"]), _Sub(2, ["status"])]


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_direct(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return 1


async def test_switch_notices_antispam():
    t = [0.0]
    notifier = _Notifier()
    notices = SwitchNotices(_Book(), notifier, clock=lambda: t[0])
    await notices.on_switched(DIRECT, J, "r", [])
    t[0] += 60
    await notices.on_switched(J, W, "r", [])
    await notices.on_switched(W, DIRECT, "r", [])
    assert [c for c, _ in notifier.sent] == [1]  # только админ, один раз
    t[0] += NOTICE_MIN_INTERVAL_S
    await notices.on_switched(DIRECT, J, "r", [])
    assert len(notifier.sent) == 2
    assert "ещё 2 раз" in notifier.sent[1][1]
