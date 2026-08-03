"""guests_view: рендер иерархии /guests — чистые функции из готовых данных
(bot/handlers/invites.py::on_guest_screen делает сетевые вызовы и запись
состояния, сюда не относится)."""

from sa_home_bot.bot import commands, guest_rights, guests_view
from sa_home_bot.subscriptions.models import SOURCE_GUEST, Subscription


def _guest(chat_id: int, name: str = "Гость", rights: frozenset[str] = frozenset()) -> Subscription:
    return Subscription(
        name=name,
        chat_id=chat_id,
        allowed_commands=rights,
        source=SOURCE_GUEST,
        invited_at="2026-08-01T10:00:00+00:00",
    )


def _callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# --- список гостей ---------------------------------------------------------


def test_list_view_paginates_and_links_cards():
    guests = [_guest(i) for i in range(7)]
    text, kb = guests_view.build_list_view(guests, 0)
    callbacks = _callbacks(kb)

    card_callbacks = [c for c in callbacks if c.startswith(f"st:{commands.GUEST_CARD_CODE}:")]
    assert len(card_callbacks) == guests_view.GUEST_PAGE_SIZE
    # «Следующая страница» уносит на offset = размер страницы.
    assert f"st:{commands.GUESTS_LIST_CODE}:{guests_view.GUEST_PAGE_SIZE}" in callbacks
    assert "7" in text  # счётчик в заголовке


def test_list_view_second_page_has_only_prev_button():
    guests = [_guest(i) for i in range(7)]
    _, kb = guests_view.build_list_view(guests, guests_view.GUEST_PAGE_SIZE)
    callbacks = _callbacks(kb)
    assert f"st:{commands.GUESTS_LIST_CODE}:0" in callbacks
    assert not any(
        c.startswith(f"st:{commands.GUESTS_LIST_CODE}:") and c.endswith(":10") for c in callbacks
    )


def test_list_view_empty_still_offers_open_codes():
    text, kb = guests_view.build_list_view([], 0)
    assert "Пока никого" in text
    assert f"st:{commands.OPEN_CODES_LIST_CODE}:0" in _callbacks(kb)


# --- открытые коды ---------------------------------------------------------


def test_codes_view_offers_revoke_and_back():
    rows = [{"code": "ABCD1234", "expires_at": "2026-08-01T10:00:00+00:00"}]
    text, kb = guests_view.build_codes_view(rows, 0)
    callbacks = _callbacks(kb)
    assert any(c.startswith(f"st:{commands.CODE_REVOKE_CODE}:ABCD1234") for c in callbacks)
    assert f"st:{commands.GUESTS_LIST_CODE}:0" in callbacks
    assert "ABCD-1234" in text  # печатается группами, как везде в боте


def test_codes_view_empty_says_so():
    text, _ = guests_view.build_codes_view([], 0)
    assert "нет" in text.lower()


# --- карточка гостя ---------------------------------------------------------


def test_card_view_has_perms_stats_kick_back():
    sub = _guest(77, rights=frozenset({"chat@llm"}))
    text, kb = guests_view.build_card_view(sub)
    callbacks = _callbacks(kb)
    assert f"st:{commands.GUEST_PERMS_CODE}:77:0" in callbacks
    assert f"st:{commands.GUEST_STATS_CODE}:77" in callbacks
    assert f"st:{commands.GUEST_KICK_CONFIRM_CODE}:77" in callbacks
    assert f"st:{commands.GUESTS_LIST_CODE}:0" in callbacks
    assert "77" in text


def test_kick_confirm_view_asks_before_final_callback():
    sub = _guest(77)
    text, kb = guests_view.build_kick_confirm_view(sub)
    callbacks = _callbacks(kb)
    # Финальный отзыв и отмена (назад к карточке) — разные callback'и: кнопка
    # «выставить» больше не бьёт мгновенно, как раньше, а спрашивает «точно?».
    assert f"st:{commands.GUEST_REVOKE_CODE}:77" in callbacks
    assert f"st:{commands.GUEST_CARD_CODE}:77" in callbacks
    assert "точно" in text.lower()


def test_back_to_card_view_wraps_body_with_back_button():
    text, kb = guests_view.build_back_to_card_view("Гость", 77, "📶 VPN: 0.0 / 100 ГБ")
    assert "VPN" in text
    assert _callbacks(kb) == [f"st:{commands.GUEST_CARD_CODE}:77"]


# --- права гостя -------------------------------------------------------


def test_perms_view_lists_rights_with_disable_buttons():
    sub = _guest(77, rights=frozenset({"chat@llm", "search@net"}))
    text, kb = guests_view.build_perms_view(sub, 0)
    callbacks = _callbacks(kb)
    assert guest_rights.label("chat@llm") in text
    assert f"st:{commands.GUEST_PERM_OFF_CODE}:77:0:chat@llm" in callbacks
    assert f"st:{commands.GUEST_PERM_OFF_CODE}:77:0:search@net" in callbacks
    assert f"st:{commands.GUEST_PERM_ADD_LIST_CODE}:77:0" in callbacks
    assert f"st:{commands.GUEST_CARD_CODE}:77" in callbacks


def test_perms_view_empty_says_no_rights():
    sub = _guest(77)
    text, _ = guests_view.build_perms_view(sub, 0)
    assert "Прав нет" in text


def test_perm_add_view_excludes_already_granted():
    sub = _guest(77, rights=frozenset({"chat@llm"}))
    text, kb = guests_view.build_perm_add_view(sub, 0)
    callbacks = _callbacks(kb)
    assert not any(c.endswith(":chat@llm") for c in callbacks)
    assert any(c.endswith(":search@net") for c in callbacks)
    assert f"st:{commands.GUEST_PERMS_CODE}:77:0" in callbacks  # назад


def test_perm_add_view_all_granted_says_so():
    sub = _guest(77, rights=frozenset(r.right for r in guest_rights.GUEST_RIGHTS))
    text, kb = guests_view.build_perm_add_view(sub, 0)
    assert "уже выданы" in text
    # Кроме «Назад», выдавать больше нечего.
    assert _callbacks(kb) == [f"st:{commands.GUEST_PERMS_CODE}:77:0"]


# --- пагинация ---------------------------------------------------------


def test_clamp_offset_falls_back_to_last_valid_page():
    assert guests_view.clamp_offset(12, 6, 7) == 6  # последняя страница из 7 начинается с 6
    assert guests_view.clamp_offset(0, 6, 3) == 0
    assert guests_view.clamp_offset(12, 6, 0) == 0  # пусто — всегда страница 0


# --- каталог прав ---------------------------------------------------------


def test_guest_right_label_falls_back_to_raw_string():
    assert guest_rights.label("chat@llm") != "chat@llm"
    assert guest_rights.label("unknown@thing") == "unknown@thing"


def test_guest_rights_catalog_has_unique_rights():
    rights = [r.right for r in guest_rights.GUEST_RIGHTS]
    assert len(rights) == len(set(rights))


def test_guest_rights_catalog_excludes_infrastructure_and_delegation():
    # Каталог осознанно не включает управление нодами/питанием, админские
    # VPN-действия и invite/guests (AUTHORIZATION.md §10.4) — точечно гостю
    # через кнопку такое не выдаётся.
    rights = {r.right for r in guest_rights.GUEST_RIGHTS}
    forbidden = (
        "restart@node",
        "poweroff@node",
        "peers@vpn",
        "resolve_request@vpn",
        "invite",
        "guests",
        "*",
    )
    for right in forbidden:
        assert right not in rights
