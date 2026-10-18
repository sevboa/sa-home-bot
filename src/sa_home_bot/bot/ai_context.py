from __future__ import annotations

import json
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram.types import Message, User

from sa_home_bot.bot import people_cards
from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.service_errors import UNREACHABLE_ERRORS
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.db.store import Store
from sa_home_bot.graph_memory import protocol as graph_memory_protocol
from sa_home_bot.memory import protocol as memory_protocol
from sa_home_bot.people import claims as people_claims
from sa_home_bot.people.book import PeopleBook
from sa_home_bot.proto.addresses import GRAPH_MEMORY_ADDRESS, MEMORY_ADDRESS
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.utils.timefmt import zone_or_none

log = logging.getLogger(__name__)

LENGTH_NOTE = (
    "Объём ответа соразмеряй с репликой собеседника, а не со своими прошлыми "
    "ответами: на короткую реплику — одна-три фразы; развёрнуто — только если "
    "об этом просят или без этого не обойтись."
)
ALFRED_TIMEZONE = "Europe/Bucharest"

MEMORY_RECALL_LIMIT = 3
MEMORY_FACT_CHARS = 160
MEMORY_TIMEOUT_S = 5.0


GRAPH_MEMORY_TIMEOUT_S = 3.0
GRAPH_MEMORY_RECALL_LIMIT = 3
GRAPH_MEMORY_FACT_CHARS = 160


async def _recall_result(
    node_link: ServiceLink,
    chat_id: int | None,
    text: str,
    *,
    action: str,
    dst,
    args: dict,
    timeout: float,
    unavailable_log: str,
) -> dict | None:
    if chat_id is None or not text.strip():
        return None
    try:
        return await node_link.command(
            action, {"query": text, "chat_id": chat_id, **args}, dst=dst, timeout=timeout
        )
    except UNREACHABLE_ERRORS as exc:
        log.debug(unavailable_log, exc)
        return None


PERSONAL_FACT_MARK = " [личное: знай, но вслух не пересказывай]"


async def recall_facts(node_link: ServiceLink, chat_id: int | None, text: str) -> list[str]:
    result = await _recall_result(
        node_link,
        chat_id,
        text,
        action=memory_protocol.ACTION_RECALL,
        dst=MEMORY_ADDRESS,
        args={"limit": MEMORY_RECALL_LIMIT},
        timeout=MEMORY_TIMEOUT_S,
        unavailable_log="ai_context: память не ответила: %s",
    )
    facts: list[str] = []
    for fact in (result or {}).get("facts", []):
        text_value = str(fact.get("text", ""))[:MEMORY_FACT_CHARS]
        if not text_value:
            continue
        facts.append(text_value + PERSONAL_FACT_MARK if fact.get("sensitive") else text_value)
    return facts


async def recall_graph_facts(node_link: ServiceLink, chat_id: int | None, text: str) -> list[str]:
    result = await _recall_result(
        node_link,
        chat_id,
        text,
        action=graph_memory_protocol.ACTION_SEARCH,
        dst=GRAPH_MEMORY_ADDRESS,
        args={"limit": GRAPH_MEMORY_RECALL_LIMIT},
        timeout=GRAPH_MEMORY_TIMEOUT_S,
        unavailable_log="ai_context: графовая память не ответила: %s",
    )
    return [str(fact)[:GRAPH_MEMORY_FACT_CHARS] for fact in (result or {}).get("facts", []) if fact]


REPLIED_PICTURE_NOTE = (
    "Собеседник отвечает на твою картинку #{id} «{caption}». Ты её не видишь, "
    "пока не посмотришь: о деталях — сначала look_at_photo (whose=mine).{seen}"
)
REPLIED_PICTURE_SEEN = " Когда ты её отправлял, на ней было видно: {seen}"


