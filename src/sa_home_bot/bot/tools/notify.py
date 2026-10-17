from __future__ import annotations

import random
from html import escape
from typing import Any

from sa_home_bot.bot import (
    commands,
    recipients,
)
from sa_home_bot.bot.tools.core import CommandRight, ToolContext, ToolSpec, text_arg
from sa_home_bot.bot.tools.delivery import (
    NEED_RECIPIENT_ID,
    deliver_personal_message,
    parse_recipient_id,
)

_NOTIFY_PERSONAS: tuple[dict[str, str], ...] = (
    {
        "title": "Владелец",
        "emoji": "🏠",
        "style_prompt": (
            "Нейтральный, обычный деловой тон от лица владельца дома — просто "
            "и по-человечески, без вычурности и ролевой игры."
        ),
    },
    {
        "title": "Хозяин",
        "emoji": "🧰",
        "style_prompt": (
            "Тон рачительного хозяина-хозяйственника: по-деловому, чуть "
            "ворчливо про порядок и хозяйство — как человек, который сам всё "
            "чинит, следит за расходами и не любит бардак. Обычная "
            "разговорная речь, можно вставить хозяйственную лексику "
            "(«приведём в порядок», «учтено», «по-хозяйски», «не разбрасывай»)."
        ),
    },
    {
        "title": "Админ",
        "emoji": "🤓",
        "style_prompt": (
            "Тон айтишника-технаря: сухо, по пунктам, с лёгким канцеляритом и "
            "техническим жаргоном («по протоколу», «статус», «зафиксировано»). "
            "Уместно закончить короткой технической подписью вроде «// конец "
            "уведомления»."
        ),
    },
    {
        "title": "Граф",
        "emoji": "🦇",
        "style_prompt": (
            "Тон аристократа-вампира вроде графа Дракулы: старомодная, чуть "
            "зловещая, витиеватая речь, архаизмы («приветствую, о смертный», "
            "«засим откланиваюсь»), готическая атмосфера — но это стилизация "
            "для колорита, не всерьёз пугать адресата."
        ),
    },
)
_NOTIFY_PERSONAS_BY_TITLE = {p["title"]: p for p in _NOTIFY_PERSONAS}


def _resolve_persona(raw: str) -> dict[str, str] | None:
    """Найти персонажа по значению persona из notify_guest.

    Терпимо к лишнему вокруг названия: модель иногда копирует persona не
    отдельным словом, а вместе с эмодзи или другим текстом из ответа
    notify_persona (живой баг 2026-08-05 — "Граф 🦇" вместо "Граф"), точное
    совпадение по словарю такое не находило.
    """
    normalized = raw.strip()
    if normalized in _NOTIFY_PERSONAS_BY_TITLE:
        return _NOTIFY_PERSONAS_BY_TITLE[normalized]
    for persona in _NOTIFY_PERSONAS:
        if persona["title"].casefold() in normalized.casefold():
            return persona
    return None


async def tool_notify_persona(ctx: ToolContext, args: dict[str, Any]) -> str:
    persona = random.choice(_NOTIFY_PERSONAS)
    return (
        f"Персонаж: {persona['title']}\n"
        f"Эмодзи персонажа (это для контекста, НЕ значение persona): "
        f"{persona['emoji']}\n"
        f"Стиль: {persona['style_prompt']}\n"
        f"{persona['title']} — своя, отдельная от Альфреда личность (не "
        f"«Альфред временно изображает {persona['title']}а», а именно "
        f"{persona['title']} как таковой). Пиши text от первого лица как сам "
        f"{persona['title']}, не выдавай себя ЗА Альфреда и не отождествляй "
        f"себя с ним в тексте — упоминать Альфреда как отдельного, другого "
        f"персонажа (например «это передал Альфред» или что-то в этом духе) "
        f"можно, если уместно по смыслу, просто не путай два лица в одно.\n"
        f"Напиши текст уведомления в этом стиле и вызови notify_guest с "
        f'persona="{persona["title"]}" — ровно этим словом, без эмодзи и без '
        f"кавычек внутри значения."
    )


