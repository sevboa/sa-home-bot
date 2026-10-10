"""Кто есть кто (Этап 58): снимок карточек, профилей Telegram и подписок.

Грузится одним заходом на ход /ai (людей — десятки) и дальше отвечает
синхронно: тулы и справка спрашивают имя, метки для поиска и пол отсюда, а
не сшивают подписку, конфиг и граф каждый по-своему.

Отображаемое имя: имя из карточки → имя из профиля Telegram → имя
подписки. @ник — из профиля, иначе из подписки («Имя (@ник)»).
"""

from __future__ import annotations

import re
from typing import Any

from sa_home_bot.people import claims as people_claims
from sa_home_bot.people.claims import Card

_HANDLE_RE = re.compile(r"\(@([A-Za-z0-9_]{3,})\)")

# Виды меток — их видит модель в причине совпадения find_person.
LABEL_NAME = "имя"
LABEL_ALIAS = "прозвище"
LABEL_TELEGRAM = "имя в Telegram"
LABEL_NICK = "ник"
LABEL_SUBSCRIPTION = "имя при приглашении"


def split_label(label: str) -> tuple[str, str]:
    """«Имя Фамилия (@ник)» → («Имя Фамилия», «ник»): так бот пишет
    invited_user и ai_turns.user_name."""
    match = _HANDLE_RE.search(label or "")
    if match is None:
        return " ".join((label or "").split()), ""
    name = _HANDLE_RE.sub(" ", label)
    return " ".join(name.split()), match.group(1)


class PeopleBook:
    def __init__(
        self,
        claims: list[dict[str, Any]],
        profiles: list[dict[str, Any]],
        book: Any | None = None,
    ) -> None:
        self._cards = people_claims.build_cards(claims)
        self._profiles = {int(p["user_id"]): p for p in profiles}
        self._book = book  # SubscriptionBook | None

    @classmethod
    async def load(cls, store: Any, book: Any | None = None) -> PeopleBook:
        return cls(await store.person_claims(), await store.person_profiles(), book)

    @classmethod
    def empty(cls, book: Any | None = None) -> PeopleBook:
        """Без БД (служба tasks, MCP-агент): только имена подписок."""
        return cls([], [], book)

    def card(self, person_id: int) -> Card:
        return self._cards.get(person_id) or {}

    def value(self, person_id: int, field: str) -> str | None:
        return people_claims.value(self._cards.get(person_id), field)

    def gender(self, person_id: int) -> str | None:
        return self.value(person_id, people_claims.FIELD_GENDER)

    def _sub(self, person_id: int) -> Any | None:
        return self._book.for_chat(person_id) if self._book is not None else None

    def _telegram_name(self, person_id: int) -> str:
        profile = self._profiles.get(person_id)
        if profile is None:
            return ""
        return " ".join(f"{profile['first_name']} {profile['last_name']}".split())

    def username(self, person_id: int) -> str:
        profile = self._profiles.get(person_id)
        if profile is not None and profile["username"]:
            return str(profile["username"])
        sub = self._sub(person_id)
        if sub is None:
            return ""
        for label in (sub.invited_user, sub.name):
            nick = split_label(label or "")[1]
            if nick:
                return nick
        return ""

    def name(self, person_id: int) -> str | None:
        """Как человека называть: карточка → Telegram → подписка."""
        named = self.value(person_id, people_claims.FIELD_NAME) or self._telegram_name(person_id)
        if named:
            return named
        sub = self._sub(person_id)
        if sub is None:
            return None
        return split_label(sub.invited_user or sub.name)[0] or None

    def display(self, person_id: int) -> str | None:
        """«Андрей Александрович Севбо (@kein)» — имя и ник, если он есть."""
        name = self.name(person_id)
        if name is None:
            return None
        nick = self.username(person_id)
        return f"{name} (@{nick})" if nick and nick.casefold() not in name.casefold() else name

    def labels(self, person_id: int) -> list[tuple[str, str]]:
        """Все имена человека с видом метки — для поиска и для «ещё: …».
        Подпись подписки владельца («me») в метки не идёт: это технический
        ключ, гость не должен находить владельца по нему."""
        own: list[tuple[str, str]] = []
        card = self._cards.get(person_id) or {}
        own += [(LABEL_NAME, n) for n in card.get("names") or []]
        own += [(LABEL_ALIAS, a["value"]) for a in card.get("aliases") or []]
        telegram = self._telegram_name(person_id)
        if telegram:
            own.append((LABEL_TELEGRAM, telegram))
        nick = self.username(person_id)
        if nick:
            own.append((LABEL_NICK, f"@{nick}"))
        sub = self._sub(person_id)
        if sub is not None and not sub.is_owner:
            for label in (sub.name, sub.invited_user):
                name = split_label(label or "")[0]
                if name:
                    own.append((LABEL_SUBSCRIPTION, name))
        seen: set[str] = set()
        unique: list[tuple[str, str]] = []
        for kind, label in own:
            key = label.casefold()
            if key not in seen:
                seen.add(key)
                unique.append((kind, label))
        return unique