async def replied_picture_note(message: Message, store: Store | None) -> str | None:
    if store is None or message.chat is None or message.reply_to_message is None:
        return None
    image = await store.alfred_image(
        message.chat.id, message_id=message.reply_to_message.message_id
    )
    if image is None:
        return None
    seen = ""
    try:
        params = json.loads(image.get("params") or "{}")
    except (TypeError, ValueError):
        params = {}
    if isinstance(params, dict) and isinstance(params.get("seen"), str) and params["seen"]:
        seen = REPLIED_PICTURE_SEEN.format(seen=params["seen"])
    return REPLIED_PICTURE_NOTE.format(id=image["id"], caption=image["caption"], seen=seen)


def display_name(user: User | None) -> str | None:
    if user is None:
        return None
    name = user.full_name
    return f"{name} (@{user.username})" if user.username else name


def _person_age(birth_date: str | None, today: date) -> int | None:
    if not birth_date:
        return None
    try:
        birth = date.fromisoformat(birth_date)
    except ValueError:
        return None
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


_MONTHS_RU_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _person_birthday_ru(birth_date: str | None) -> str | None:
    if not birth_date:
        return None
    try:
        birth = date.fromisoformat(birth_date)
    except ValueError:
        return None
    return f"{birth.day} {_MONTHS_RU_GENITIVE[birth.month - 1]}"


_PRONOUNS = {
    "m": ("он", "него", "его"),
    "f": ("она", "неё", "её"),
    None: ("собеседник", "собеседника", "его/её"),
}


def _speaker_life_note(people: PeopleBook, person_id: int) -> str | None:
    gender = people.gender(person_id)
    subject, prepositional, possessive = _PRONOUNS.get(gender, _PRONOUNS[None])
    parts: list[str] = []
    tz = people.value(person_id, people_claims.FIELD_TIMEZONE)
    if tz:
        zone = zone_or_none(tz)
        if zone is not None:
            local_now = datetime.now(zone)
            city = people.value(person_id, people_claims.FIELD_CITY)
            where = f" в {city}" if city else ""
            parts.append(
                f"У {prepositional}{where} сейчас "
                f"{local_now:%H:%M} ({ai_tools.WEEKDAYS_RU[local_now.weekday()]}) — "
                f"это {possessive} часовой пояс, а не пояс сервера выше; называй "
                "именно это время, если спросят, который час, или это иначе уместно."
            )
    birth_date = people.value(person_id, people_claims.FIELD_BIRTH_DATE)
    age = _person_age(birth_date, date.today())
    if age is not None:
        parts.append(f"Точный возраст ({subject}) сейчас: {age} лет.")
    birthday = _person_birthday_ru(birth_date)
    if birthday is not None:
        parts.append(f"День рождения ({possessive}): {birthday}.")
    return " ".join(parts) or None


_REPLY_QUOTE_MAX_CHARS = 800


def _cap_quote(text: str) -> str:
    if len(text) > _REPLY_QUOTE_MAX_CHARS:
        return text[:_REPLY_QUOTE_MAX_CHARS] + "…"
    return text


async def _reply_context_lines(message: Message, store: Store, dialogue_id: int) -> list[str]:
    reply_to = message.reply_to_message
    if reply_to is None:
        return []
    lines: list[str] = []

    known = await store.ai_turn(message.chat.id, reply_to.message_id) if message.chat else None
    is_own_thread = known is not None and known["dialogue_id"] == dialogue_id
    if not is_own_thread:
        text = reply_to.text or reply_to.caption
        if text:
            author = display_name(reply_to.from_user) or "неизвестный собеседник"
            text = _cap_quote(text)
            lines.append(f"Сообщение, на которое сейчас отвечают ({author}): «{text}».")

    quote = message.quote
    if quote is not None and quote.text:
        quote_text = _cap_quote(quote.text)
        if is_own_thread:
            lines.append(
                f"Отвечая тебе, собеседник выделил и процитировал фрагмент "
                f"именно ИЗ ТВОЕГО ЖЕ предыдущего сообщения в этом треде — "
                f"вот он, дословно: «{quote_text}». Если тебя просят повторить "
                f"или процитировать «то слово»/«то, на что я ответил» — речь "
                f"именно про этот фрагмент, не подбирай другое слово из своей "
                f"памяти о переписке."
            )
        else:
            lines.append(
                f"Отвечая, собеседник выделил и процитировал в сообщении "
                f"выше именно этот фрагмент, дословно: «{quote_text}». Если "
                f"тебя просят повторить или процитировать «то, на что я "
                f"ответил» — речь именно про этот фрагмент."
            )

    return lines


