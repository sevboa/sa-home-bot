"""Инвайт-коды приватного входа: формат, разбор, лимит попыток.

Здесь только чистая механика кода — выдача, нормализация, счётчик попыток.
Кто кого впускает и что при этом говорит, живёт в `bot/handlers/invites.py`,
а сама дверь (молчание для неподписных чатов) — в `bot/middlewares.py`.

Алфавит — Crockford base32: из привычного base32 убраны `I`, `L`, `O`, `U`
(первые три ни на слух, ни на глаз не отличимы от `1` и `0`; `U` убран, чтобы
случайное слово не получилось грубым). При разборе `I`/`L` читаются как `1`,
`O` — как `0`: человек, переписавший код с экрана, ошибиться в этом месте не
должен. 32⁸ ≈ 1.1·10¹² — подбор вслепую бессмысленен даже без лимитера, но
лимитер всё равно есть: молчание бота само по себе от онлайн-перебора не
защищает.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections.abc import Hashable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sa_home_bot.bot.notifier import Notifier, notify_admins
from sa_home_bot.config import InvitesConfig
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.guests import GuestStore
from sa_home_bot.subscriptions.models import SOURCE_GUEST, Subscription

log = logging.getLogger(__name__)

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
GROUP = 4  # печатаем как «K7M4-QP2R»: так его проще продиктовать и списать

# Что читается как что при разборе (правило Crockford).
_CONFUSABLES = {"I": "1", "L": "1", "O": "0"}
# Разделители, которые человек может оставить, копируя код.
_SEPARATORS = re.compile(r"[\s\-_.]+")
# Потолок длины сырого текста, который вообще рассматривается как код: без
# него любое сообщение в чате считалось бы попыткой подбора и съедало лимит.
_MAX_RAW_LENGTH = 16


def generate_code() -> str:
    """Новый код. secrets, не random: это секрет, пусть и короткоживущий."""
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def format_code(code: str) -> str:
    return "-".join(code[i : i + GROUP] for i in range(0, len(code), GROUP))


def normalize(text: str | None) -> str | None:
    """Сырой текст → канонический код, либо None — это не код.

    Терпимо к регистру, разделителям и путанице `O/0`, `I/1`; всё остальное
    (лишние символы, другая длина) — не код.
    """
    if not text:
        return None
    raw = text.strip()
    if len(raw) > _MAX_RAW_LENGTH:
        return None
    cleaned = _SEPARATORS.sub("", raw).upper()
    if len(cleaned) != CODE_LENGTH:
        return None
    candidate = "".join(_CONFUSABLES.get(ch, ch) for ch in cleaned)
    if any(ch not in ALPHABET for ch in candidate):
        return None
    return candidate


class AttemptLimiter:
    """Счётчик попыток под произвольным ключом (изначально — chat_id
    неподписного чата, вводящего инвайт-код; bot/tools.py переиспользует
    тот же класс под ключ-пару (откуда, кому) для tell/notify_guest — живая
    находка 2026-08-06, см. её докстринг там).

    Живёт в памяти процесса и обнуляется при рестарте — сознательно: это
    защита от долбёжки, а не учётная запись, и хранить её в реплицируемом
    состоянии не за чем. Окно скользящее: держим отметки времени попыток и
    выбрасываем те, что старше часа.
    """

    WINDOW_S = 3600.0

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._hits: dict[Hashable, list[float]] = {}

    def register(self, key: Hashable, now: float | None = None) -> bool:
        """Засчитать попытку. False — лимит исчерпан, дальше не проверяем."""
        moment = time.monotonic() if now is None else now
        hits = [t for t in self._hits.get(key, []) if moment - t < self.WINDOW_S]
        hits.append(moment)
        self._hits[key] = hits
        return len(hits) <= self._limit

    def exhausted_now(self, key: Hashable) -> bool:
        """Ровно ли сейчас исчерпан лимит — момент, когда стоит сказать админу
        (один раз на окно, а не на каждую следующую попытку)."""
        return len(self._hits.get(key, [])) == self._limit + 1

    def forget(self, key: Hashable) -> None:
        """Забыть ключ — например, чат больше не «чужой» (код принят)."""
        self._hits.pop(key, None)


@dataclass(frozen=True)
class Admission:
    """Кого и по чьему коду впустили — данные для приветствия и уведомления."""

    subscription: Subscription
    invited_by_chat_id: int
    code: str


class Gatekeeper:
    """Единственное, что чужой чат может сделать с ботом, — предъявить код.

    Всё остальное для неподписного чата не существует (`SilenceGate` в
    bot/middlewares.py просто не пускает апдейт дальше). Здесь — выпуск
    кодов, их погашение и превращение погашенного кода в живую подписку.
    """

    def __init__(
        self,
        cfg: InvitesConfig,
        store: Store,
        book: SubscriptionBook,
        guests: GuestStore,
        notifier: Notifier,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._book = book
        self._guests = guests
        self._notifier = notifier
        self._limiter = AttemptLimiter(cfg.max_attempts_per_hour)

    @property
    def enabled(self) -> bool:
        """Инвайты работают, только если гостей есть куда записать: без
        гостевого пакета впущенный исчезнет при первом же рестарте, а такая
        дверь хуже, чем никакой."""
        return self._cfg.enabled and self._guests.available

    # --- выпуск -------------------------------------------------------------

    async def issue(
        self, chat_id: int, user_id: int | None, ttl_s: float | None = None
    ) -> tuple[str, datetime]:
        """Выпустить код для чата-приглашающего. Возвращает код и срок.

        ``ttl_s`` — разовое переопределение срока (см. bot/handlers/
        invites.py::cmd_invite, "/invite <часы>"); ``None`` — обычный
        дефолт из конфига (``[invites].ttl_s``)."""
        now = datetime.now(tz=UTC)
        expires = now + timedelta(seconds=ttl_s if ttl_s is not None else self._cfg.ttl_s)
        code = generate_code()
        await self._store.create_invite(code, chat_id, user_id, now, expires)
        log.info("Выпущен инвайт-код (chat=%s, годен до %s)", chat_id, expires.isoformat())
        return code, expires

    async def open_codes(self) -> list[dict]:
        return await self._store.open_invites(datetime.now(tz=UTC))

    async def known_code(self, text: str | None) -> tuple[str, dict] | None:
        """Наш ли это код (в любом состоянии) — вместе с его записью.

        Нужно там, где код прислали в чат, который и так подписной: сам по
        себе гейт такое сообщение пропускает как обычный текст, и оно уезжает
        в разговор с Альфредом. Живая находка 2026-07-30: так и вышло —
        модель приняла код за поисковый запрос и отправила его в веб-поиск,
        то есть секрет уехал во внешний поисковик. Распознать код надо до
        того, как он попадёт куда-либо ещё.

        Проверяем не только формат, но и наличие в базе: восемь символов из
        алфавита — слишком слабый признак, чтобы перехватывать по нему любую
        похожую строку в живом разговоре.
        """
        code = normalize(text)
        if code is None:
            return None
        row = await self._store.find_invite(code)
        return (code, row) if row is not None else None

    async def revoke_code(self, code: str) -> bool:
        return await self._store.revoke_invite(code, datetime.now(tz=UTC))

    # --- вход ---------------------------------------------------------------

    async def try_admit(
        self,
        chat_id: int,
        text: str | None,
        *,
        user_id: int | None = None,
        user_name: str | None = None,
        chat_title: str | None = None,
    ) -> Admission | None:
        """Проверить предъявленный код и, если он годен, впустить чат.

        None — не код, код не годен или лимит попыток исчерпан. Различать эти
        случаи снаружи незачем: ответ во всех один — молчание (иначе «код
        неверный» стало бы подтверждением, что бот здесь есть).
        """
        if not self.enabled:
            return None
        code = normalize(text)
        if code is None:
            return None
        if not self._limiter.register(chat_id):
            if self._limiter.exhausted_now(chat_id):
                await notify_admins(
                    self._book,
                    self._notifier,
                    f"🚨 Подбор инвайт-кода: chat_id=<code>{chat_id}</code> "
                    f"исчерпал лимит попыток. Отвечать ему я не буду.",
                )
            return None
        now = datetime.now(tz=UTC)
        row = await self._store.redeem_invite(
            code, at=now, chat_id=chat_id, user_id=user_id, user_name=user_name
        )
        if row is None:
            return None
        self._limiter.forget(chat_id)
        subscription = self._make_guest(chat_id, now, row, user_name, chat_title)
        self._book.add(subscription)
        self._guests.save(self._book.guests())
        log.info(
            "Инвайт погашен: chat_id=%s (%s), пригласил chat_id=%s",
            chat_id, user_name or "?", row.get("issued_by_chat_id"),
        )
        return Admission(
            subscription=subscription,
            invited_by_chat_id=int(row.get("issued_by_chat_id") or 0),
            code=code,
        )

    def _make_guest(
        self,
        chat_id: int,
        now: datetime,
        row: dict,
        user_name: str | None,
        chat_title: str | None,
    ) -> Subscription:
        # Имя подписки — для человека в /guests, идентификатор всегда chat_id.
        name = chat_title or user_name or f"гость {chat_id}"
        return Subscription(
            name=name,
            chat_id=chat_id,
            event_types=frozenset(self._cfg.grant_events),
            allowed_commands=frozenset(self._cfg.grant_commands),
            source=SOURCE_GUEST,
            invited_by_chat_id=int(row.get("issued_by_chat_id") or 0),
            invited_at=now.isoformat(),
            invited_user=user_name or "",
        )

    # --- отзыв --------------------------------------------------------------

    def revoke_guest(self, chat_id: int) -> bool:
        """Выставить гостя. False — такого гостя нет (или он не гость)."""
        sub = self._book.for_chat(chat_id)
        if sub is None or not sub.is_guest:
            return False
        self._book.remove(chat_id)
        self._guests.save(self._book.guests())
        log.info("Гость chat_id=%s отозван", chat_id)
        return True

    # --- права гостя (точечно, в рантайме) -----------------------------------
    #
    # Раньше (AUTHORIZATION.md §8.11 в старой редакции) права гостя были
    # заданы раз и навсегда набором `[invites].grant_commands` — решение
    # 2026-08-04 сняло это ограничение: гостевые подписки и так уже мутабельны
    # в рантайме (revoke_guest выше), точечная правка — тот же приём, что и
    # полный отзыв, только re-write подписки вместо её удаления.

    def set_guest_rights(
        self, chat_id: int, allowed_commands: frozenset[str]
    ) -> Subscription | None:
        """Переписать набор прав гостя целиком. None — не гость (или его нет).

        Владельческую подписку так не тронуть: она иммутабельна во время
        работы (правка = правка конфига + рестарт), и `sub.is_guest` это
        гарантирует так же, как в revoke_guest.
        """
        sub = self._book.for_chat(chat_id)
        if sub is None or not sub.is_guest:
            return None
        updated = sub.with_allowed_commands(allowed_commands)
        self._book.add(updated)
        self._guests.save(self._book.guests())
        return updated

    def add_guest_right(self, chat_id: int, right: str) -> Subscription | None:
        sub = self._book.for_chat(chat_id)
        if sub is None:
            return None
        updated = self.set_guest_rights(chat_id, sub.allowed_commands | {right})
        if updated is not None:
            log.info("Гостю chat_id=%s выдано право %s", chat_id, right)
        return updated

    def remove_guest_right(self, chat_id: int, right: str) -> Subscription | None:
        sub = self._book.for_chat(chat_id)
        if sub is None:
            return None
        updated = self.set_guest_rights(chat_id, sub.allowed_commands - {right})
        if updated is not None:
            log.info("У гостя chat_id=%s отозвано право %s", chat_id, right)
        return updated

    def set_guest_family(self, chat_id: int, family: bool) -> Subscription | None:
        """Переключить флаг «семья» у гостя. None — не гость (или его нет).

        Флаг не входит в allowed_commands — это отдельная ось доступа,
        дающая гостю ещё и scope="family" в памяти (memory/service.py).
        """
        sub = self._book.for_chat(chat_id)
        if sub is None or not sub.is_guest:
            return None
        updated = sub.with_family(family)
        self._book.add(updated)
        self._guests.save(self._book.guests())
        log.info("Гостю chat_id=%s флаг «семья» выставлен в %s", chat_id, family)
        return updated
