from sa_home_bot.bot import vpn_admin_view
from sa_home_bot.subscriptions.models import SOURCE_CONFIG, SOURCE_GUEST, Subscription

GB = 1_000_000_000


def _guest(chat_id: int, name: str = "Аня", rights: frozenset[str] = frozenset()) -> Subscription:
    return Subscription(name=name, chat_id=chat_id, source=SOURCE_GUEST, allowed_commands=rights)


def _owner(chat_id: int = 1, name: str = "Владелец") -> Subscription:
    return Subscription(
        name=name, chat_id=chat_id, source=SOURCE_CONFIG, allowed_commands=frozenset({"*"})
    )


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


def test_owner_first_in_list():
    owner = _owner(1, "Владелец")
    guests = [_guest(11, "Гость")]
    all_subs = [owner] + guests
    sorted_subs = vpn_admin_view.sort_guests(all_subs)
    assert sorted_subs[0].chat_id == 1
    assert sorted_subs[1].chat_id == 11


def test_owner_shows_crown_in_guests_view():
    owner = _owner(1, "Владелец")
    guests = [_guest(11, "Гость")]
    all_subs = [owner] + guests
    text, kb = vpn_admin_view.build_guests_view(all_subs, {}, 0)
    assert "👑" in text
    assert "Владелец" in text
    assert vpn_admin_view.guest_cb(1) in _callbacks(kb)


def test_owner_guest_view_no_rights_button():
    owner = _owner(1, "Владелец")
    servers = [_server()]
    text, kb = vpn_admin_view.build_guest_view(owner, servers)
    callbacks = _callbacks(kb)
    assert vpn_admin_view.rights_cb(1) not in callbacks
    assert vpn_admin_view.location_cb(1, "jeeves") in callbacks


def test_owner_location_view_has_quota_controls():
    owner = _owner(1, "Владелец")
    server = _server()
    text, kb = vpn_admin_view.build_location_view(owner, server)
    callbacks = _callbacks(kb)
    assert vpn_admin_view.set_access_cb(1, "jeeves", "off") in callbacks
    for preset in vpn_admin_view.QUOTA_PRESETS_GB:
        assert vpn_admin_view.set_access_cb(1, "jeeves", str(preset)) in callbacks


def test_owner_rights_rejected():
    owner = _owner(1, "Владелец")
    toggle = vpn_admin_view.toggle_by_key("see")
    assert toggle is not None
    assert vpn_admin_view.toggle_state(owner, toggle)


def test_non_owner_config_subscription_not_in_list():
    config_sub = Subscription(
        name="Конфиг", chat_id=100, source=SOURCE_CONFIG, allowed_commands=frozenset({"usage@vpn"})
    )
    guests = [_guest(11, "Гость")]
    all_subs = [config_sub] + guests
    sorted_subs = vpn_admin_view.sort_guests(all_subs)
    assert len(sorted_subs) == 1
    assert sorted_subs[0].chat_id == 11


def test_page_entries_counts_owner():
    owner = _owner(1, "Владелец")
    guests = [_guest(i, f"Гость{i}") for i in range(1, 10)]
    all_subs = [owner] + guests
    page = vpn_admin_view.visible_guests(all_subs, 0)
    assert len(page) == 8
    assert page[0].chat_id == 1
    page2 = vpn_admin_view.visible_guests(all_subs, 8)
    assert len(page2) == 2
    assert page2[0].chat_id == 8


def _find_owner(book_subs: list[Subscription]) -> Subscription | None:
    return next((s for s in book_subs if s.is_owner and s.chat_id > 0), None)


def test_find_owner_returns_owner():
    owner = _owner(1, "Владелец")
    guests = [_guest(11, "Гость")]
    all_subs = [owner] + guests
    found = _find_owner(all_subs)
    assert found is not None
    assert found.chat_id == 1
    assert found.is_owner


def test_find_owner_returns_none_for_non_owner():
    config_sub = Subscription(
        name="Конфиг", chat_id=100, source=SOURCE_CONFIG, allowed_commands=frozenset({"usage@vpn"})
    )
    guests = [_guest(11, "Гость")]
    all_subs = [config_sub] + guests
    found = _find_owner(all_subs)
    assert found is None
