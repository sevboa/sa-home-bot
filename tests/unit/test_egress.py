"""Этап 52: выбор маршрута до Bot API, переключение сессии, детектор, менеджер."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe, GetUpdates
from aiohttp import TCPConnector

from sa_home_bot.bot import egress
from sa_home_bot.bot.egress import (
    DIRECT,
    EgressManager,
    TroubleDetector,
    apply_route,
    choose_route,
    load_saved_state,
)
from sa_home_bot.config import TelegramConfig

TOKEN = "123456:ABCDEF"
J = "socks5://100.1.1.1:1080"
W = "socks5://100.2.2.2:1080"


def cand(node, socks, ms, ok=True, stale=False, check=True):
    return {
        "node": node,
        "label": node,
        "socks": socks,
        "check": {"ok": ok, "ms": ms, "seen_at": "x", "stale": stale} if check else None,
    }


JC = cand("jeeves", "100.1.1.1:1080", 200)
WC = cand("wooster", "100.2.2.2:1080", 400)


def pick(current=DIRECT, direct_ok=False, streak=0, cands=(), bans=None, now=1000.0, **kw):
    return choose_route(
        current=current,
        direct_ok=direct_ok,
        direct_streak=streak,
        candidates=list(cands),
        bans=bans or {},
        now=now,
        **kw,
    )


# --- чистый выбор


def test_direct_stays_when_alive():
    d = pick(DIRECT, True, 2, [JC])
    assert (d.route, d.changed) == (DIRECT, False)


def test_direct_dead_picks_min_ms():
    d = pick(DIRECT, False, 0, [WC, JC])
    assert (d.route, d.changed) == (J, True)


@pytest.mark.parametrize(
    "bad",
    [
        cand("a", None, 100),
        cand("a", "1.1.1.1:1", 100, check=False),
        cand("a", "1.1.1.1:1", 100, ok=False),
        cand("a", "1.1.1.1:1", 100, stale=True),
    ],
)
def test_invalid_candidates_ignored(bad):
    d = pick(DIRECT, False, 0, [bad])
    assert (d.route, d.changed) == (DIRECT, False)


def test_return_to_direct_needs_two_successes():
    assert pick(J, True, 1, [JC]).route == J
    d = pick(J, True, 2, [JC])
    assert (d.route, d.changed) == (DIRECT, True)


def test_prefer_direct_false_stays_on_proxy():
    assert pick(J, True, 5, [JC], prefer_direct=False).route == J


def test_proxy_switch_hysteresis():
    slow = cand("jeeves", "100.1.1.1:1080", 1000)
    fast = cand("wooster", "100.2.2.2:1080", 400)
    assert pick(J, False, 0, [slow, fast]).route == W  # 1000 > 2*400 и разница > 300
    close = cand("jeeves", "100.1.1.1:1080", 700)
    assert not pick(J, False, 0, [close, fast]).changed  # выигрыш < 2×
    small = cand("jeeves", "100.1.1.1:1080", 280)
    tiny = cand("wooster", "100.2.2.2:1080", 100)
    assert not pick(J, False, 0, [small, tiny]).changed  # > 2×, но < 300 мс


def test_current_proxy_invalid_or_banned_switches():
    stale = cand("jeeves", "100.1.1.1:1080", 100, stale=True)
    assert pick(J, False, 0, [stale, WC]).route == W
    d = pick(J, False, 0, [JC, WC], bans={J: 2000.0}, now=1000.0)
    assert d.route == W
    # бан истёк
    assert not pick(J, False, 0, [JC, WC], bans={J: 900.0}, now=1000.0).changed


def test_extra_proxies_after_measured():
    extra = "socks5://9.9.9.9:1080"
    d = pick(DIRECT, False, 0, [WC], extra_proxies=[extra])
    assert d.route == W
    d = pick(DIRECT, False, 0, [], extra_proxies=[extra])
    assert d.route == extra
    d = pick(DIRECT, False, 0, [WC], bans={W: 5000.0}, extra_proxies=[extra])
    assert d.route == extra


def test_no_candidates_keeps_route():
    d = pick(J, False, 0, [])
    assert (d.route, d.changed) == (J, False)


# --- сессия


def test_apply_route_direct_and_proxy():
    s = AiohttpSession(proxy=J)
    assert s.proxy == J
    apply_route(s, DIRECT)
    assert s.proxy is None
    assert s._connector_type is TCPConnector
    assert s._connector_init["ttl_dns_cache"] == 3600
    assert s._connector_init["limit"] == 100
    assert "ssl" in s._connector_init
    assert s._should_reset_connector
    s._should_reset_connector = False
    apply_route(s, W)
    assert s.proxy == W and s._should_reset_connector
    assert s._connector_type is not TCPConnector
    assert egress.current_route(s) == W


# --- детектор


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


async def _ok(bot, method):
    return "ok"


def _failing(exc):
    async def f(bot, method):
        raise exc

    return f


async def test_detector_three_consecutive():
    hits = []
    det = TroubleDetector(on_trouble=lambda: hits.append(1), clock=Clock())
    err = TelegramNetworkError(method=GetMe(), message="boom")
    for _ in range(2):
        with pytest.raises(TelegramNetworkError):
            await det(_failing(err), None, GetMe())
    assert not hits
    await det(_ok, None, GetMe())  # успех сбрасывает
    for _ in range(3):
        with pytest.raises(TelegramNetworkError):
            await det(_failing(err), None, GetMe())
    assert hits == [1]


async def test_detector_no_success_60s():
    hits = []
    clock = Clock()
    det = TroubleDetector(on_trouble=lambda: hits.append(1), clock=clock)
    with pytest.raises(asyncio.TimeoutError):
        await det(_failing(TimeoutError()), None, GetMe())
    assert not hits
    clock.t += 61
    with pytest.raises(asyncio.TimeoutError):
        await det(_failing(TimeoutError()), None, GetMe())
    assert hits == [1]


async def test_detector_getupdates_timeout_counts_and_recovery():
    """Таймаут getUpdates — сбой (блокировка DROP'ом приходит таймаутами)."""
    hits, rec = [], []
    det = TroubleDetector(
        on_trouble=lambda: hits.append(1), on_recovered=lambda: rec.append(1), clock=Clock()
    )
    to = TelegramNetworkError(method=GetUpdates(), message="Request timeout error")
    for _ in range(3):
        with pytest.raises(TelegramNetworkError):
            await det(_failing(to), None, GetUpdates())
    assert hits == [1]
    await det(_ok, None, GetMe())
    assert rec == [1]


async def test_detector_retriggers_after_reset_and_keeps_recovery():
    """После смены маршрута (reset) сбой на новом маршруте снова триггерит,
    а on_recovered всё равно приходит на первом успехе."""
    hits, rec = [], []
    det = TroubleDetector(
        on_trouble=lambda: hits.append(1), on_recovered=lambda: rec.append(1), clock=Clock()
    )
    err = TelegramNetworkError(method=GetMe(), message="x")
    for _ in range(3):
        with pytest.raises(TelegramNetworkError):
            await det(_failing(err), None, GetMe())
    det.reset()
    for _ in range(3):
        with pytest.raises(TelegramNetworkError):
            await det(_failing(err), None, GetMe())
    assert hits == [1, 1]
    det.reset()
    await det(_ok, None, GetMe())
    assert rec == [1]


# --- состояние


def test_state_save_load(tmp_path):
    p = tmp_path / "sub" / "egress.json"
    assert load_saved_state(p) is None
    egress.save_state(p, {"route": J, "ms": 5})
    assert load_saved_state(p)["route"] == J
    p.write_text("{битый", encoding="utf-8")
    assert load_saved_state(p) is None
    p.write_text(json.dumps([1]), encoding="utf-8")
    assert load_saved_state(p) is None


# --- менеджер


def make_manager(tmp_path, cands, probe, **cfg_kw):
    bot = Bot(TOKEN, session=AiohttpSession())
    box = {"c": cands, "probe": probe}

    async def fetch():
        return box["c"]

    clock = Clock()
    m = EgressManager(
        bot,
        TelegramConfig(proxy_mode="auto", **cfg_kw),
        fetch,
        "alfred",
        state_path=tmp_path / "egress.json",
        clock=clock,
        probe_direct=lambda: _aprobe(box),
    )
    return m, box, clock


async def _aprobe(box):
    return box["probe"]


async def test_manager_trouble_tick_flow(tmp_path):
    m, box, clock = make_manager(tmp_path, [JC, WC], False)
    events = []
    m.on_switched = lambda old, new, reason, c: events.append((old, new, c))
    assert m.route == DIRECT
    await m.handle_trouble()
    assert m.route == J and m.bot.session.proxy == J
    assert events[0][0] == DIRECT and events[0][1] == J
    assert m.state()["route"] == J and m.state()["ms"] == 200
    assert load_saved_state(tmp_path / "egress.json")["route"] == J

    # на прокси поймали ошибки → бан, берём wooster
    await m.handle_trouble()
    assert m.route == W and J in m.bans

    # direct ожил: нужен второй тик
    box["probe"] = True
    await m.tick()
    assert m.route == W
    await m.tick()
    assert m.route == DIRECT and m.bot.session.proxy is None
    assert [e[1] for e in events] == [J, W, DIRECT]
    await m.bot.session.close()


async def test_manager_tick_on_direct_and_failed_fetch(tmp_path):
    m, box, clock = make_manager(tmp_path, [], True)

    async def bad():
        raise RuntimeError("нет роя")

    m._fetch = bad
    await m.tick()
    assert m.route == DIRECT
    await m.bot.session.close()


async def test_manager_start_stop(tmp_path):
    m, box, clock = make_manager(tmp_path, [JC], True)
    m.start()
    assert m.detector in list(m.bot.session.middleware)
    await m.stop()
    await m.bot.session.close()
