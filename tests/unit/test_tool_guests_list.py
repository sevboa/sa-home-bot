"""Тул guests_list: справочник гостей для владельца (право invite, как у
самой команды /guests — см. bot/guest_rights.py, гостям это право точечно
не выдаётся)."""

from __future__ import annotations

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.config import GuestSubscriptionConfig, Settings, SubscriptionConfig
from sa_home_bot.subscriptions.book import SubscriptionBook

OWNER_CHAT = 1
NATASHA_CHAT = 77
IGOR_CHAT = 88


def _book() -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="me", chat_id=OWNER_CHAT, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name="Наташа",
                chat_id=NATASHA_CHAT,
                allowed_commands=["chat@llm", "recall@memory"],
                family=True,
            ),
            GuestSubscriptionConfig(
                name="Игорь",
                chat_id=IGOR_CHAT,
                allowed_commands=["chat@llm"],
                family=False,
            ),
        ],
    )


def _ctx(**over):
    defaults = dict(
        chat_id=OWNER_CHAT,
        dialogue_id=10,
        trigger_message_id=10,
        settings=Settings(),
        book=_book(),
    )
    defaults.update(over)
    return ai_tools.ToolContext(**defaults)


# --- права -----------------------------------------------------------------


def test_guests_list_requires_invite_right():
    owner = _book().for_chat(OWNER_CHAT)
    guest = _book().for_chat(NATASHA_CHAT)
    assert "guests_list" in ai_tools.tools_for(owner).handlers
    assert "guests_list" not in ai_tools.tools_for(guest).handlers
    assert "guests_list" not in ai_tools.tools_for(None).handlers


# --- сам тул -----------------------------------------------------------------


async def test_guests_list_shows_all_by_default():
    result = await ai_tools.tool_guests_list(_ctx(), {})
    assert "Наташа" in result
    assert "Игорь" in result
    assert "Гостей: 2" in result


async def test_guests_list_filters_by_right():
    result = await ai_tools.tool_guests_list(_ctx(), {"right": "recall@memory"})
    assert "Наташа" in result
    assert "Игорь" not in result


async def test_guests_list_filters_by_family_yes():
    result = await ai_tools.tool_guests_list(_ctx(), {"family": "yes"})
    assert "Наташа" in result
    assert "Игорь" not in result


async def test_guests_list_filters_by_family_no():
    result = await ai_tools.tool_guests_list(_ctx(), {"family": "no"})
    assert "Игорь" in result
    assert "Наташа" not in result


async def test_guests_list_empty_after_filter_says_so():
    result = await ai_tools.tool_guests_list(_ctx(), {"right": "usage@vpn"})
    assert "нет" in result.lower()


async def test_guests_list_unavailable_without_book():
    ctx = ai_tools.ToolContext(
        chat_id=OWNER_CHAT, dialogue_id=1, trigger_message_id=1, settings=Settings()
    )
    result = await ai_tools.tool_guests_list(ctx, {})
    assert result.startswith("недоступно")


# --- компактность и пагинация (живая находка 2026-08-06: без этого модель
# сама обрезала пересказ до нескольких гостей и не говорила, что список
# неполный) -------------------------------------------------------------


async def test_guests_list_is_always_compact_no_right_labels():
    result = await ai_tools.tool_guests_list(_ctx(), {})
    assert "прав: 2" in result  # Наташа: chat@llm, recall@memory
    assert "Разговор с Альфредом" not in result

    filtered = await ai_tools.tool_guests_list(_ctx(), {"right": "chat@llm"})
    assert "Разговор с Альфредом" not in filtered
    assert "прав: 1" in filtered  # Игорь: только chat@llm


async def test_guests_list_pagination_notes_when_more_remain():
    result = await ai_tools.tool_guests_list(_ctx(), {"limit": 1})
    assert "Наташа" in result
    assert "Игорь" not in result
    assert "Это не все" in result
    assert "offset=1" in result


async def test_guests_list_pagination_second_page():
    result = await ai_tools.tool_guests_list(_ctx(), {"limit": 1, "offset": 1})
    assert "Игорь" in result
    assert "Наташа" not in result
    assert "Это не все" not in result


async def test_guests_list_no_pagination_note_when_all_shown():
    result = await ai_tools.tool_guests_list(_ctx(), {})
    assert "Это не все" not in result


async def test_guests_list_works_from_self_scheduled_remind_without_notifier():
    """Служба tasks (self-scheduled remind, живая находка 2026-08-06) не
    даёт notifier/store вовсе, но свою SubscriptionBook теперь передаёт (см.
    tasks/service.py::_fire_chat_loop) — только книга и нужна, тул читает
    только конфиг подписок, а не живой Telegram."""
    ctx = ai_tools.ToolContext(
        chat_id=OWNER_CHAT,
        dialogue_id=1,
        trigger_message_id=1,
        settings=Settings(),
        book=_book(),
        notifier=None,
        store=None,
    )
    result = await ai_tools.tool_guests_list(ctx, {})
    assert "Наташа" in result
    assert "Игорь" in result
