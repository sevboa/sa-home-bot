"""Перенос знаний о людях в карточки (Этап 58.2) — один раз, метки в
app_state.

- ``[[people]]`` из конфига — слово владельца: слабое утверждение с
  by_id = владелец (о себе самом — сильное);
- профили Telegram тех, кто писал до person_profiles: последнее
  ``ai_turns.user_name`` и ``invited_user`` подписок;
- карточки ``PersonClaim`` из графа (этап 54) — по-прежнему на mycraft;
  метка ставится только после ответа службы, иначе повтор позже.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sa_home_bot.config import PersonConfig, Settings
from sa_home_bot.graph_memory import protocol as graph_memory_protocol
from sa_home_bot.people import claims as people_claims
from sa_home_bot.people.book import split_label
from sa_home_bot.proto.messages import Address

log = logging.getLogger(__name__)

SEED_CONFIG_KEY = "people_seed_config_v1"
SEED_PROFILES_KEY = "people_seed_profiles_v1"
SEED_GRAPH_KEY = "people_seed_graph_v1"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def config_person_ids(person: PersonConfig, book: Any) -> list[int]:
    """Telegram id записи [[people]]: telegram_id, иначе подписка с тем же
    @ником («Имя (@ник)»)."""
    if person.telegram_id:
        return [person.telegram_id]
    handle = person.telegram_username.lstrip("@").casefold()
    if not handle:
        return []
    found: list[int] = []
    for sub in book.all():
        for label in (sub.name, sub.invited_user):
            if split_label(label or "")[1].casefold() == handle and sub.chat_id not in found:
                found.append(sub.chat_id)
    return found


def _owner_id(book: Any) -> int | None:
    return next((sub.chat_id for sub in book.all() if sub.is_owner and sub.chat_id > 0), None)


def config_claims(settings: Settings, book: Any, now: datetime) -> list[dict[str, Any]]:
    owner = _owner_id(book)
    if owner is None:
        return []
    out: list[dict[str, Any]] = []
    for person in settings.people:
        fields = {
            people_claims.FIELD_NAME: person.full_name,
            people_claims.FIELD_GENDER: person.gender,
            people_claims.FIELD_BIRTH_DATE: person.birth_date,
            people_claims.FIELD_CITY: person.city,
            people_claims.FIELD_TIMEZONE: person.timezone,
        }
        for subject_id in config_person_ids(person, book):
            for field, raw in fields.items():
                if not raw:
                    continue
                try:
                    field, value = people_claims.normalize_claim(field, raw)
                except people_claims.ClaimError as exc:
                    log.warning("[[people]] %s: %s пропущено: %s", subject_id, field, exc)
                    continue
                out.append(
                    {
                        "subject_id": subject_id,
                        "field": field,
                        "value": value,
                        "by_id": owner,
                        "strength": people_claims.strength_for(subject_id, owner),
                        "at": _iso(now),
                    }
                )
    return out


async def seed_local(
    store: Any, settings: Settings, book: Any, now: datetime | None = None
) -> None:
    """Конфиг и профили — всё, что есть на alfred. Зовётся при старте бота."""
    now = now or datetime.now(UTC)
    if await store.get_state(SEED_PROFILES_KEY) is None:
        profiles: dict[int, tuple[str, str]] = {}
        for sub in book.all():
            if sub.chat_id > 0 and sub.invited_user:
                profiles[sub.chat_id] = split_label(sub.invited_user)
        # ai_turns свежее приглашения — перекрывает.
        for row in await store.latest_turn_user_names():
            profiles[int(row["user_id"])] = split_label(row["user_name"])
        for person in settings.people:
            for subject_id in config_person_ids(person, book):
                name, nick = profiles.get(subject_id, ("", ""))
                if not nick and person.telegram_username:
                    profiles[subject_id] = (name, person.telegram_username.lstrip("@"))
        known = {int(p["user_id"]) for p in await store.person_profiles()}
        for user_id, (name, nick) in profiles.items():
            if user_id not in known and (name or nick):
                await store.upsert_person_profile(user_id, name, "", nick, now)
        await store.set_state(SEED_PROFILES_KEY, _iso(now))
        log.info("Карточки людей: перенесены профили Telegram (%d)", len(profiles))
    if await store.get_state(SEED_CONFIG_KEY) is None:
        claims = config_claims(settings, book, now)
        if claims:
            await store.add_person_claims(claims)
        await store.set_state(SEED_CONFIG_KEY, _iso(now))
        log.info("Карточки людей: перенесён [[people]] (%d утверждений)", len(claims))


def graph_card_claims(subject_id: int, card: dict[str, Any]) -> list[dict[str, Any]]:
    """Карточка этапа 54 ({gender, name: {value, by_id, strength, at}, aliases})
    → утверждения с теми же источниками."""
    out: list[dict[str, Any]] = []
    picked = [(f, card.get(f)) for f in (people_claims.FIELD_GENDER, people_claims.FIELD_NAME)]
    picked += [(people_claims.FIELD_ALIAS, a) for a in card.get("aliases") or []]
    for field, claim in picked:
        if not isinstance(claim, dict) or not claim.get("value"):
            continue
        strength = claim.get("strength")
        if strength not in (people_claims.STRENGTH_SELF, people_claims.STRENGTH_ACQUAINTANCE):
            continue
        out.append(
            {
                "subject_id": subject_id,
                "field": field,
                "value": str(claim["value"]),
                "by_id": int(claim.get("by_id") or subject_id),
                "strength": strength,
                "at": str(claim.get("at") or ""),
            }
        )
    return out


async def seed_from_graph(store: Any, book: Any, node_link: Any) -> bool:
    """Карточки из графа. True — перенесено (или уже было); False — служба
    не ответила, попробовать позже. Ошибки связи не пробрасывает."""
    if await store.get_state(SEED_GRAPH_KEY) is not None:
        return True
    if node_link is None:
        return False
    ids = sorted(sub.chat_id for sub in book.all() if sub.chat_id > 0)
    dst = Address(node=graph_memory_protocol.NODE_ID, service=graph_memory_protocol.SERVICE_NAME)
    try:
        result = await node_link.command(
            graph_memory_protocol.ACTION_PERSON_CARDS,
            {"ids": ",".join(str(i) for i in ids)},
            dst=dst,
            timeout=10.0,
        )
    except Exception as exc:  # служба спит/недоступна — повтор позже
        log.info("Карточки людей: граф не ответил, перенос позже (%s)", exc)
        return False
    claims: list[dict[str, Any]] = []
    for key, card in (result.get("cards") or {}).items():
        try:
            subject_id = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(card, dict):
            claims += graph_card_claims(subject_id, card)
    if claims:
        await store.add_person_claims(claims)
    await store.set_state(SEED_GRAPH_KEY, _iso(datetime.now(UTC)))
    log.info("Карточки людей: перенесено из графа %d утверждений", len(claims))
    return True


# Связь с нодой поднимается не сразу, mycraft может спать: первая попытка —
# через минуту после старта, дальше раз в 10 минут, пока не перенесём.
GRAPH_SEED_FIRST_S = 60.0
GRAPH_SEED_RETRY_S = 600.0


async def graph_seed_loop(store: Any, book: Any, node_link: Any) -> None:
    """Фоновая задача бота: перенос карточек из графа, пока не удастся."""
    await asyncio.sleep(GRAPH_SEED_FIRST_S)
    while True:
        try:
            if await seed_from_graph(store, book, node_link):
                return
        except Exception:
            log.exception("Карточки людей: перенос из графа упал")
        await asyncio.sleep(GRAPH_SEED_RETRY_S)
