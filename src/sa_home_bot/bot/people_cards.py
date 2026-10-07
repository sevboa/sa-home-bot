"""Карточки людей в справке перед ходом (Этап 54.3): чтение из graph_memory
и текст для модели.

Карточку собирает служба (graph_memory/people.py::build_cards), здесь только
запрос и формулировки. Имён источников в графе нет — только by_id; имя
подставляет вызывающий из того, что знает бот (подписки, участники чата).
Недоступность mycraft — пустой словарь, а не ошибка: справка необязательна,
как и recall_graph_facts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.graph_memory import protocol as graph_memory_protocol
from sa_home_bot.proto.messages import Address, ProtoError

log = logging.getLogger(__name__)

# Как GRAPH_MEMORY_TIMEOUT_S в bot/ai_flow.py: спящий mycraft не должен
# заметно задерживать ответ.
PERSON_CARDS_TIMEOUT_S = 3.0

_GENDER_WORD = {
    graph_memory_protocol.GENDER_MALE: "мужчина",
    graph_memory_protocol.GENDER_FEMALE: "женщина",
}
_TITLE = {graph_memory_protocol.GENDER_MALE: "сэр", graph_memory_protocol.GENDER_FEMALE: "мадам"}

Card = dict[str, Any]
NameOf = Callable[[int], str | None]


async def fetch_person_cards(node_link: ServiceLink | None, ids: list[int]) -> dict[int, Card]:
    ids = sorted({i for i in ids if i})
    if node_link is None or not ids:
        return {}
    dst = Address(node=graph_memory_protocol.NODE_ID, service=graph_memory_protocol.SERVICE_NAME)
    try:
        result = await node_link.command(
            graph_memory_protocol.ACTION_PERSON_CARDS,
            {"ids": ",".join(str(i) for i in ids)},
            dst=dst,
            timeout=PERSON_CARDS_TIMEOUT_S,
        )
    except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
        log.debug("people_cards: карточки не получены: %s", exc)
        return {}
    cards: dict[int, Card] = {}
    for key, card in (result.get("cards") or {}).items():
        try:
            cards[int(key)] = card
        except (TypeError, ValueError):
            continue
    return cards


def card_gender(card: Card | None) -> str | None:
    claim = (card or {}).get("gender")
    return claim.get("value") if isinstance(claim, dict) else None


def _is_self(claim: dict[str, Any]) -> bool:
    return claim.get("strength") == graph_memory_protocol.CLAIM_STRENGTH_SELF


def _source(claim: dict[str, Any], name_of: NameOf) -> str:
    return name_of(int(claim.get("by_id") or 0)) or f"гость с id {claim.get('by_id')}"


def _by_gender(gender: str | None, male: str, female: str, unknown: str) -> str:
    if gender == graph_memory_protocol.GENDER_MALE:
        return male
    if gender == graph_memory_protocol.GENDER_FEMALE:
        return female
    return unknown


def speaker_card_note(
    card: Card | None, name_of: NameOf, *, known_gender: str | None = None
) -> str | None:
    """Строки о собеседнике. ``known_gender`` — пол из settings.people: он
    задан владельцем в конфиге, важнее карточки, и строку о поле тогда не
    пишем (её уже дал _known_person_note), только склоняем по нему."""
    if not card:
        return None
    parts: list[str] = []
    claim = card.get("gender")
    gender = known_gender or card_gender(card)
    if known_gender is None and isinstance(claim, dict) and gender in _GENDER_WORD:
        word, title = _GENDER_WORD[gender], _TITLE[gender]
        if _is_self(claim):
            said = _by_gender(gender, "сказал это о себе сам", "сказала это о себе сама", "")
            parts.append(
                f"Собеседник — {word}: {said}. Обращайся «{title}» и склоняй слова о "
                "собеседнике в этом роде. Это точно — по имени не угадывай. Если "
                "спросят, откуда знаешь, — с собственных слов собеседника."
            )
        else:
            source = _source(claim, name_of)
            parts.append(
                f"Собеседник — {word}: так говорит {source}, сам собеседник этого "
                f"не подтверждал. Обращайся «{title}», пока он(а) не скажет иначе. "
                f"Если спросят, откуда знаешь, — назови источник: {source}."
            )
    name = card.get("name")
    if isinstance(name, dict) and name.get("value"):
        if _is_self(name):
            whose = _by_gender(
                gender, "сам так представился", "сама так представилась", "так представились сами"
            )
        else:
            whose = f"так говорит {_source(name, name_of)}"
        parts.append(f"Собеседника зовут {name['value']} ({whose}).")
    aliases = [a["value"] for a in card.get("aliases") or [] if a.get("value")]
    if aliases:
        pronoun = _by_gender(gender, "Его", "Её", "Собеседника")
        parts.append(f"{pronoun} также зовут: " + ", ".join(aliases) + ".")
    return " ".join(parts) or None


def roster_line(person_id: int, display: str, card: Card | None, name_of: NameOf) -> str:
    """«Милана — id 202 — женщина (сама сказала)» для списка знакомых и
    участников: по этой строке модель и адресует, и склоняет."""
    bits = [display, f"id {person_id}"]
    gender = (card or {}).get("gender")
    if isinstance(gender, dict) and gender.get("value") in _GENDER_WORD:
        word = _GENDER_WORD[gender["value"]]
        if _is_self(gender):
            bits.append(f"{word} (с собственных слов)")
        else:
            bits.append(f"{word} (так говорит {_source(gender, name_of)})")
    aliases = [a["value"] for a in (card or {}).get("aliases") or [] if a.get("value")]
    if aliases:
        bits.append("зовут также " + ", ".join(aliases))
    return " — ".join(bits)