async def people_context_lines(
    message: Message,
    store: Store,
    book: SubscriptionBook | None,
    people: PeopleBook,
) -> list[str]:
    user = message.from_user
    if user is None:
        return []
    speaker_id = user.id
    names: dict[int, str] = {}
    sender_name = display_name(user)
    if sender_name:
        names[speaker_id] = sender_name
    participants: list[int] = []
    if message.chat is not None and message.chat.type in ("group", "supergroup"):
        for row in await store.chat_participants(message.chat.id):
            if row["user_id"] == speaker_id:
                continue
            participants.append(row["user_id"])
            names.setdefault(row["user_id"], row["user_name"])
    acquaintances: list[int] = []
    for row in await store.relationships_for(speaker_id, status="confirmed"):
        other = row["guest_b"] if row["guest_a"] == speaker_id else row["guest_a"]
        acquaintances.append(other)

    def name_of(person_id: int) -> str | None:
        return people.display(person_id) or names.get(person_id)

    lines: list[str] = []
    for note in (
        people_cards.speaker_card_note(people.card(speaker_id), name_of),
        _speaker_life_note(people, speaker_id),
    ):
        if note:
            lines.append(note)
    if participants:
        lines.append(
            "Участники этого чата: "
            + "; ".join(
                people_cards.roster_line(p, name_of(p) or "?", people.card(p), name_of)
                for p in participants
            )
            + "."
        )
    if acquaintances:
        lines.append(
            "Знакомые собеседника (им можно передавать сообщения, адресуй по id): "
            + "; ".join(
                people_cards.roster_line(a, name_of(a) or "?", people.card(a), name_of)
                for a in acquaintances
            )
            + "."
        )
    lines.append(
        "Порядок дома: письма ты передаёшь только между теми, кого представил "
        "друг другу сам, — знакомство через тебя (request_acquaintance). Это "
        "касается и родных: знают ли люди друг друга в жизни, не важно. Просят "
        "связать, познакомить или передать тому, кого нет среди знакомых "
        "собеседника, — открывай форму знакомства. Исключение одно: передать "
        "«владельцу»/«хозяину» по роли можно всегда."
    )
    return lines


NO_INTERNET_NOTE = (
    "Выхода в интернет у тебя сейчас нет — проверить ничего не "
    "можешь. Если спрашивают о свежем (новости, события последних "
    "лет, чей-то запуск/матч/курс, «что было вчера») — прямо скажи, "
    "что сейчас без интернета и потому не знаешь. НЕ придумывай "
    "события, даты, числа и названия и не выдавай за факт то, что "
    "помнишь: память могла устареть или подвести. Признаться в "
    "незнании — не стыдно, соврать — стыдно."
)

TOOL_FAILURE_NOTE = (
    "Если инструмент вернул отказ или ошибку (например, «не удалось», "
    "«не записал», «не умею», «недоступен», «не настроено») — это финальный "
    "ответ на твой запрос к нему, не повод придумать правдоподобные "
    "цифры/факты взамен. Если это было действие (записать, передать, "
    "отправить, запомнить) — оно НЕ сделано: не говори «записал», "
    "«передал», «зафиксировал». Скажи собеседнику как есть: не получилось, "
    "почему (если сказано) и что можно сделать дальше. Доложить хозяину, "
    "что поручение не исполнено, — долг дворецкого, а не извинение и не "
    "выход из роли."
)


def _server_time_note() -> str:
    now_local = datetime.now().astimezone()
    offset = now_local.strftime("%z")
    return (
        f"Точное время сейчас (пояс сервера, UTC{offset[:3]}:{offset[3:]}): "
        f"{now_local:%Y-%m-%d %H:%M} ({ai_tools.WEEKDAYS_RU[now_local.weekday()]}). "
        "Это ПОЯС СЕРВЕРА, не обязательно пояс собеседника — если тебя "
        "спросят, в каком поясе сейчас сам собеседник, и ниже нет отдельной "
        "строки про его/её местное время — честно скажи, что не знаешь, не "
        "додумывай."
    )


