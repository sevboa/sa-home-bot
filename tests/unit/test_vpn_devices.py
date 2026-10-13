"""bot/vpn_devices.py: склейка ответов usage всех нод в устройства (57.2),
сводка главной и тексты экспертного режима."""

from __future__ import annotations

from datetime import UTC, datetime

from sa_home_bot.bot import vpn_devices as vd

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
GB = 10**9


def _conn(transport, hs=None, *, broken=False, used=0, created="2026-10-01T10:00:00+00:00"):
    return {
        "transport": transport,
        "status": "active",
        "last_handshake_at": hs,
        "created_at": created,
        "broken": broken,
        "used_bytes": used,
    }


def _server(node, label, entries, **over):
    return {
        "node": node,
        "label": label,
        "limit_bytes": 100 * GB,
        "remaining_bytes": 87 * GB,
        "transports": ["reality", "awg"],
        "device_usage": entries,
        "check": [],
    } | over


NL = _server(
    "jeeves",
    "🇳🇱 Нидерланды",
    [
        {
            "device_label": "iPhone",
            "used_bytes": int(8.7 * GB),
            "connections": [_conn("reality", "2026-10-10T14:02:00+00:00")],
        },
        {
            "device_label": "Rose",
            "used_bytes": 0,
            "connections": [_conn("awg", created="2026-10-05T10:00:00+00:00")],
        },
    ],
)
US = _server(
    "wooster",
    "🇺🇸 США",
    [
        {
            "device_label": "iPhone",
            "used_bytes": int(0.4 * GB),
            "connections": [
                _conn("reality"),
                _conn("awg", broken=True, created="2026-10-02T10:00:00+00:00"),
            ],
        }
    ],
)


def test_devices_are_glued_by_label_across_nodes():
    devices = vd.build_devices([NL, US])
    assert [d.label for d in devices] == ["iPhone", "Rose"]
    iphone = devices[0]
    assert iphone.traffic_by_node == {"jeeves": int(8.7 * GB), "wooster": int(0.4 * GB)}
    assert iphone.used_bytes == int(9.1 * GB)
    assert [(c.node, c.transport) for c in iphone.issued] == [
        ("jeeves", "reality"),
        ("wooster", "awg"),
        ("wooster", "reality"),
    ]
    assert iphone.last_handshake_at == "2026-10-10T14:02:00+00:00"
    assert not iphone.never_connected
    assert devices[1].never_connected


def test_broken_country_detected():
    iphone = vd.build_devices([NL, US])[0]
    assert iphone.broken_nodes() == ["wooster"]
    assert [c.transport for c in iphone.broken_in("wooster")] == ["awg"]
    assert iphone.broken_in("jeeves") == []


def test_old_node_without_device_usage_falls_back_to_devices():
    old = {
        "node": "jeeves",
        "label": "🇳🇱 Нидерланды",
        "devices": [{"device_label": "Rose", "transport": "awg", "last_handshake_at": None}],
    }
    (rose,) = vd.build_devices([old])
    assert rose.label == "Rose" and rose.used_bytes == 0 and rose.never_connected


def test_device_key_is_short_and_stable_for_cyrillic():
    key = vd.device_key("Очень длинное имя устройства 🍎" * 3)
    assert len(key) == 8 and key == vd.device_key("Очень длинное имя устройства 🍎" * 3)
    devices = vd.build_devices([NL, US])
    assert vd.find_device(devices, vd.device_key("Rose")).label == "Rose"
    assert vd.find_device(devices, "nope") is None


def test_card_rows_include_not_issued():
    servers = [NL, US]
    iphone = vd.build_devices(servers)[0]
    rows = vd.card_rows(iphone, servers)
    assert [(c.node, c.transport, c.issued) for c in rows] == [
        ("jeeves", "awg", False),
        ("jeeves", "reality", True),
        ("wooster", "awg", True),
        ("wooster", "reality", True),
    ]


def test_card_text_words_and_terms():
    servers = [NL, US]
    text = vd.card_text(vd.build_devices(servers)[0], servers, now=NOW, tz=UTC)
    assert "<b>iPhone</b> · октябрь" in text
    assert "🇳🇱 Нидерланды — 8.7 ГБ" in text
    assert "🇺🇸 США — 0.4 ГБ" in text
    assert "🇳🇱 VLESS · Hiddify — на связи 10.10 14:02" in text
    assert "🇳🇱 AmneziaVPN — не выдано" in text
    assert "🇺🇸 VLESS · Hiddify — ещё не подключалось" in text
    assert "🇺🇸 AmneziaVPN — 🔧 сервер его не помнит" in text
    assert "России" not in text


def test_list_text():
    devices = vd.build_devices([NL, US])
    text = vd.list_text(devices, tz=UTC)
    assert "iPhone — 9.1 ГБ, на связи 10.10 14:02 🔧" in text
    assert "Rose — ещё не подключалось" in text


