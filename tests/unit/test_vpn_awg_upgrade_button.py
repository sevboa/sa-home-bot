from __future__ import annotations

import pytest

from sa_home_bot.bot import vpn_callbacks, vpn_screens, vpn_settings
from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.vpn import protocol as vpn_protocol

from .vpn_support import (
    GB,
    GUEST_FULL,
    KEY,
    LABEL,
    FakeNodeLink,
    button_texts,
    make_conn,
    make_srv,
    press,
)


def _servers(*, legacy_awg=False, broken=False):
    nl = make_srv(
        "jeeves",
        "🇳🇱 Нидерланды",
        [
            {
                "device_label": LABEL,
                "used_bytes": int(8.7 * GB),
                "connections": [make_conn("reality", "2026-10-10T14:02:00+00:00")],
            }
        ],
    )
    us = make_srv(
        "wooster",
        "🇺🇸 США",
        [
            {
                "device_label": LABEL,
                "used_bytes": int(0.4 * GB),
                "connections": [
                    make_conn("reality"),
                    {**make_conn("awg", broken=broken), "awg_legacy": legacy_awg},
                ],
            }
        ],
    )
    return [nl, us]


class Link(FakeNodeLink):
    def __init__(self, servers, **kw):
        super().__init__(**kw)
        self.servers = servers

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}))
        self.dsts.append(dst)
        if action == vpn_protocol.ACTION_REISSUE:
            return {
                "device_label": args["device_label"],
                "transport": args["transport"],
                "config_text": "cfg",
                "qr_png_b64": None,
                "location": "🇺🇸 США",
                "prior_device_count": 1,
            }
        return {}


@pytest.fixture(autouse=True)
def _fanout(monkeypatch):
    state = {"servers": _servers()}

    async def fake_fanout(node_link, action, args):
        return state["servers"]

    monkeypatch.setattr("sa_home_bot.bot.vpn_nodes.fanout", fake_fanout)
    return state


async def _press(data, sub=GUEST_FULL, link=None, chat_id=778):
    return await press(data, sub=sub, link=link or Link(None), chat_id=chat_id)


def test_build_devices_carries_awg_legacy():
    servers = _servers(legacy_awg=True)
    devices = vd.build_devices(servers)
    device = devices[0]
    awg_conns = [c for c in device.issued if c.transport == vpn_protocol.TRANSPORT_AWG]
    assert len(awg_conns) == 1
    assert awg_conns[0].awg_legacy is True
    assert awg_conns[0].node == "wooster"


def test_legacy_awg_nodes_returns_only_awg_with_legacy():
    servers = _servers(legacy_awg=True)
    device = vd.build_devices(servers)[0]
    assert device.legacy_awg_nodes() == ["wooster"]


def test_legacy_awg_nodes_no_duplicates():
    servers = _servers(legacy_awg=True)
    device = vd.build_devices(servers)[0]
    assert device.legacy_awg_nodes() == ["wooster"]


def test_legacy_awg_nodes_only_awg_not_reality():
    servers = _servers(legacy_awg=True)
    device = vd.build_devices(servers)[0]
    reality_conns = [c for c in device.issued if c.transport == vpn_protocol.TRANSPORT_REALITY]
    assert all(not c.awg_legacy for c in reality_conns)
    assert device.legacy_awg_nodes() == ["wooster"]


def test_legacy_awg_nodes_empty_when_no_legacy():
    servers = _servers(legacy_awg=False)
    device = vd.build_devices(servers)[0]
    assert device.legacy_awg_nodes() == []


@pytest.mark.parametrize("legacy_awg", [True, False])
async def test_device_card_has_no_awg_upgrade_button(_fanout, legacy_awg):
    _fanout["servers"] = _servers(legacy_awg=legacy_awg)
    cb, _, _ = await _press(f"act:vpn:vpn_card:d{KEY}")
    texts = button_texts(cb.message.edit_markups[-1])
    assert not any("AmneziaWG 2.0" in t for t in texts)


@pytest.mark.parametrize("has_page", [True, False])
def test_card_text_legacy_awg_hint(has_page):
    device = vd.build_devices(_servers(legacy_awg=True))[0]
    text = vd.card_text(device, _servers(legacy_awg=True), has_page=has_page)
    assert "🇺🇸 AmneziaVPN (версия 1) —" in text
    assert "🇺🇸 AmneziaVPN —" not in text
    assert (
        "⚠️ AmneziaVPN (🇺🇸 США) работает на старой версии AmneziaWG — её легче заблокировать."
        in text
    )
    assert "Чтобы перейти на версию 2:" in text
    assert "1. Обновите приложение AmneziaVPN." in text
    assert (
        "3. Когда убедитесь, что новое подключение работает, "
        "удалите старое в приложении AmneziaVPN." in text
    )
    assert "Если перевыпуск выдаёт ошибку — включите Hiddify и попробуйте ещё раз." in text
    if has_page:
        assert (
            f"2. Нажмите «{vpn_settings.PAGE_CARD_BUTTON}», перейдите по ссылке и перевыпустите "
            f"все ключи AmneziaVPN («{vd.PAGE_REISSUE_BUTTON}»), добавьте новые ключи "
            "в приложение." in text
        )
    else:
        assert (
            f"2. Нажмите «{vpn_screens.REISSUE_BUTTON}» ниже и выберите все ключи AmneziaVPN, "
            "добавьте новые ключи в приложение." in text
        )


def test_card_text_without_legacy_awg_has_no_hint():
    servers = _servers(legacy_awg=False)
    device = vd.build_devices(servers)[0]
    text = vd.card_text(device, servers, has_page=True)
    assert "версия 1" not in text
    assert "Чтобы перейти на версию 2" not in text
    assert "🇺🇸 AmneziaVPN —" in text


def test_card_text_hint_names_unknown_node_by_id():
    servers = _servers(legacy_awg=True)
    device = vd.build_devices(servers)[0]
    text = vd.card_text(device, [])
    assert "⚠️ AmneziaVPN (wooster) работает на старой версии AmneziaWG" in text


async def test_show_screen_card_without_page_uses_reissue_button(_fanout):
    _fanout["servers"] = _servers(legacy_awg=True)
    cb, _, _ = await _press(f"act:vpn:vpn_card:d{KEY}")
    text = cb.message.edits[-1]
    assert vpn_screens.REISSUE_BUTTON in text
    assert vpn_settings.PAGE_CARD_BUTTON not in text


async def test_expert_reissue_fix_does_not_send_keep_old(_fanout):
    _fanout["servers"] = _servers(broken=True)
    link = Link(_fanout["servers"])
    cb, _, _ = await _press(
        f"act:vpn:reissue:~{vpn_callbacks.KIND_FIX}{KEY}:wooster",
        link=link,
    )
    reissue_calls = [c for c in link.calls if c[0] == vpn_protocol.ACTION_REISSUE]
    assert len(reissue_calls) == 1
    args = reissue_calls[0][1]
    assert "keep_old" not in args
