"""bot/vpn_admin_view.py: экраны «👥 Все гости» в /vpn — кому на какой локации
открыт доступ и на сколько ГБ. Чистые функции из готовых данных (сеть и запись
— в bot/handlers/vpn.py, они проверяются в test_vpn_handler.py)."""

from sa_home_bot.bot import guest_rights, vpn_admin_view
from sa_home_bot.subscriptions.models import SOURCE_GUEST, Subscription

GB = 1_000_000_000


def _guest(chat_id: int, name: str = "Аня", rights: frozenset[str] = frozenset()) -> Subscription:
    return Subscription(
        name=name, chat_id=chat_id, source=SOURCE_GUEST, allowed_commands=rights
    )


def _vpn_guest(chat_id: int, name: str = "Аня") -> Subscription:
    """Гость, которому в /guests выдали группу «📶 VPN»."""
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


def _texts(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


# --- список гостей ---------------------------------------------------------


def test_guests_view_shows_where_each_guest_is_open():
    guests = [_guest(11), _guest(22, "Борис")]
    access = {11: [_server()], 22: [_server(allowed=False)]}
    text, kb = vpn_admin_view.build_guests_view(guests, access, 0)
    assert "🇳🇱 2/200 ГБ" in text
    assert "доступа нет" in text
    assert vpn_admin_view.guest_cb(11) in _callbacks(kb)


def test_guests_view_lists_guest_with_no_locations_at_all():
    """Кому ещё ничего не выдавали — именно ему и надо выдать: список берётся
    из подписок бота, а не из сводки службы."""
    text, kb = vpn_admin_view.build_guests_view([_guest(11)], {}, 0)
    assert "доступа нет" in text
    assert vpn_admin_view.guest_cb(11) in _callbacks(kb)


def test_guests_view_puts_vpn_group_first():
    """Сверху те, с кем тут работают, — кому группа «📶 VPN» уже выдана."""
    guests = [_guest(11, "Без VPN"), _vpn_guest(22, "С VPN"), _guest(33, "Тоже без")]
    text, kb = vpn_admin_view.build_guests_view(guests, {}, 0)
    assert _callbacks(kb)[0] == vpn_admin_view.guest_cb(22)
    assert text.index("С VPN") < text.index("Без VPN")


def test_sort_guests_keeps_order_inside_halves():
    """Сортировка устойчивая: внутри половин порядок книги подписок не меняется."""
    guests = [_guest(11, "б"), _vpn_guest(22, "в"), _guest(33, "а"), _vpn_guest(44, "г")]
    assert [g.chat_id for g in vpn_admin_view.sort_guests(guests)] == [22, 44, 11, 33]


def test_in_vpn_group_counts_partial_and_wildcards():
    group = guest_rights.group("vpn")
    assert vpn_admin_view.in_vpn_group(_vpn_guest(1))
    # Одного права хватает: группу могли доправить руками или снять лишнее.
    assert vpn_admin_view.in_vpn_group(_guest(2, rights=frozenset({"usage@vpn"})))
    # Групповые формы из конфига тоже считаются.
    assert vpn_admin_view.in_vpn_group(_guest(3, rights=frozenset({"*@vpn"})))
    assert vpn_admin_view.in_vpn_group(_guest(4, rights=frozenset({"*"})))
    assert not vpn_admin_view.in_vpn_group(_guest(5, rights=frozenset({"chat@llm"})))
    assert not vpn_admin_view.in_vpn_group(_guest(6))
    assert group is not None  # каталог на месте — иначе тест бессмысленен


def test_guests_view_paginates():
    guests = [_guest(i) for i in range(1, 20)]
    _text, kb = vpn_admin_view.build_guests_view(guests, {}, 0)
    assert vpn_admin_view.guests_cb(vpn_admin_view.GUEST_PAGE_SIZE) in _callbacks(kb)


# --- карточка гостя --------------------------------------------------------


def test_server_name_is_flag_alone_when_label_has_one():
    """Флаг уже называет страну — слово рядом с ним ничего не добавляет."""
    assert vpn_admin_view.server_name(_server()) == "🇳🇱"
    assert vpn_admin_view.server_name(_server(label="🇺🇸 США")) == "🇺🇸"


def test_server_name_keeps_label_without_flag():
    # Сокращать нечего — показываем как есть; совсем без метки остаётся id ноды.
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
    """Локаций нет — но умения гостя правятся и без связи с роем: они живут
    в подписке бота, а не в службе."""
    text, kb = vpn_admin_view.build_guest_view(_guest(11), [])
    assert "не на связи" in text
    assert _callbacks(kb) == [vpn_admin_view.rights_cb(11), vpn_admin_view.guests_cb(0)]


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


# --- экран умений гостя (тонкие права, 2026-09-24) --------------------------


def test_rights_view_marks_full_partial_and_empty():
    """Три состояния тумблера. Неполный набор — не выдумка: гости, впущенные
    до появления `vpn_card@vpn` в группе, живут с одним `usage@vpn`."""
    guest = _guest(11, rights=frozenset({"usage@vpn"}))
    _text, kb = vpn_admin_view.build_rights_view(guest)
    marks = {text.split(" ", 1)[0] for text in _texts(kb) if text[0] in "✅🔸⬜"}
    assert marks == {"🔸", "⬜"}  # «видеть карточку» неполон, остальное пусто


def test_rights_view_shows_owner_wildcard_as_enabled():
    """Владельцу с `*` экран показывает выданным всё, а не пустые квадратики."""
    owner = Subscription(chat_id=1, name="admin", allowed_commands=frozenset({"*"}))
    for toggle in vpn_admin_view.VPN_TOGGLES:
        assert vpn_admin_view.toggle_state(owner, toggle)


def test_toggle_fills_incomplete_set_before_removing_it():
    """Неполный комплект дожимается, а не снимается (как «Добавить право» в
    /guests): половина умения — это поломка, её чинят, а не добивают."""
    toggle = vpn_admin_view.toggle_by_key("dev")
    assert toggle is not None
    partial = _guest(11, rights=frozenset({"issue@vpn"}))
    filled = vpn_admin_view.toggle_rights(partial, toggle)
    assert toggle.rights <= filled

    full = _guest(11, rights=filled)
    assert vpn_admin_view.toggle_state(full, toggle)
    assert not (vpn_admin_view.toggle_rights(full, toggle) & toggle.rights)


def test_toggle_touches_only_its_own_rights():
    """Тумблер VPN не должен задевать права других служб."""
    toggle = vpn_admin_view.toggle_by_key("proxy")
    assert toggle is not None
    guest = _guest(11, rights=frozenset({"chat@llm", "proxy_link@vpn"}))
    assert vpn_admin_view.toggle_rights(guest, toggle) == frozenset({"chat@llm"})


def test_proxy_toggle_is_not_part_of_the_vpn_group():
    """Прокси — не VPN: выдача группы «📶 VPN» в /guests его не приносит,
    он открывается точечно здесь (решение пользователя 2026-09-24)."""
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
    assert "Прокси Telegram" not in text  # группа его не даёт


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