def test_home_summary_single_and_multi_country():
    assert vd.home_summary([NL], now=NOW) == ["Осталось 87 ГБ из 100 до 1 ноября."]
    lines = vd.home_summary([NL, US], now=NOW)
    assert lines[0] == "🇳🇱 Нидерланды: осталось 87 ГБ из 100 до 1 ноября."
    assert lines[1].startswith("🇺🇸 США: осталось")


def test_home_summary_december_rolls_to_january_and_small_remaining():
    srv = _server("jeeves", "🇳🇱 Нидерланды", [], remaining_bytes=int(2.5 * GB))
    (line,) = vd.home_summary([srv], now=datetime(2026, 12, 5, tzinfo=UTC))
    assert line == "Осталось 2.5 ГБ из 100 до 1 января."


def test_home_summary_blocked():
    srv = _server("jeeves", "🇳🇱 Нидерланды", [], blocked=True)
    assert "лимит исчерпан" in vd.home_summary([srv], now=NOW)[0]


def _check(transport, status):
    return {"server": "x", "transport": transport, "status": status, "observers": 2}


def test_summary_shows_status_always_and_fallback_hint():
    us = _server("wooster", "🇺🇸 США", [], check=[_check("reality", "alerting")])
    nl = _server("jeeves", "🇳🇱 Нидерланды", [], check=[_check("reality", "ok")])
    lines = vd.home_summary([nl, us], now=NOW)
    assert lines[0] == "🇳🇱 Нидерланды — ✅ работает"
    assert lines[1].startswith("осталось 87 ГБ из 100")
    assert lines[2] == "🇺🇸 США — ⚠️ может не работать"
    assert lines[-1] == "Если одна страна не работает — выберите другую."


def test_summary_no_hint_when_all_ok_or_all_bad_or_no_data():
    ok = [_check("reality", "ok")]
    bad = [_check("reality", "partial")]
    nl_ok = _server("jeeves", "🇳🇱 Нидерланды", [], check=ok)
    us_ok = _server("wooster", "🇺🇸 США", [], check=ok)
    all_ok = vd.home_summary([nl_ok, us_ok], now=NOW)
    assert "✅" in all_ok[0] and not any("выберите" in line for line in all_ok)
    nl_bad = _server("jeeves", "🇳🇱 Нидерланды", [], check=bad)
    us_bad = _server("wooster", "🇺🇸 США", [], check=bad)
    all_bad = vd.home_summary([nl_bad, us_bad], now=NOW)
    assert not any("выберите" in line for line in all_bad)
    # данных нет — статус не пишем, только остаток
    (line,) = vd.home_summary([_server("jeeves", "🇳🇱 Нидерланды", [])], now=NOW)
    assert line == "Осталось 87 ГБ из 100 до 1 ноября."


def test_summary_single_country_with_status():
    nl = _server("jeeves", "🇳🇱 Нидерланды", [], check=[_check("reality", "ok")])
    assert vd.home_summary([nl], now=NOW) == [
        "🇳🇱 Нидерланды — ✅ работает",
        "Осталось 87 ГБ из 100 до 1 ноября.",
    ]


def test_summary_looks_at_vless_first():
    """AmneziaVPN красный, но VLESS (основной способ) зелёный — страна работает."""
    srv = _server(
        "wooster", "🇺🇸 США", [], check=[_check("awg", "alerting"), _check("reality", "ok")]
    )
    assert vd.home_summary([srv], now=NOW)[0] == "🇺🇸 США — ✅ работает"


def test_card_adds_status_only_for_bad_transport():
    servers = [
        _server(
            "wooster",
            "🇺🇸 США",
            [],
            check=[_check("awg", "alerting"), _check("reality", "ok")],
        )
    ]
    import copy

    base = [copy.deepcopy(NL), copy.deepcopy(US)]
    base[1]["check"] = [_check("awg", "alerting"), _check("reality", "ok")]
    text = vd.card_text(vd.build_devices(base)[0], base, now=NOW, tz=UTC)
    assert "🇺🇸 AmneziaVPN — 🔧 сервер его не помнит · ⚠️ сервер сейчас может не работать" in text
    assert "🇺🇸 VLESS · Hiddify — ещё не подключалось\n" in text + "\n"
    assert "🇳🇱 VLESS · Hiddify — на связи 10.10 14:02\n" in text + "\n"
    assert vd.transport_health(servers[0], "awg") == "alerting"
    assert vd.transport_health(servers[0], "reality") == "ok"
    assert vd.transport_health(_server("x", "🇺🇸 США", []), "awg") is None


def test_home_text_has_no_colors_or_per_country_traffic():
    us = _server("wooster", "🇺🇸 США", [], check=[_check("reality", "alerting")])
    nl = _server("jeeves", "🇳🇱 Нидерланды", [], check=[_check("reality", "ok")])
    text = vd.home_text([nl, us], now=NOW)
    assert "🟢" not in text and "🔴" not in text and "🟠" not in text
    assert "Если одна страна не работает — выберите другую." in text
