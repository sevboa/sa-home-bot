from __future__ import annotations

import pytest

from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot import vpn_notify
from sa_home_bot.config import VpnConfig, VpnWarpConfig
from sa_home_bot.domain.models import OK
from sa_home_bot.utils.process import ProcResult
from sa_home_bot.vpn import protocol as vp
from sa_home_bot.vpn import warp_probe
from sa_home_bot.vpn.warp_probe import WarpProbeMixin
from tests.unit.test_vpn_notify import ADMIN, NL, _run

DOWN = vp.EVENT_VPN_WARP_DOWN
RECOVERED = vp.EVENT_VPN_WARP_RECOVERED


class Host(WarpProbeMixin):
    def __init__(self, cfg: VpnConfig) -> None:
        self._cfg = cfg
        self.events: list[tuple[str, dict]] = []
        self._warp_status = OK
        self._warp_count = 0
        self._warp_since = None
        self._warp_alive = None

    async def _emit(self, name: str, data: dict) -> None:
        self.events.append((name, data))

    def _where(self) -> dict:
        return {"node": "ion", "location": "MD"}


def _host(warp: VpnWarpConfig | None = None) -> Host:
    return Host(VpnConfig(warp=warp))


def _script(monkeypatch, codes: list[str]) -> list[list[str]]:
    calls: list[list[str]] = []
    queue = list(codes)

    async def fake_run(argv, **_kw):
        calls.append(list(argv))
        code = queue.pop(0)
        return ProcResult(0 if code else 28, code.encode(), b"")

    monkeypatch.setattr(warp_probe, "run", fake_run)
    return calls


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ([""], []),
        (["", ""], [DOWN]),
        (["", "204", "", "204"], []),
        (["", "", "204"], [DOWN, RECOVERED]),
        (["", "", "", "", "204", "204"], [DOWN, RECOVERED]),
        (["200", "503", "503"], [DOWN]),
    ],
)
async def test_hysteresis_emits_once_per_transition(monkeypatch, codes, expected):
    host = _host(VpnWarpConfig())
    _script(monkeypatch, codes)
    for _ in codes:
        await host.probe_warp_once()
    assert [name for name, _ in host.events] == expected


async def test_state_follows_probe(monkeypatch):
    host = _host(VpnWarpConfig())
    _script(monkeypatch, ["", "", "204"])
    await host.probe_warp_once()
    assert host.warp_state() == {"alive": False, "down": False}
    await host.probe_warp_once()
    assert host.warp_state() == {"alive": False, "down": True}
    assert host.events == [(DOWN, {"node": "ion", "location": "MD"})]
    await host.probe_warp_once()
    assert host.warp_state() == {"alive": True, "down": False}


async def test_probe_goes_through_local_socks(monkeypatch):
    host = _host(VpnWarpConfig(probe_port=12345))
    calls = _script(monkeypatch, ["204"])
    await host.probe_warp_once()
    argv = calls[0]
    assert argv[argv.index("--socks5-hostname") + 1] == "127.0.0.1:12345"
    assert argv[-1] == "https://www.google.com/generate_204"
    assert "sudo" not in argv


async def test_node_without_warp_does_not_probe(monkeypatch):
    host = _host(None)
    calls = _script(monkeypatch, [])
    await host.probe_warp_once()
    assert calls == []
    assert host.events == []
    assert host.warp_state() is None


async def test_run_error_counts_as_failure(monkeypatch):
    host = _host(VpnWarpConfig(probe_fail_threshold=1))

    async def boom(argv, **_kw):
        raise OSError("no curl")

    monkeypatch.setattr(warp_probe, "run", boom)
    await host.probe_warp_once()
    assert [name for name, _ in host.events] == [DOWN]


async def test_warp_events_reach_owner(monkeypatch):
    notifier, store, send = _run(monkeypatch)
    where = {"node": "ion", "location": NL}
    await send(DOWN, where)
    await send(RECOVERED, where)
    country = vd.Country("ion", NL)
    assert [(chat, text) for chat, text, _m in notifier.sent] == [
        (ADMIN, vpn_notify.warp_down_text(country)),
        (ADMIN, vpn_notify.warp_recovered_text(country)),
    ]
    assert [event[0] for event in store.events] == [DOWN, RECOVERED]
