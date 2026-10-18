from sa_home_bot.bot import guest_rights, vpn_admin_view
from sa_home_bot.subscriptions.models import SOURCE_GUEST, Subscription

GB = 1_000_000_000


def _guest(chat_id: int, name: str = "Аня", rights: frozenset[str] = frozenset()) -> Subscription:
    return Subscription(name=name, chat_id=chat_id, source=SOURCE_GUEST, allowed_commands=rights)


def _vpn_guest(chat_id: int, name: str = "Аня") -> Subscription:
    group = guest_rights.group("vpn")
    assert group is not None
    return _guest(chat_id, name, rights=group.rights)


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


def button_texts(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


def test_guests_view_shows_where_each_guest_is_open():
    guests = [_guest(11), _guest(22, "Борис")]
    access = {11: [_server()], 22: [_server(allowed=False)]}
    text, kb = vpn_admin_view.build_guests_view(guests, access, 0)
    assert "🇳🇱 2/200 ГБ" in text
    assert "доступа нет" in text
    assert vpn_admin_view.guest_cb(11) in _callbacks(kb)


def test_guests_view_lists_guest_with_no_locations_at_all():
    text, kb = vpn_admin_view.build_guests_view([_guest(11)], {}, 0)
    assert "доступа нет" in text
    assert vpn_admin_view.guest_cb(11) in _callbacks(kb)


def test_guests_view_puts_vpn_group_first():
    guests = [_guest(11, "Без VPN"), _vpn_guest(22, "С VPN"), _guest(33, "Тоже без")]
    text, kb = vpn_admin_view.build_guests_view(guests, {}, 0)
    assert _callbacks(kb)[0] == vpn_admin_view.guest_cb(22)
    assert text.index("С VPN") < text.index("Без VPN")


def test_sort_guests_keeps_order_inside_halves():
    guests = [_guest(11, "б"), _vpn_guest(22, "в"), _guest(33, "а"), _vpn_guest(44, "г")]
    assert [g.chat_id for g in vpn_admin_view.sort_guests(guests)] == [22, 44, 11, 33]


def test_has_any_vpn_right_counts_partial_and_wildcards():
    group = guest_rights.group("vpn")
    assert vpn_admin_view.has_any_vpn_right(_vpn_guest(1))
    assert vpn_admin_view.has_any_vpn_right(_guest(2, rights=frozenset({"usage@vpn"})))
    assert vpn_admin_view.has_any_vpn_right(_guest(3, rights=frozenset({"*@vpn"})))
    assert vpn_admin_view.has_any_vpn_right(_guest(4, rights=frozenset({"*"})))
    assert not vpn_admin_view.has_any_vpn_right(_guest(5, rights=frozenset({"chat@llm"})))
    assert not vpn_admin_view.has_any_vpn_right(_guest(6))
    assert group is not None


def test_guests_view_paginates():
    guests = [_guest(i) for i in range(1, 20)]
    _text, kb = vpn_admin_view.build_guests_view(guests, {}, 0)
    assert vpn_admin_view.guests_cb(vpn_admin_view.GUEST_PAGE_SIZE) in _callbacks(kb)


def test_server_name_is_flag_alone_when_label_has_one():
    assert vpn_admin_view.server_name(_server()) == "🇳🇱"
    assert vpn_admin_view.server_name(_server(label="🇺🇸 США")) == "🇺🇸"


def test_server_name_keeps_label_without_flag():
    assert vpn_admin_view.server_name(_server(label="Дача")) == "Дача"
    assert vpn_admin_view.server_name(_server(label="")) == "jeeves"


def test_guest_view_marks_open_and_closed_locations():
    guest = _guest(11)
    servers = [_server(), _server("wooster", label="🇺🇸 США", allowed=False)]
    text, kb = vpn_admin_view.build_guest_view(guest, servers)
    assert "✅" in text and "⬜" in text
    assert "Нидерланды" not in text and "США" not in text
    assert "🇳🇱" in text and "🇺🇸" in text
    assert "база 200 ГБ (личная)" in text
    assert vpn_admin_view.location_cb(11, "jeeves") in _callbacks(kb)
    assert vpn_admin_view.location_cb(11, "wooster") in _callbacks(kb)


def test_guest_view_survives_dead_swarm():
    text, kb = vpn_admin_view.build_guest_view(_guest(11), [])
    assert "не на связи" in text
    assert _callbacks(kb) == [vpn_admin_view.rights_cb(11), vpn_admin_view.guests_cb(0)]


def test_location_view_offers_quota_presets_when_open():
    text, kb = vpn_admin_view.build_location_view(_guest(11), _server())
    callbacks = _callbacks(kb)
    assert "открыт" in text
    assert vpn_admin_view.set_access_cb(11, "jeeves", "off") in callbacks
    for preset in vpn_admin_view.QUOTA_PRESETS_GB:
        assert vpn_admin_view.set_access_cb(11, "jeeves", str(preset)) in callbacks
    assert any(t.startswith("•200") for t in button_texts(kb))


def test_location_view_hides_quota_buttons_while_closed():
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
    _text, kb = vpn_admin_view.build_location_view(
        _guest(11), _server(base_limit_bytes=vpn_admin_view.QUOTA_STEP_GB * GB)
    )
    assert vpn_admin_view.set_access_cb(11, "jeeves", "50") in _callbacks(kb)


def test_parse_guest_value_tells_screen_from_page():
    assert vpn_admin_view.parse_guest_value("g42") == 42
    assert vpn_admin_view.parse_guest_value("0") is None
    assert vpn_admin_view.parse_guest_value(None) is None
    assert vpn_admin_view.parse_guest_value("gxx") is None


def test_parse_guest_value_handles_negative_chat_id():
    assert vpn_admin_view.parse_guest_value("g-100500") == -100500


def test_parse_set_access_value():
    assert vpn_admin_view.parse_set_access_value("42_on") == (42, "on")
    assert vpn_admin_view.parse_set_access_value("42_200") == (42, "200")
    assert vpn_admin_view.parse_set_access_value("42") is None
    assert vpn_admin_view.parse_set_access_value(None) is None


def test_callbacks_fit_telegram_limit():
    longest = vpn_admin_view.set_access_cb(-1001234567890, "wooster", "1000")
    assert len(longest.encode()) <= 64


def test_rights_view_marks_full_partial_and_empty():
    guest = _guest(11, rights=frozenset({"usage@vpn"}))
    _text, kb = vpn_admin_view.build_rights_view(guest)
    marks = {text.split(" ", 1)[0] for text in button_texts(kb) if text[0] in "✅🔸⬜"}
    assert marks == {"🔸", "⬜"}


def test_rights_view_shows_owner_wildcard_as_enabled():
    owner = Subscription(chat_id=1, name="admin", allowed_commands=frozenset({"*"}))
    for toggle in vpn_admin_view.VPN_TOGGLES:
        assert vpn_admin_view.toggle_state(owner, toggle)


def test_toggle_fills_incomplete_set_before_removing_it():
    toggle = vpn_admin_view.toggle_by_key("dev")
    assert toggle is not None
    partial = _guest(11, rights=frozenset({"issue@vpn"}))
    filled = vpn_admin_view.toggle_rights(partial, toggle)
    assert toggle.rights <= filled

    full = _guest(11, rights=filled)
    assert vpn_admin_view.toggle_state(full, toggle)
    assert not (vpn_admin_view.toggle_rights(full, toggle) & toggle.rights)


def test_toggle_touches_only_its_own_rights():
    toggle = vpn_admin_view.toggle_by_key("proxy")
    assert toggle is not None
    guest = _guest(11, rights=frozenset({"chat@llm", "proxy_link@vpn"}))
    assert vpn_admin_view.toggle_rights(guest, toggle) == frozenset({"chat@llm"})


def test_proxy_toggle_is_not_part_of_the_vpn_group():
    group = guest_rights.group("vpn")
    assert group is not None
    assert "proxy_link@vpn" not in group.rights
    toggle = vpn_admin_view.toggle_by_key("proxy")
    assert toggle is not None
    assert not vpn_admin_view.toggle_state(_vpn_guest(11), toggle)


def test_guest_card_links_to_rights_screen():
    _text, kb = vpn_admin_view.build_guest_view(_vpn_guest(11), [_server()])
    assert vpn_admin_view.rights_cb(11) in _callbacks(kb)


def test_guest_card_lists_enabled_skills():
    text, _kb = vpn_admin_view.build_guest_view(_vpn_guest(11), [_server()])
    assert "Умения:" in text
    assert "Прокси Telegram" not in text


def test_parse_rights_value():
    assert vpn_admin_view.parse_rights_value("r42") == 42
    assert vpn_admin_view.parse_rights_value("r-100500") == -100500
    assert vpn_admin_view.parse_rights_value("g42") is None
    assert vpn_admin_view.parse_rights_value(None) is None


def test_rights_callbacks_fit_telegram_limit():
    longest = max(
        vpn_admin_view.toggle_cb(-1001234567890, toggle.key)
        for toggle in vpn_admin_view.VPN_TOGGLES
    )
    assert len(longest.encode()) <= 64
    assert len(vpn_admin_view.rights_cb(-1001234567890).encode()) <= 64
