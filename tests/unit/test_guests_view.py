"""guests_view: рендер иерархии /guests — чистые функции из готовых данных
(bot/handlers/invites.py::on_guest_screen делает сетевые вызовы и запись
состояния, сюда не относится)."""

from sa_home_bot.bot import commands, guest_rights, guests_view
from sa_home_bot.subscriptions.models import SOURCE_GUEST, Subscription


def _guest(
    chat_id: int,
    name: str = "Гость",
    rights: frozenset[str] = frozenset(),
    family: bool = False,
) -> Subscription:
    return Subscription(
        name=name,
        chat_id=chat_id,
        allowed_commands=rights,
        source=SOURCE_GUEST,
        invited_at="2026-08-01T10:00:00+00:00",
        family=family,
    )


def _callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# --- список гостей ---------------------------------------------------------


def test_list_view_paginates_and_links_cards():
    total = guests_view.GUEST_PAGE_SIZE + 2
    guests = [_guest(i) for i in range(total)]
    text, kb = guests_view.build_list_view(guests, 0)
    callbacks = _callbacks(kb)

    card_callbacks = [c for c in callbacks if c.startswith(f"st:{commands.GUEST_CARD_CODE}:")]
    assert len(card_callbacks) == guests_view.GUEST_PAGE_SIZE
    # «Следующая страница» уносит на offset = размер страницы.
    assert f"st:{commands.GUESTS_LIST_CODE}:{guests_view.GUEST_PAGE_SIZE}" in callbacks
    assert str(total) in text  # счётчик в заголовке


def test_list_view_second_page_has_only_prev_button():
    total = guests_view.GUEST_PAGE_SIZE + 2
    guests = [_guest(i) for i in range(total)]
    _, kb = guests_view.build_list_view(guests, guests_view.GUEST_PAGE_SIZE)
    callbacks = _callbacks(kb)
    assert f"st:{commands.GUESTS_LIST_CODE}:0" in callbacks
    next_offset = guests_view.GUEST_PAGE_SIZE * 2
    assert not any(
        c.startswith(f"st:{commands.GUESTS_LIST_CODE}:") and c.endswith(f":{next_offset}")
        for c in callbacks
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


def _button_texts(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row]


def test_card_view_shows_family_toggle():
    not_family = _guest(77, family=False)
    text, kb = guests_view.build_card_view(not_family)
    callbacks = _callbacks(kb)
    assert f"st:{commands.GUEST_FAMILY_CODE}:77" in callbacks
    assert "Семья: нет" in text
    assert any("Сделать членом семьи" in t for t in _button_texts(kb))

    in_family = _guest(77, family=True)
    text, kb = guests_view.build_card_view(in_family)
    assert "Семья: да" in text
    assert any("Исключить из семьи" in t for t in _button_texts(kb))


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
    sub = _guest(77, rights=guest_rights.CATALOG_RIGHTS)
    text, kb = guests_view.build_perm_add_view(sub, 0)
    assert "уже выданы" in text
    # Кроме «Назад», выдавать больше нечего.
    assert _callbacks(kb) == [f"st:{commands.GUEST_PERMS_CODE}:77:0"]


# --- группы прав ---------------------------------------------------------


def _vpn_group() -> guest_rights.RightGroup:
    group = guest_rights.group("vpn")
    assert group is not None
    return group


def test_perms_view_collapses_service_rights_into_one_group_row():
    group = _vpn_group()
    sub = _guest(77, rights=group.rights | {"chat@llm"})
    text, kb = guests_view.build_perms_view(sub, 0)
    callbacks = _callbacks(kb)
    # Семь прав VPN — одна строка и одна кнопка снятия на всю группу.
    assert text.count(group.label) == 1
    assert f"st:{commands.GUEST_GROUP_OFF_CODE}:77:0:vpn" in callbacks
    assert not any(c.endswith(":issue@vpn") for c in callbacks)
    # Одиночное право осталось само по себе.
    assert f"st:{commands.GUEST_PERM_OFF_CODE}:77:0:chat@llm" in callbacks


def test_perms_view_shows_fraction_for_partial_group():
    sub = _guest(77, rights=frozenset({"usage@vpn", "issue@vpn"}))
    text, kb = guests_view.build_perms_view(sub, 0)
    assert f"(2/{len(_vpn_group().rights)})" in text
    # Снимается всё равно целиком.
    assert f"st:{commands.GUEST_GROUP_OFF_CODE}:77:0:vpn" in _callbacks(kb)


def test_perm_add_view_offers_group_and_hides_its_members():
    sub = _guest(77)
    text, kb = guests_view.build_perm_add_view(sub, 0)
    callbacks = _callbacks(kb)
    assert f"st:{commands.GUEST_GROUP_ADD_CODE}:77:0:vpn" in callbacks
    assert not any(c.endswith(":usage@vpn") for c in callbacks)
    # Подсказка ведёт туда, где настраиваются нюансы доступа.
    assert "/vpn" in text


def test_perm_add_view_still_offers_partially_granted_group():
    sub = _guest(77, rights=frozenset({"usage@vpn"}))
    assert f"st:{commands.GUEST_GROUP_ADD_CODE}:77:0:vpn" in _callbacks(
        guests_view.build_perm_add_view(sub, 0)[1]
    )


def test_granted_rows_counts_group_as_one():
    group = _vpn_group()
    sub = _guest(77, rights=group.rights | {"chat@llm"})
    assert guests_view.granted_rows(sub) == 2


def test_group_of_finds_owning_group():
    assert guest_rights.group_of("issue@vpn") is _vpn_group()
    assert guest_rights.group_of("chat@llm") is None
    assert guest_rights.group_of("peers@vpn") is None


def test_group_member_label_carries_service_prefix():
    # В общем списке прав «перевыпустить» без службы не читается.
    assert guest_rights.label("reissue@vpn") == "📶 VPN: перевыпустить"


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
    # VPN-действия и invite/guests (AUTHORIZATION.md §10.4) — ни точечно, ни
    # группой такое гостю через кнопку не выдаётся.
    forbidden = (
        "restart@node",
        "poweroff@node",
        "peers@vpn",
        "resolve_request@vpn",
        "set_quota@vpn",
        "set_access@vpn",
        "invite",
        "guests",
        "*",
        "*@vpn",
    )
    for right in forbidden:
        assert right not in guest_rights.CATALOG_RIGHTS


def test_guest_groups_do_not_overlap_with_single_rights():
    singles = {r.right for r in guest_rights.GUEST_RIGHTS}
    seen: set[str] = set()
    for group in guest_rights.GUEST_GROUPS:
        assert not group.rights & singles, f"{group.key} дублирует одиночное право"
        assert not group.rights & seen, f"{group.key} пересекается с другой группой"
        seen |= group.rights
