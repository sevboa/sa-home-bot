"""bot/vpn_admin_view.py: экраны «👥 Все гости» в /vpn — кому на какой локации
открыт доступ и на сколько ГБ. Чистые функции из готовых данных (сеть и запись
— в bot/handlers/vpn.py, они проверяются в test_vpn_handler.py)."""

from sa_home_bot.bot import vpn_admin_view
from sa_home_bot.subscriptions.models import SOURCE_GUEST, Subscription

GB = 1_000_000_000


def _guest(chat_id: int, name: str = "Аня") -> Subscription:
    return Subscription(name=name, chat_id=chat_id, source=SOURCE_GUEST)


def _server(node: str = "jeeves", **over) -> dict:
    return {
        "node": node,
        "label": "🇳🇱 Нидерланды",
        "used_bytes": 2 * GB,
        "limit_bytes": 200 * GB,
        "base_limit_bytes": 200 * GB,
        "personal_base": True,
        "allowed": True,
    } | over


def _callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def _texts(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


# --- список гостей ---------------------------------------------------------


def test_guests_view_shows_where_each_guest_is_open():
    guests = [_guest(11), _guest(22, "Борис")]
    access = {11: [_server()], 22: [_server(allowed=False)]}
    text, kb = vpn_admin_view.build_guests_view(guests, access, 0)
    assert "🇳🇱 Нидерланды 2/200 ГБ" in text
    assert "доступа нет" in text
    assert vpn_admin_view.guest_cb(11) in _callbacks(kb)


def test_guests_view_lists_guest_with_no_locations_at_all():
    """Кому ещё ничего не выдавали — именно ему и надо выдать: список берётся
    из подписок бота, а не из сводки службы."""
    text, kb = vpn_admin_view.build_guests_view([_guest(11)], {}, 0)
    assert "доступа нет" in text
    assert vpn_admin_view.guest_cb(11) in _callbacks(kb)


def test_guests_view_paginates():
    guests = [_guest(i) for i in range(1, 20)]
    _text, kb = vpn_admin_view.build_guests_view(guests, {}, 0)
    assert vpn_admin_view.guests_cb(vpn_admin_view.GUEST_PAGE_SIZE) in _callbacks(kb)


# --- карточка гостя --------------------------------------------------------


def test_guest_view_marks_open_and_closed_locations():
    guest = _guest(11)
    servers = [_server(), _server("wooster", label="🇺🇸 США", allowed=False)]
    text, kb = vpn_admin_view.build_guest_view(guest, servers)
    assert "✅" in text and "⬜" in text
    assert "база 200 ГБ (личная)" in text
    assert vpn_admin_view.location_cb(11, "jeeves") in _callbacks(kb)
    assert vpn_admin_view.location_cb(11, "wooster") in _callbacks(kb)


def test_guest_view_survives_dead_swarm():
    text, kb = vpn_admin_view.build_guest_view(_guest(11), [])
    assert "не на связи" in text
    assert _callbacks(kb) == [vpn_admin_view.guests_cb(0)]


# --- экран локации ---------------------------------------------------------


def test_location_view_offers_quota_presets_when_open():
    text, kb = vpn_admin_view.build_location_view(_guest(11), _server())
    callbacks = _callbacks(kb)
    assert "открыт" in text
    assert vpn_admin_view.set_access_cb(11, "jeeves", "off") in callbacks
    for preset in vpn_admin_view.QUOTA_PRESETS_GB:
        assert vpn_admin_view.set_access_cb(11, "jeeves", str(preset)) in callbacks
    # Текущая база помечена точкой — видно, что выбрано сейчас.
    assert any(t.startswith("•200") for t in _texts(kb))


def test_location_view_hides_quota_buttons_while_closed():
    """Пока доступ закрыт, гигабайты выставлять не из чего — сначала открыть."""
    text, kb = vpn_admin_view.build_location_view(_guest(11), _server(allowed=False))
    callbacks = _callbacks(kb)
    assert "закрыт" in text
    assert vpn_admin_view.set_access_cb(11, "jeeves", "on") in callbacks
    assert not any(c.endswith("_200:jeeves") for c in callbacks)


def test_location_view_step_buttons_walk_around_current_base():
    _text, kb = vpn_admin_view.build_location_view(_guest(11), _server(base_limit_bytes=200 * GB))
    callbacks = _callbacks(kb)
    assert vpn_admin_view.set_access_cb(11, "jeeves", "250") in callbacks
    assert vpn_admin_view.set_access_cb(11, "jeeves", "150") in callbacks


def test_step_down_never_goes_to_zero():
    """Ноль — не квота, а закрытый доступ (служба его и не примет)."""
    _text, kb = vpn_admin_view.build_location_view(
        _guest(11), _server(base_limit_bytes=vpn_admin_view.QUOTA_STEP_GB * GB)
    )
    assert vpn_admin_view.set_access_cb(11, "jeeves", "50") in _callbacks(kb)


# --- разбор callback_data --------------------------------------------------


def test_parse_guest_value_tells_screen_from_page():
    assert vpn_admin_view.parse_guest_value("g42") == 42
    assert vpn_admin_view.parse_guest_value("0") is None  # номер страницы
    assert vpn_admin_view.parse_guest_value(None) is None
    assert vpn_admin_view.parse_guest_value("gxx") is None


def test_parse_guest_value_handles_negative_chat_id():
    # Групповые чаты имеют отрицательный chat_id — «g-100500» должен читаться.
    assert vpn_admin_view.parse_guest_value("g-100500") == -100500


def test_parse_set_access_value():
    assert vpn_admin_view.parse_set_access_value("42_on") == (42, "on")
    assert vpn_admin_view.parse_set_access_value("42_200") == (42, "200")
    assert vpn_admin_view.parse_set_access_value("42") is None
    assert vpn_admin_view.parse_set_access_value(None) is None


def test_callbacks_fit_telegram_limit():
    """Лимит callback_data — 64 байта; chat_id бывает длинным."""
    longest = vpn_admin_view.set_access_cb(-1001234567890, "wooster", "1000")
    assert len(longest.encode()) <= 64