def _alfred_local_time_note() -> str:
    alfred_now = datetime.now(ZoneInfo(ALFRED_TIMEZONE))
    return (
        f"Отдельно: ТВОЁ (Альфреда) личное время сейчас — там, где ты живёшь: "
        f"{alfred_now:%H:%M} ({ai_tools.WEEKDAYS_RU[alfred_now.weekday()]}). Своё "
        "место жительства ты не скрываешь, если прямо спросят, где ты живёшь или "
        "который час у ТЕБЯ (не у собеседника и не пояс сервера выше) — называй "
        "именно это время; про сам город — см. свой характер."
    )


async def _group_thread_lines(
    message: Message, store: Store, dialogue_id: int, sender_name: str
) -> list[str]:
    lines: list[str] = []
    starter = await store.ai_turn(message.chat.id, dialogue_id)
    starter_name = starter.get("user_name") if starter else None
    if starter_name and starter_name != sender_name:
        lines.append(
            f"Этот разговор начал(а) {starter_name}, а сейчас в него отвечает "
            f"{sender_name} — не тот человек, кто начинал. Это чужой тред: не "
            f"отказывай в ответе, но веди себя чуть сдержаннее, чем со "
            f"{starter_name} — обратись к {sender_name} по имени и мягко "
            f"дай понять, что это разговор {starter_name} (например: «Это, "
            f"кажется, беседа {starter_name}, сэр/мадам — но извольте, чем "
            f"могу быть полезен?» или предложи начать свой отдельный "
            f"разговор), не подхватывай тему с тем же радушием, как с тем, "
            f"кто начал."
        )

    participants = await store.chat_participants(message.chat.id)
    others = [
        p["user_name"]
        for p in participants
        if p["user_name"] and p["user_name"] not in (sender_name, starter_name)
    ]
    if others:
        lines.append("В этом чате к тебе также обращались: " + ", ".join(others) + ".")
    return lines


def _memory_lines(memory_facts: list[str] | None, graph_facts: list[str] | None) -> list[str]:
    lines: list[str] = []
    if memory_facts:
        lines.append(
            "Из того, что ты помнишь про этот разговор и его людей: "
            + "; ".join(memory_facts)
            + ". Пользуйся, если к месту, но не зачитывай списком и не "
            "объявляй, что «сверился с памятью»."
        )
    if graph_facts:
        lines.append(
            "Дополнительно — связи, которые ты мог уловить между людьми и "
            "вещами (может быть неполным или устаревшим, используй как "
            "подсказку, а не как точный факт): " + "; ".join(graph_facts) + "."
        )
    return lines


async def build_context_note(
    message: Message,
    store: Store,
    dialogue_id: int,
    *,
    has_web_search: bool = True,
    memory_facts: list[str] | None = None,
    graph_facts: list[str] | None = None,
    people_lines: list[str] | None = None,
) -> str:
    lines = [_server_time_note()]
    if not has_web_search:
        lines.append(NO_INTERNET_NOTE)
    lines.append(TOOL_FAILURE_NOTE)
    lines.append(_alfred_local_time_note())

    sender_name = display_name(message.from_user)
    is_group = message.chat is not None and message.chat.type in ("group", "supergroup")
    if sender_name is not None:
        lines.append(f"Сейчас с тобой говорит: {sender_name}.")
    if people_lines:
        lines.extend(people_lines)
    if sender_name is not None and is_group:
        lines.extend(await _group_thread_lines(message, store, dialogue_id, sender_name))

    lines.extend(_memory_lines(memory_facts, graph_facts))

    lines.extend(await _reply_context_lines(message, store, dialogue_id))

    lines.append(
        "Это справка для тебя, не пересказывай её вслух — просто учти при ответе "
        "(например, обратиться по имени), если уместно."
    )
    return " ".join(lines)
