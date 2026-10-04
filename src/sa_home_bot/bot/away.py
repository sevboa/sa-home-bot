"""«Альфред в городе» (Этап 51, IMPLEMENTATION_PLAN.md): окно обслуживания mycraft.

На время обслуживания или занятия мощностей сервера (обновления, генерация
датасета, обучение LoRA) никто не должен звать модель — ни гости, ни
владелец. Альфред «спустился из замка в город по делам»: входящее копится в
``ai_turns``, модель не зовётся, гостям уходит заготовленная записка; по
возвращении он разбирает всё сам (bot/away_return.py).

Состояние — одна запись ``app_state["alfred_away"]`` (JSON, ``AwayState``):
читается из БД на каждом входящем, поэтому правка из CLI (``sa-home-bot away``,
src/sa_home_bot/away_cli.py) подхватывается без перезапуска. Таймеров в памяти
нет — срок и потолок проверяет периодический проход (bot/away_return.py::
AwayRunner), состояние живёт в БД и переживает рестарт бота.

Фазы: ``away`` — Альфред в городе; ``returning`` — возвращение объявлено
(``/back``, потолок 24 ч), но очередь ещё не разобрана: гости по-прежнему видят
записку «вот-вот вернётся», модель всё ещё не зовётся ни для кого, кроме
разбора очереди. Запись исчезает, когда ``pending`` пуст.

Потолок: возвращение не позже 24 ч от ``started_at``; продления за потолок не
уводят. По ``until`` режим НЕ снимается — Альфред «задерживается» до ``/back``.

Модуль не импортирует хендлеры на уровне модуля (хендлеры импортируют его).
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot.ai_flow import ALFRED_TIMEZONE, display_name
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store

if TYPE_CHECKING:
    from aiogram.types import Message

log = logging.getLogger(__name__)

ALFRED_AWAY_KEY = "alfred_away"
# Очередь сообщений владельцу, которые должен отправить бот: CLI — отдельный
# процесс и Telegram не знает (изменения от Claude, см. AwayService.queue_notice).
ALFRED_AWAY_NOTICES_KEY = "alfred_away_notices"

PHASE_AWAY = "away"
PHASE_RETURNING = "returning"

SET_BY_ADMIN = "admin"
SET_BY_CLAUDE = "claude"

# Потолок отъезда от started_at.
MAX_AWAY = timedelta(hours=24)
# За сколько до срока напомнить владельцу.
REMIND_BEFORE = timedelta(minutes=15)
# Не чаще раза в N минут на чат — записка гостю (после продления — заново).
NOTE_INTERVAL = timedelta(minutes=30)
# Сколько раз подряд можно не доставить ответ чату при возвращении, прежде
# чем снять чат с очереди (владельцу — сообщение).
MAX_RETURN_ATTEMPTS = 3

# Разбор очереди голосовых при возвращении (bot/away_return.py): лимит на чат.
MAX_VOICES_PER_CHAT = 5
MAX_VOICE_SECONDS_PER_CHAT = 10 * 60

KIND_TEXT = "text"
KIND_PHOTO = "photo"
KIND_STICKER = "sticker"
KIND_MEDIA = "media"  # голосовое / кружочек / аудио
KIND_SUMMON = "summon"  # позвали без содержимого (голый /alfred, упоминание)

MEDIA_VOICE = "voice"
MEDIA_VIDEO_NOTE = "video_note"
MEDIA_AUDIO = "audio"
_MEDIA_LABELS = {
    MEDIA_VOICE: "голосовое",
    MEDIA_VIDEO_NOTE: "кружочек",
    MEDIA_AUDIO: "аудио",
}
# Заглушка в ai_turns на месте файла, пока он не распознан.
MEDIA_STUB_SUFFIX = ", ждёт распознавания]"
MEDIA_FAILED_TEXT = "[голосовое, не удалось разобрать]"
MEDIA_SKIPPED_TEXT = "[голосовое, не разбиралось — прислали ещё несколько голосовых]"

NOT_AWAY_TEXT = "Альфред и так дома."
USAGE_TEXT = (
    "Использование: /away &lt;срок&gt; [причина] — срок: <code>3ч</code>, "
    "<code>1ч30м</code>, <code>до 23:00</code>, <code>завтра 10:00</code>.\n"
    "/away +1ч — продлить, /back — вернуть, /away — статус."
)


# Кнопки напоминания владельцу: «away:ext:<минуты>» и «away:back»
# (bot/handlers/away.py).
CALLBACK_PREFIX = "away"
CB_EXTEND = "ext"
CB_BACK = "back"
EXTEND_STEPS_MIN = (30, 60, 120)


def reminder_keyboard() -> InlineKeyboardMarkup:
    """[+30м] [+1ч] [+2ч] [Вернуться сейчас]."""
    labels = {30: "+30м", 60: "+1ч", 120: "+2ч"}
    row = [
        InlineKeyboardButton(
            text=labels[m], callback_data=f"{CALLBACK_PREFIX}:{CB_EXTEND}:{m}"
        )
        for m in EXTEND_STEPS_MIN
    ]
    back = InlineKeyboardButton(
        text="Вернуться сейчас", callback_data=f"{CALLBACK_PREFIX}:{CB_BACK}"
    )
    return InlineKeyboardMarkup(inline_keyboard=[row, [back]])


class AwayError(Exception):
    """Понятная человеку причина отказа (текст — прямо в ответ владельцу)."""


# ---------------------------------------------------------------- разбор срока

_UNITS: dict[str, int] = {}
for _names, _seconds in (
    (("д", "дн", "день", "дня", "дней", "d", "day", "days"), 86400),
    (("ч", "час", "часа", "часов", "h", "hr", "hrs", "hour", "hours"), 3600),
    (("м", "мин", "минута", "минуту", "минуты", "минут", "m", "min", "mins", "minute", "minutes"),
     60),
):
    for _name in _names:
        _UNITS[_name] = _seconds

_PIECE_RE = re.compile(r"\s*(\d+(?:[.,]\d+)?)\s*([a-zа-яё]+)", re.IGNORECASE)
_CLOCK_RE = re.compile(r"(\d{1,2})[:.](\d{2})(?!\d)")


def parse_duration(text: str) -> tuple[timedelta, str] | None:
    """Срок с начала строки: ``3ч``, ``1ч30м``, ``90 минут``, ``1.5h``.

    Возвращает (срок, остаток строки) или None — строка не начинается со
    срока. Число без единицы («3») — не срок: неясно, часы это или минуты."""
    total = 0.0
    pos = 0
    found = False
    while True:
        match = _PIECE_RE.match(text, pos)
        if match is None:
            break
        seconds = _UNITS.get(match.group(2).lower())
        if seconds is None:
            break
        total += float(match.group(1).replace(",", ".")) * seconds
        pos = match.end()
        found = True
    if not found:
        return None
    return timedelta(seconds=round(total)), text[pos:].strip()


def _clock_after(prefix: str, text: str) -> tuple[int, int, str] | None:
    """«до 23:00 причина» → (23, 0, «причина»), если строка начинается с prefix."""
    lowered = text.lower()
    if not lowered.startswith(prefix):
        return None
    tail = text[len(prefix) :].lstrip()
    match = _CLOCK_RE.match(tail)
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute, tail[match.end() :].strip()


def parse_away_args(text: str, now: datetime, tz: tzinfo) -> tuple[datetime, str]:
    """Аргументы ``/away``/``away set`` → (until в UTC, причина).

    ``3ч``/``1ч30м`` — от ``now``; ``до 23:00`` — ближайшее такое время в
    часовом поясе ``tz`` (уже прошло — завтра); ``завтра 10:00`` — завтра.
    Потолок здесь НЕ применяется (см. clamp_until): нужен started_at.
    Исключение AwayError — срока нет или он непонятен."""
    text = (text or "").strip()
    if not text:
        raise AwayError("Нужен срок. " + USAGE_TEXT)
    now_local = now.astimezone(tz)

    clock = _clock_after("до", text)
    if clock is not None:
        hour, minute, reason = clock
        target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now_local:
            target += timedelta(days=1)
        return target.astimezone(UTC), reason
    clock = _clock_after("завтра", text)
    if clock is not None:
        hour, minute, reason = clock
        target = (now_local + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        return target.astimezone(UTC), reason

    parsed = parse_duration(text)
    if parsed is None:
        raise AwayError("Не понял срок «" + text.split()[0] + "». " + USAGE_TEXT)
    delta, reason = parsed
    if delta <= timedelta(0):
        raise AwayError("Срок должен быть больше нуля.")
    return now.astimezone(UTC) + delta, reason


def parse_extend(text: str) -> timedelta:
    """``+1ч``/``1ч``/``30м`` → на сколько продлить."""
    parsed = parse_duration((text or "").strip().lstrip("+").strip())
    if parsed is None or parsed[0] <= timedelta(0):
        raise AwayError("Не понял, на сколько продлить. Пример: /away +1ч")
    return parsed[0]


def clamp_until(until: datetime, started_at: datetime) -> tuple[datetime, bool]:
    """Срок не позже потолка (started_at + 24 ч). Второе — True, если урезали."""
    ceiling = started_at + MAX_AWAY
    if until > ceiling:
        return ceiling, True
    return until, False


# ------------------------------------------------------------------ оформление


def format_remaining(until: datetime, now: datetime) -> str:
    """«через 2 ч 15 мин»; минуты — вверх, меньше минуты — «меньше чем через
    минуту», срок прошёл — «вот-вот должен вернуться»."""
    seconds = (until - now).total_seconds()
    if seconds <= 0:
        return "вот-вот должен вернуться"
    if seconds < 60:
        return "меньше чем через минуту"
    minutes = -(-int(seconds) // 60)  # потолок: 2 ч 14 мин 10 с → 2 ч 15 мин
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    return "через " + " ".join(parts)


def _clock(until: datetime, now: datetime, tz: tzinfo) -> str:
    """«18:00» — или «10:00 завтра», если по часам tz это другой день."""
    local = until.astimezone(tz)
    text = f"{local:%H:%M}"
    if local.date() != now.astimezone(tz).date():
        text += " завтра"
    return text


def castle_clock(until: datetime, now: datetime) -> str:
    """Время возвращения по «замковым часам» — Europe/Bucharest."""
    return _clock(until, now, ZoneInfo(ALFRED_TIMEZONE))


def note_text(state: AwayState, now: datetime) -> str:
    """Записка гостю — заготовленная фраза в образе, без LLM. Остаток
    считается в момент отправки."""
    if state.phase == PHASE_RETURNING:
        return "Альфред уже идёт обратно в замок — вот-вот должен вернуться."
    if state.until <= now:
        return "Альфред задерживается в городе — вот-вот должен вернуться."
    remaining = format_remaining(state.until, now)
    when = castle_clock(state.until, now)
    if state.extended:
        return f"Альфред задерживается в городе — передаёт, что будет к {when} ({remaining})."
    return (
        f"Альфред спустился в город по делам. Обещал вернуться к {when} по замковым "
        f"часам ({remaining})."
    )


def _times(count: int) -> str:
    return "1 раз" if count == 1 else f"{count} раза" if 2 <= count <= 4 else f"{count} раз"


def status_text(state: AwayState | None, now: datetime, tz: tzinfo) -> str:
    """Статус владельцу: «в городе до 18:00 (через 2 ч 15 мин), продлевали 1
    раз, включил: Claude, причина: датасет». Время — в поясе владельца."""
    if state is None:
        return NOT_AWAY_TEXT + " Отправить в город: /away 3ч [причина]"
    waiting = f", ждут ответа чатов: {len(state.pending)}" if state.pending else ""
    if state.phase == PHASE_RETURNING:
        return f"Альфред возвращается в замок и разбирает очередь{waiting}."
    parts = [f"в городе до {_clock(state.until, now, tz)} ({format_remaining(state.until, now)})"]
    if state.extended:
        parts.append(f"продлевали {_times(state.extended)}")
    parts.append("включил: " + ("Claude" if state.set_by == SET_BY_CLAUDE else "владелец"))
    if state.reason:
        parts.append(f"причина: {html.escape(state.reason)}")
    return "Альфред " + ", ".join(parts) + waiting + "."


# ------------------------------------------------------------------ состояние


@dataclass
class AwayState:
    """Запись ``app_state["alfred_away"]``."""

    until: datetime
    started_at: datetime
    set_by: str = SET_BY_ADMIN
    reason: str = ""
    extended: int = 0
    # chat_id (строкой — ключи JSON) → когда последний раз писали записку
    noted: dict[str, str] = field(default_factory=dict)
    # chat_id → что нужно для ответа при возвращении (см. AwayService.intercept)
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    phase: str = PHASE_AWAY
    # для какого until уже отправлено напоминание за 15 мин (после продления
    # until другой — напомним заново)
    reminded_for: str = ""
    # chat_id → сколько раз подряд не вышло ответить при возвращении
    attempts: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "until": self.until.isoformat(),
                "started_at": self.started_at.isoformat(),
                "set_by": self.set_by,
                "reason": self.reason,
                "extended": self.extended,
                "noted": self.noted,
                "pending": self.pending,
                "phase": self.phase,
                "reminded_for": self.reminded_for,
                "attempts": self.attempts,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> AwayState | None:
        """None — записи нет или она повреждена (тогда Альфред считается дома:
        испорченное состояние не должно навсегда заглушить бота)."""
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return cls(
                until=datetime.fromisoformat(data["until"]),
                started_at=datetime.fromisoformat(data["started_at"]),
                set_by=str(data.get("set_by") or SET_BY_ADMIN),
                reason=str(data.get("reason") or ""),
                extended=int(data.get("extended") or 0),
                noted=dict(data.get("noted") or {}),
                pending=dict(data.get("pending") or {}),
                phase=str(data.get("phase") or PHASE_AWAY),
                reminded_for=str(data.get("reminded_for") or ""),
                attempts={k: int(v) for k, v in (data.get("attempts") or {}).items()},
            )
        except (ValueError, KeyError, TypeError):
            log.warning("away: запись %s повреждена — считаю, что Альфред дома", ALFRED_AWAY_KEY)
            return None

    @property
    def ceiling(self) -> datetime:
        return self.started_at + MAX_AWAY


def media_stub(kind: str) -> str:
    return f"[{_MEDIA_LABELS.get(kind, 'голосовое')}{MEDIA_STUB_SUFFIX}"


def is_media_stub(content: str) -> bool:
    return content.startswith("[") and content.endswith(MEDIA_STUB_SUFFIX)


def media_of(message: Message) -> tuple[str, Any] | None:
    """(вид, объект файла) для голосового/кружочка/аудио сообщения."""
    for kind in (MEDIA_VOICE, MEDIA_VIDEO_NOTE, MEDIA_AUDIO):
        obj = getattr(message, kind, None)
        if obj:
            return kind, obj
    return None


# --------------------------------------------------------------------- сервис


def local_tz() -> tzinfo:
    """Часовой пояс самого сервера (alfred) — для разбора «до 23:00» из CLI."""
    return datetime.now().astimezone().tzinfo or UTC


class AwayService:
    """Состояние «Альфред в городе»: чтение/правка записи и перехват входящих.

    Без ``notifier``/``book`` (CLI, отдельный процесс) умеет только править
    запись; перехват и записки нужны боту."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    def now(self) -> datetime:
        return self._clock()

    @property
    def media_dir(self) -> Path:
        """Куда качаются голосовые — рядом с БД бота (eMMC alfred), не на /mnt/scratch."""
        return Path(self.settings.database.path).parent / "away-media"

    # --- чтение / правка записи ---

    async def load(self) -> AwayState | None:
        return AwayState.from_json(await self.store.get_state(ALFRED_AWAY_KEY))

    async def is_active(self) -> bool:
        return await self.load() is not None

    async def _update(self, mutate: Callable[[AwayState | None], AwayState | None]) -> None:
        """Атомарная правка записи; ``mutate`` чистая (см. Store.update_state)."""

        def fn(raw: str | None) -> str | None:
            new = mutate(AwayState.from_json(raw))
            return None if new is None else new.to_json()

        await self.store.update_state(ALFRED_AWAY_KEY, fn)

    async def start(
        self,
        until: datetime,
        *,
        set_by: str = SET_BY_ADMIN,
        reason: str = "",
    ) -> tuple[AwayState, bool]:
        """Отправить Альфреда в город. Второе — True, если срок урезан потолком.
        Уже в городе — AwayError (продлевать надо ``extend``)."""
        now = self.now()
        until = until.astimezone(UTC)
        if until <= now:
            raise AwayError("Срок уже прошёл.")
        until, clamped = clamp_until(until, now)
        current = await self.load()
        if current is not None:
            raise AwayError(
                "Альфред уже в городе. " + status_text(current, now, local_tz())
                + " Продлить: /away +1ч, вернуть: /back."
            )
        fresh = AwayState(until=until, started_at=now, set_by=set_by, reason=reason.strip())

        def mutate(state: AwayState | None) -> AwayState | None:
            return state or fresh

        await self._update(mutate)
        stored = await self.load()
        assert stored is not None
        return stored, clamped

    async def extend(self, delta: timedelta) -> AwayState:
        """Продлить («задерживается»). Отсчёт — от срока, а если он уже прошёл —
        от «сейчас». Потолок 24 ч не пробивается: уже на нём — AwayError.
        Записки гостям после продления пишутся заново (noted сбрасывается)."""
        now = self.now()
        outcome: dict[str, Any] = {}

        def mutate(state: AwayState | None) -> AwayState | None:
            outcome.clear()
            if state is None:
                outcome["error"] = NOT_AWAY_TEXT + " Сначала /away <срок>."
                return None
            if state.phase == PHASE_RETURNING:
                outcome["error"] = "Альфред уже возвращается — продлевать поздно."
                return state
            new_until, _ = clamp_until(max(state.until, now) + delta, state.started_at)
            if new_until <= state.until:
                outcome["error"] = (
                    "Потолок отъезда — 24 часа от начала, дальше продлевать нельзя."
                )
                return state
            state.until = new_until
            state.extended += 1
            state.noted = {}
            outcome["state"] = state
            return state

        await self._update(mutate)
        if "error" in outcome:
            raise AwayError(outcome["error"])
        return outcome["state"]

    async def back(self) -> tuple[AwayState, bool]:
        """Вернуть Альфреда. Второе — True, если разбирать нечего и режим снят
        сразу; иначе фаза ``returning`` до конца очереди (bot/away_return.py)."""
        outcome: dict[str, Any] = {}

        def mutate(state: AwayState | None) -> AwayState | None:
            outcome.clear()
            if state is None:
                outcome["error"] = NOT_AWAY_TEXT
                return None
            outcome["state"] = state
            if not state.pending:
                outcome["immediate"] = True
                return None
            outcome["immediate"] = False
            state.phase = PHASE_RETURNING
            return state

        await self._update(mutate)
        if "error" in outcome:
            raise AwayError(outcome["error"])
        return outcome["state"], outcome["immediate"]

    async def clear(self) -> None:
        """Снять режим совсем (очередь пуста)."""
        await self._update(lambda state: None)

    async def queue_notice(self, text: str) -> None:
        """Положить сообщение владельцу в очередь (CLI → бот, см. AwayRunner)."""

        def fn(raw: str | None) -> str | None:
            items = json.loads(raw) if raw else []
            items.append(text)
            return json.dumps(items, ensure_ascii=False)

        await self.store.update_state(ALFRED_AWAY_NOTICES_KEY, fn)

    async def take_notices(self) -> list[str]:
        taken: list[str] = []

        def fn(raw: str | None) -> str | None:
            taken.clear()
            if raw:
                taken.extend(json.loads(raw))
            return None

        await self.store.update_state(ALFRED_AWAY_NOTICES_KEY, fn)
        return list(taken)

    # --- перехват ---

    async def intercept(
        self,
        message: Message,
        *,
        kind: str | None = None,
        text: str = "",
        dialogue_id: int | None = None,
        continuing: bool = False,
    ) -> bool:
        """Перехват ИИ-входа. True — Альфред в городе: входящее записано,
        записка (если пора) отправлена, вызывающий выходит, модель не зовётся.
        False — Альфред дома, всё идёт как обычно.

        ``kind`` — вид входящего; не передан — определяется по сообщению
        (файл/фото/стикер/текст). ``text`` — текст обращения без служебной
        части (``/alfred …`` без команды, упоминание без @имени, подпись).
        ``dialogue_id`` — тред, в который сообщение пошло бы в обычном режиме;
        ``continuing`` — это реплай в уже начатый тред. За время отъезда все
        входящие чата сводятся в один диалог: тот, что открыл первый перехват
        (в топике — сам топик; вне топика — последний тред чата)."""
        state = await self.load()
        if state is None or message.chat is None:
            return False
        chat_id = message.chat.id
        now = self.now()
        media = media_of(message)
        if kind is None:
            if media is not None:
                kind = KIND_MEDIA
            elif message.photo:
                kind = KIND_PHOTO
            elif message.sticker:
                kind = KIND_STICKER
            else:
                kind = KIND_TEXT
                text = text or (message.text or message.caption or "").strip()
        if kind == KIND_TEXT and not text:
            kind = KIND_SUMMON

        content = await self._content_for(message, kind, text, media)
        dialogue = await self._resolve_dialogue(
            state, chat_id, message, dialogue_id, continuing
        )
        recorded = False
        if content is not None:
            recorded = await self._record(message, dialogue, content, kind, media, now)

        sender = message.from_user
        entry = {
            "dialogue_id": dialogue,
            "last_message_id": message.message_id,
            "thread_id": message.message_thread_id,
            "chat_type": message.chat.type,
            "is_topic": bool(getattr(message, "is_topic_message", None)),
            "user": (
                {
                    "id": sender.id,
                    "first_name": sender.first_name,
                    "last_name": sender.last_name,
                    "username": sender.username,
                }
                if sender is not None
                else None
            ),
            "text": content if isinstance(content, str) else "",
            "since": now.isoformat(),
        }
        key = str(chat_id)
        send_note = False

        def mutate(current: AwayState | None) -> AwayState | None:
            nonlocal send_note
            send_note = False
            if current is None:
                return None
            if recorded:
                old = current.pending.get(key)
                merged = dict(entry)
                if old:
                    merged["dialogue_id"] = old["dialogue_id"]
                    merged["since"] = old.get("since", entry["since"])
                current.pending[key] = merged
                current.attempts.pop(key, None)
            last = current.noted.get(key)
            if last is None or now - datetime.fromisoformat(last) >= NOTE_INTERVAL:
                current.noted[key] = now.isoformat()
                send_note = True
            return current

        await self._update(mutate)
        if send_note:
            fresh = await self.load()
            if fresh is not None:
                try:
                    await message.reply(f"<i>{note_text(fresh, now)}</i>")
                except Exception:  # noqa: BLE001 — записка не должна ронять приём
                    log.warning("away: записка не ушла (chat=%s)", chat_id, exc_info=True)
        return True

    async def note_for_click(self) -> str | None:
        """Текст записки для кнопки интерактива (ответ на нажатие); None — Альфред дома."""
        state = await self.load()
        return None if state is None else note_text(state, self.now())

    async def _resolve_dialogue(
        self,
        state: AwayState,
        chat_id: int,
        message: Message,
        dialogue_id: int | None,
        continuing: bool,
    ) -> int:
        old = state.pending.get(str(chat_id))
        if old:
            return int(old["dialogue_id"])
        fallback = dialogue_id if dialogue_id is not None else message.message_id
        if message.message_thread_id or continuing:
            return fallback
        latest = await self.store.latest_ai_dialogue(chat_id)
        return latest if latest is not None else fallback

    async def _content_for(
        self, message: Message, kind: str, text: str, media: tuple[str, Any] | None
    ) -> str | None:
        """Что записать в ai_turns вместо хода (None — писать нечего)."""
        from sa_home_bot.bot.handlers import ai as ai_handler  # noqa: PLC0415 — цикл импортов

        if kind == KIND_TEXT:
            return text
        if kind == KIND_PHOTO:
            caption = (message.caption or "").strip()
            marker = ai_handler.PHOTO_MARKER
            return f"{marker} {caption}".strip()
        if kind == KIND_STICKER:
            emoji = message.sticker.emoji if message.sticker else None
            marker = ai_handler.STICKER_MARKER
            return f"{marker} {emoji}".strip() if emoji else marker
        if kind == KIND_MEDIA and media is not None:
            return media_stub(media[0])
        return None

    async def _record(
        self,
        message: Message,
        dialogue_id: int,
        content: str,
        kind: str,
        media: tuple[str, Any] | None,
        now: datetime,
    ) -> bool:
        """Записать ход в ai_turns; файлы — на диск и в away_media. False —
        сообщение уже записано (повторная доставка апдейта)."""
        chat_id = message.chat.id
        if await self.store.ai_turn(chat_id, message.message_id) is not None:
            return False
        sender = message.from_user
        await self.store.record_ai_turn(
            chat_id,
            message.message_id,
            dialogue_id,
            "user",
            content,
            now,
            user_id=sender.id if sender else None,
            user_name=display_name(sender),
        )
        if kind == KIND_MEDIA and media is not None:
            await self._save_media(message, media, now)
        return True

    async def _save_media(
        self, message: Message, media: tuple[str, Any], now: datetime
    ) -> None:
        """Скачать файл сразу (потом его, возможно, уже не достать): путь на
        диске alfred; не вышло — остаётся file_id как запасной путь."""
        kind, obj = media
        chat_id = message.chat.id
        path: str | None = None
        try:
            self.media_dir.mkdir(parents=True, exist_ok=True)
            target = self.media_dir / f"{chat_id}_{message.message_id}.{kind}"
            await message.bot.download(obj, destination=target)
            path = str(target)
        except Exception:  # noqa: BLE001 — запасной путь: file_id
            log.warning(
                "away: не удалось скачать файл (chat=%s, msg=%s)", chat_id, message.message_id,
                exc_info=True,
            )
        await self.store.add_away_media(
            chat_id,
            message.message_id,
            kind,
            path,
            getattr(obj, "file_id", None),
            int(getattr(obj, "duration", 0) or 0),
            now,
        )

    # --- очередь возвращения (bot/away_return.py) ---

    async def finish_chat(self, chat_id: int, handled_message_id: int) -> None:
        """Снять чат с очереди после доставленного ответа. Пока шёл ответ,
        в чат могли написать ещё — тогда чат остаётся, будет следующий круг.
        Очередь опустела в фазе ``returning`` — режим снимается совсем."""
        key = str(chat_id)

        def mutate(state: AwayState | None) -> AwayState | None:
            if state is None:
                return None
            entry = state.pending.get(key)
            if entry is not None and int(entry["last_message_id"]) == handled_message_id:
                del state.pending[key]
                state.attempts.pop(key, None)
            if state.phase == PHASE_RETURNING and not state.pending:
                return None
            return state

        await self._update(mutate)

    async def record_failure(self, chat_id: int) -> bool:
        """Не вышло ответить чату. True — попытки кончились, чат снят с очереди."""
        key = str(chat_id)
        dropped = False

        def mutate(state: AwayState | None) -> AwayState | None:
            nonlocal dropped
            dropped = False
            if state is None:
                return None
            state.attempts[key] = state.attempts.get(key, 0) + 1
            if state.attempts[key] >= MAX_RETURN_ATTEMPTS:
                state.pending.pop(key, None)
                state.attempts.pop(key, None)
                dropped = True
                if state.phase == PHASE_RETURNING and not state.pending:
                    return None
            return state

        await self._update(mutate)
        return dropped

    async def mark_phase_returning(self) -> AwayState | None:
        """Потолок 24 ч — принудительное возвращение (из прохода планировщика)."""
        outcome: dict[str, Any] = {}

        def mutate(state: AwayState | None) -> AwayState | None:
            outcome.clear()
            if state is None or state.phase == PHASE_RETURNING:
                return state
            outcome["state"] = state
            if not state.pending:
                return None
            state.phase = PHASE_RETURNING
            return state

        await self._update(mutate)
        return outcome.get("state")

    async def mark_reminded(self) -> bool:
        """Отметить, что напоминание за 15 мин по текущему сроку отправлено.
        False — уже было (или режима нет): слать не нужно."""
        sent = False

        def mutate(state: AwayState | None) -> AwayState | None:
            nonlocal sent
            sent = False
            if state is None or state.phase != PHASE_AWAY:
                return state
            stamp = state.until.isoformat()
            if state.reminded_for == stamp:
                return state
            state.reminded_for = stamp
            sent = True
            return state

        await self._update(mutate)
        return sent
