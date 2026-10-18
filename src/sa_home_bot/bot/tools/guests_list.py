from __future__ import annotations

from typing import Any

from sa_home_bot.bot import (
    commands,
)
from sa_home_bot.bot.tools.core import (
    CommandRight,
    ToolContext,
    ToolSpec,
    bounded_limit,
    people_of,
    text_arg,
)

DEFAULT_GUESTS_LIST_LIMIT = 30
MAX_GUESTS_LIST_LIMIT = 100


async def tool_guests_list(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.book is None:
        return "недоступно: список гостей сейчас не виден"
    all_guests = ctx.book.guests()
    guests = all_guests
    right = text_arg(args, "right")
    if right:
        if "@" in right:
            guests = [g for g in guests if g.allows_command(right)]
        else:
            suffix = f"@{right}"
            guests = [
                g
                for g in guests
                if g.allows_command("*") or any(c.endswith(suffix) for c in g.allowed_commands)
            ]
    if not guests:
        return "гостей с такими условиями нет"

    offset_raw = args.get("offset")
    try:
        offset = max(0, int(offset_raw)) if offset_raw is not None else 0
    except (TypeError, ValueError):
        return f"offset должен быть числом: {offset_raw!r}"
    limit = bounded_limit(args.get("limit"), DEFAULT_GUESTS_LIST_LIMIT, MAX_GUESTS_LIST_LIMIT)
    if isinstance(limit, str):
        return limit

    guests_sorted = sorted(guests, key=lambda s: s.invited_at)
    page = guests_sorted[offset : offset + limit]
    lines = [f"Гостей: {len(all_guests)} (после фильтра: {len(guests)})"]
    if not page:
        lines.append(f"offset {offset} за пределами списка — всего подходит {len(guests)}")
        return "\n".join(lines)
    lines[0] += f", показаны {offset + 1}-{offset + len(page)}"
    people = people_of(ctx)
    for g in page:
        shown = people.display(g.chat_id) or g.name
        in_shown = {(people.name(g.chat_id) or "").casefold(), f"@{people.username(g.chat_id)}"}
        aliases = [
            label
            for _kind, label in people.labels(g.chat_id)
            if label.casefold() not in {x.casefold() for x in in_shown}
        ]
        extra = f", ещё: {', '.join(aliases)}" if aliases else ""
        lines.append(
            f"• {shown} (chat_id {g.chat_id}{extra}) — прав: {len(g.allowed_commands)}"
        )
    next_offset = offset + len(page)
    if next_offset < len(guests):
        lines.append(
            f"Это не все — ещё {len(guests) - next_offset}. Чтобы показать "
            f"остальных, вызови guests_list снова с offset={next_offset}."
        )
    return "\n".join(lines)


_DECL_GUESTS_LIST: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "guests_list",
        "description": (
            "Твой личный справочник приглашённых гостей: имя, chat_id и число "
            "выданных прав. Ищешь конкретного человека (по имени, нику, «мой "
            "брат») — это find_person, а не этот список; справочник — чтобы "
            "перечислить гостей (сами права поимённо он не показывает, right — "
            "фильтр, а не перечень). Доступен "
            "только владельцу — если тул тебе виден, значит спрашивает "
            "именно он; не пересказывай этот справочник в чужом чате. "
            "right — точная строка права (например 'chat@llm', "
            "'recall@memory') — если задано, оставляет только гостей с этим "
            "правом (узнать, у кого есть конкретное право); можно передать и "
            "голое имя службы без действия (например 'vpn') — тогда "
            "оставляет гостей хоть с каким-то правом на эту службу. "
            "За один вызов отдаёт страницу (по умолчанию до 30 гостей) — "
            "перечисли в ответе ВСЕХ, кто попал в страницу, не выбирай сам "
            "часть из них. Если в конце результата есть строка «Это не "
            "все» — гостей больше, чем показано: обязательно скажи об этом "
            "владельцу (сколько ещё) вместо того чтобы промолчать, и вызови "
            "тул снова с указанным offset, если владелец хочет увидеть "
            "остальных."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "right": {
                    "type": "string",
                    "description": (
                        "Строка права для фильтра — точная 'action@service' "
                        "(например 'chat@llm') или голое имя службы без "
                        "действия (например 'vpn' — любое право на VPN)"
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "С какого гостя начать страницу (по умолчанию 0) — "
                        "используй значение из «Это не все», чтобы показать "
                        "следующих."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Сколько гостей на страницу (по умолчанию "
                        f"{DEFAULT_GUESTS_LIST_LIMIT}, максимум "
                        f"{MAX_GUESTS_LIST_LIMIT})."
                    ),
                },
            },
            "required": [],
        },
    },
}


GUESTS_LIST = ToolSpec(
    name="guests_list",
    handler=tool_guests_list,
    declaration=_DECL_GUESTS_LIST,
    requires=CommandRight(commands.required_right(commands.GUESTS.name)),
    telegram_only=True,
)