_DECL_NOTIFY_PERSONA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "notify_persona",
        "description": (
            "Первый и ОБЯЗАТЕЛЬНЫЙ шаг перед notify_guest — всегда, даже "
            "если владелец сам назвал персонажа словами ('от админа', 'как "
            "граф') — персонаж ВСЕГДА выбирается случайно этим тулом, его "
            "пожелание не подставляй напрямую в notify_guest в обход "
            "notify_persona. Тул сам случайно выбирает персонажа-«отправителя» "
            "официального уведомления и возвращает промт — КАК писать текст "
            "в его манере. Вызови его РОВНО ОДИН РАЗ НА КАЖДОГО получателя, "
            "которому собираешься отправить уведомление notify_guest — если "
            "уведомляешь нескольких гостей подряд, зови notify_persona "
            "заново для каждого, не переиспользуй один и тот же результат: "
            "иначе у всех окажется один и тот же персонаж вместо случайного "
            "у каждого. После этого сам сочини текст уведомления, следуя "
            "полученному промту, и передай его в notify_guest вместе с "
            "именем персонажа как есть."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def render_notify(persona: dict[str, str], text: str) -> str:
    body = escape(text.strip())
    title = escape(persona["title"])
    return f"{persona['emoji']} <b>{title} (официальное уведомление):</b>\n\n{body}"


async def tool_notify_guest(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.book is None or (ctx.notifier is None and ctx.emit is None):
        return "недоступно: сейчас я не могу никому написать"
    if ctx.chat_id is None:
        return "недоступно: непонятно, откуда уведомлять"
    recipient_id = parse_recipient_id(args)
    text = text_arg(args, "text")
    persona = _resolve_persona(str(args.get("persona") or ""))
    if recipient_id is None:
        return NEED_RECIPIENT_ID
    if not text:
        return "ошибка: не сказано, что передать (text)"
    if persona is None:
        return "ошибка: сначала вызови notify_persona и передай сюда его persona как есть"

    def render(_target: recipients.Recipient) -> str:
        return render_notify(persona, text)

    return await deliver_personal_message(ctx, recipient_id, text, render, allow_self=True)


_DECL_NOTIFY_GUEST: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "notify_guest",
        "description": (
            "Официальное уведомление гостю — от лица администрации дома, БЕЗ "
            "пометки «по просьбе владельца». Это инструмент только владельца, "
            "и он про ДРУГОЕ, чем tell: используй его, когда владелец прямо "
            "просит что-то ОБЪЯВИТЬ/УВЕДОМИТЬ/ПРЕДУПРЕДИТЬ гостя официально "
            "(«объяви», «предупреди», «уведоми», «разошли объявление») — а не "
            "когда просит что-то ПЕРЕДАТЬ лично от себя («скажи», «спроси», "
            "«передай») — для этого есть tell. Если из разговора неясно, "
            "хочет ли владелец сказать лично от себя (как Алексей, tell) или "
            "объявить официально (как владелец, notify_guest) — спроси "
            "прямо: «сказать как лично от тебя или как официальное "
            "уведомление?», не выбирай сам. Получатель — по recipient_id из "
            "find_person; им может быть и сам владелец (например "
            "self-напоминание в стиле персонажа). "
            "persona ВСЕГДА случайный: ОБЯЗАТЕЛЬНО сначала вызови "
            "notify_persona (на каждого получателя заново) и напиши text в "
            "его стиле — persona здесь передай ровно тем же значением, что "
            "он вернул, даже если владелец сам называл персонажа словами."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_id": {
                    "type": "integer",
                    "description": "Telegram id из find_person (может быть и сам владелец)",
                },
                "persona": {
                    "type": "string",
                    "enum": [p["title"] for p in _NOTIFY_PERSONAS],
                    "description": "Персонаж, ровно как вернул notify_persona",
                },
                "text": {
                    "type": "string",
                    "description": (
                        "Текст уведомления от первого лица персонажа — он не "
                        "выдаёт себя за Альфреда, это своя, отдельная от "
                        "Альфреда личность (упоминать Альфреда как ДРУГОГО "
                        "персонажа по смыслу можно)"
                    ),
                },
            },
            "required": ["recipient_id", "persona", "text"],
        },
    },
}


NOTIFY_PERSONA = ToolSpec(
    name="notify_persona",
    handler=tool_notify_persona,
    declaration=_DECL_NOTIFY_PERSONA,
    requires=CommandRight(commands.required_right(commands.GUESTS.name)),
)

NOTIFY_GUEST = ToolSpec(
    name="notify_guest",
    handler=tool_notify_guest,
    declaration=_DECL_NOTIFY_GUEST,
    requires=CommandRight(commands.required_right(commands.GUESTS.name)),
    telegram_only=True,
)
