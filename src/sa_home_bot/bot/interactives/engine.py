"""Движок интерактивов (Этап 47): согласие → ходы сцены → финал → эффект.

Встраивание в ход /ai (bot/handlers/ai.py::_do_ask_and_reply):

1. ``before_turn`` — ДО запроса к модели: решает, идёт ли сцена, нужна ли
   форма согласия, и собирает скрытую заметку Альфреду (рамка сцены +
   подсказка стадии + директива Ведущего + что произошло после прошлого
   действия Альфреда + журнал). Заодно отдаёт ``speech_clear`` гостя — он
   едет в каждый запрос к службе llm.
2. Альфред отвечает как обычно, ответ сразу уходит гостю.
3. ``after_turn`` — ПОСЛЕ ответа: журнал, вызов Ведущего, рамки темпа.
   Ведущий гостю НЕ виден (решение пользователя 2026-09-27: всё должно
   выглядеть как обычный диалог с Альфредом): его ``effect`` копится в
   ``Run.pending_effect`` и на следующем ходу уходит Альфреду скрытой
   подсказкой — о случившемся рассказывает сам Альфред.
4. ``flush_forms`` — последним шагом хода: формы (согласие, смена
   передатчика), которые попросили тул или финал. Форма, посланная
   посреди генерации, обогнала бы речь Альфреда (тот же приём, что
   PendingActions.flush_drafts, Этап 45).

Согласие: сцена сначала ``offered`` — невзначай заданный вопрос сценария
(«исправить проблему с коммуникацией?») с «Да»/«Нет», без намёка на игру.
«Да» — сцена стартует; «Нет» — запрет интерактивов в этой переписке
(снимается скрытой командой /interactives); без ответа час — кулдаун.
Сцены — только в личке: в общем чате подсказки сцены сбивали бы Альфреда
в разговоре с остальными. Эффект и
завершённость — на гостя глобально (base.py), так что переключатель после
завершения работает в любом чате.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import html
import json
import logging
import random
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import image_tools
from sa_home_bot.bot.interactives import cabinet as cabinet_mod
from sa_home_bot.bot.interactives import items as items_mod
from sa_home_bot.bot.interactives import radio
from sa_home_bot.bot.interactives.base import (
    STATUS_ACTIVE,
    STATUS_DECLINED,
    STATUS_DONE,
    STATUS_IDLE,
    STATUS_OFFERED,
    STATUS_PAUSED,
    InteractiveStore,
    Run,
    Scenario,
    iso,
    parse_iso,
)
from sa_home_bot.bot.interactives.cabinet import Cabinet
from sa_home_bot.bot.interactives.director import (
    ACTION_CHAT,
    ROLE_DIRECTOR,
    DirectorDecision,
    _json_object,
    ask_director,
    ask_features,
)
from sa_home_bot.bot.interactives.transylvania import TZ as TRANSYLVANIA_TZ
from sa_home_bot.bot.interactives.transylvania import PHASE_NIGHT, Outside, Transylvania
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings, reminder_reason
from sa_home_bot.db.store import Store
from sa_home_bot.llm.prompt import wrap_system_directive
from sa_home_bot.proto.messages import Address, ProtoError

log = logging.getLogger(__name__)

REGISTRY: dict[str, Scenario] = {radio.RADIO.id: radio.RADIO}

LLM_NODE = "mycraft"
LLM_SERVICE = "llm"
ACTION_SET_SPEECH_CLEAR = "set_speech_clear"
SET_SPEECH_TIMEOUT_S = 15.0

OFFER_TTL = timedelta(hours=1)
DECLINE_COOLDOWN = timedelta(hours=24)
NOTES_KEEP = 6
NOTE_TRANSCRIPT_LINES = 8

_TAG_RE = re.compile(r"<[^>]+>")

# callback_data «ia:<сценарий>:<кнопка>» — всё остальное в app_state.
CALLBACK_PREFIX = "ia"
BTN_PLAY = "p"
BTN_LATER = "l"
BTN_NEVER = "n"
BTN_EXIT = "x"
BTN_SWAP = "s"
BTN_KEEP = "k"
BTN_RETURN_OLD = "r"
BTN_INSTALL_NEW = "i"
BTN_TOGGLE_KEEP = "o"

FORM_OFFER = "offer"
FORM_SWAP = "swap"
FORM_RETURN = "return"
FORM_REINSTALL = "reinstall"
FORM_ITEM = "item"  # форма тула item_action (вещь поместья)

# Форма согласия нарочно не выдаёт, что это игра (решение пользователя
# 2026-09-27): невзначай заданный вопрос сценария и «Да»/«Нет». После
# ответа форма лишь фиксирует его — тоже без слов «сценка»/«игра».
# «Нет» — сценок в этом чате больше не будет (снять — скрытая /interactives).
OFFER_YES_SUFFIX = "\n<i>— Да</i>"
OFFER_NO_SUFFIX = "\n<i>— Нет</i>"
OFFER_EXPIRED_SUFFIX = "\n<i>— Вопрос уже неактуален</i>"
# Кнопка выхода была под сообщениями рассказчика до v0.115.3 — рассказчика
# больше нет, обработка оставлена для уже разосланных сообщений.
EXIT_ALERT = "Хорошо."

# Снимки кабинета (Этап 49.2): рисует служба llm на mycraft, будит его и
# ~30 с грузит CPU — поэтому не больше одного снимка в чате одновременно.
# Суточный потолок на чат; 0 — без лимита (снят по просьбе пользователя 2026-09-30).
PHOTO_DAILY_LIMIT = 0
PHOTO_PURPOSE = "photo"
PORTRAIT_PURPOSE = "portrait"
# Фазы снимка — опросом chat_progress службы llm (llm/service.py::
# IMAGE_PHASE_*), статусами хода /ai.
ACTION_CHAT_PROGRESS = "chat_progress"
PHOTO_PHASE_POLL_S = 1.0
PHOTO_PHASE_STATUS = {
    "compose": cabinet_mod.PHOTO_STATUS_AIMING,
    "draw": cabinet_mod.PHOTO_STATUS_DEVELOPING,
}
# Подпись снимка — реплика Альфреда (как bot/handlers/ai.py::ALFRED_PREFIX).
ALFRED_PHOTO_PREFIX = "<b>Альфред:</b> "
# Больше двух особенностей промптер под 77 токенов CLIP не удерживает:
# живые снимки 2026-09-30 из четырёх сохранили по две.
PHOTO_FEATURES_IN_FRAME = 2
PHOTO_CAPTION = "Кабинет"
# Подпись или focus про весь кабинет — общий вид, а не крупный план
# (tool_take_photo, _photo). Хвост не важен: «Вид кабинета после инцидента»,
# «кабинет с корпусом передатчика на полу» — тоже общий вид; крупным планом
# их снимали без радио на столе (живой прогон 2026-10-01).
GENERAL_VIEW_RE = re.compile(
    r"^(?:(?:мой|наш|твой|весь)\s+)?(?:общ\w+\s+(?:вид|план)\w*"
    r"|(?:(?:общ\w+\s+)?(?:вид|план|снимок|фото)\w*\s+)?(?:(?:на|всего|мо\w+|тво\w+)\s+)*"
    r"(?:кабинет|комнат)\w*)",
    re.I,
)
# Вид ИЗ окна («вид из окна», «окно с видом на горизонт», «окно и вид за ним»,
# «за окном») — не крупный план предмета, хотя focus непустой: ему нужен свет
# общего вида с окном и луной по фазе (стенд 2026-10-10: как «крупный» он получал
# _CLOSEUP_LIGHT — без окна и без луны). «Подоконник», «оконная рама с трещиной»
# сюда не попадают: это предметы, их снимают крупно.
WINDOW_VIEW_RE = re.compile(
    r"\bиз\s+(?:\w+\s+){0,2}окн|\bза\s+(?:\w+\s+){0,2}окн|\bокн\w*\s+(?:и|с)\s+вид"
    r"|window\s+view|view\s+(?:from|through|out)",
    re.I,
)
# Кадры по ходу сцены (Ведущий, поле photo): переход стадии и финал снимаются
# всегда, прочие — не чаще раза в PHOTO_SCENE_GAP_TURNS ходов.
PHOTO_SCENE_GAP_TURNS = 3
PHOTO_SCENE_CAPTION = "В кабинете"
# Настроение кадра (Ведущий, director.MOODS) → модель и LoRA службы рисования.
# Связки и вес — стенд тем 2026-09-30, одобрен пользователем; «обычно» — эталон C.
# «Жуть» на giger убрана (пользователь 2026-10-08: рисует непрошеного
# человекоподобного «чёрта» почти в каждом кадре и не нравится по виду).
MOOD_PRESETS: dict[str, tuple[str, str, float]] = {
    "rot": ("revanim", "rottech", 0.8),
    "eldritch": ("ghostmix", "eldritch", 0.8),
}

# Сюжетные предметы (Этап 49.3, items.py). Карточка предмета — фото с
# кнопкой «it:<id предмета>:<кнопка>»; портрет рисует служба llm
# (item_portrait), в кадры он вставляется пикселями (generate_image, paste).
ITEM_CALLBACK_PREFIX = "it"
# Кнопка несёт намерение, а не «переключить»: речь можно сменить и формами
# swap_radio, тогда надпись на старой карточке устарела бы.
ITEM_BTN_PUT = "p"
ITEM_BTN_REMOVE = "r"
ITEM_BTN_CARD = "c"  # кнопка описи: принести карточку вещи
ITEM_BTN_ACTIONS = "a"  # карточка → перечень действий
ITEM_BTN_BACK = "b"  # перечень действий → карточка
ITEM_BTN_KEEP = "k"  # форма тула: «Оставить как есть» (+ код действия)
# Откуда нажата кнопка — префикс кода: из описи /items (перечень вернётся в
# опись) или из формы тула item_action (форма закрывается итогом). Коды
# действий вида поэтому не начинаются с этих букв.
ITEM_FROM_INVENTORY = "i"
ITEM_FROM_FORM = "f"
ITEM_PURPOSE = "item"
ACTION_ITEM_PORTRAIT = "item_portrait"
ITEMS_SHOWN_MAX = 3
# Новый передатчик (после смены) — один на всех: обычный архетип без черт,
# зерно из стенда (b1_33). Готовность вырезки — флаг в app_state.
NEW_RADIO_CUT_KEY = "radio-new"
NEW_RADIO_SEED = 33
NEW_RADIO_READY_KEY = "item_cut_ready:radio-new"
# Крупный план предмета: фон без предмета (его вставят) — иначе промптер
# нарисует второй, свой передатчик.
ITEM_CLOSEUP_RU = "Крупный план: пустая столешница старого письменного стола"
ITEM_PENDING_TAG = "pending"

# Живые кнопки (пользователь 2026-10-02): формы сцены и смены радиостанции
# и раскрытый перечень действий карточки. Следующее сообщение гостя — «гость
# проигнорировал»: у форм кнопки снимаются, перечень сворачивается обратно
# в карточку. Иначе старая форма оставалась нажимаемой рядом с новой.
LIVE_BUTTONS_KEY = "live_buttons:{chat_id}"
LIVE_FORM = "form"
LIVE_MENU = "menu"

OPT_IN_TEXT = "Интерактивы в этом чате включены."
OPT_OUT_TEXT = "Интерактивы в этом чате выключены. Включить — /interactives on."


def parse_callback(data: str | None) -> tuple[str, str] | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or parts[1] not in REGISTRY:
        return None
    return parts[1], parts[2]


def _cb(scenario: str, button: str) -> str:
    return f"{CALLBACK_PREFIX}:{scenario}:{button}"


def _keyboard(scenario: str, rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=_cb(scenario, btn)) for text, btn in row]
            for row in rows
        ]
    )


def offer_keyboard(scenario: str) -> InlineKeyboardMarkup:
    return _keyboard(
        scenario,
        [
            [("Да", BTN_PLAY), ("Нет", BTN_NEVER)],
        ],
    )


def swap_keyboard(scenario: str) -> InlineKeyboardMarkup:
    return _keyboard(scenario, [[("Заменить", BTN_SWAP), ("Оставить старое", BTN_KEEP)]])


def toggle_keyboard(scenario: str, button: str, label: str) -> InlineKeyboardMarkup:
    return _keyboard(scenario, [[(label, button), ("Оставить как есть", BTN_TOGGLE_KEEP)]])


def _item_button(item_id: int, label: str, button: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text=label, callback_data=f"{ITEM_CALLBACK_PREFIX}:{item_id}:{button}")
    ]


def item_keyboard(kind: items_mod.ItemKind, item_id: int) -> InlineKeyboardMarkup | None:
    """Под карточкой — только «Действия», если вид объявил действия."""
    if not kind.actions:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[_item_button(item_id, radio.ITEM_ACTIONS_LABEL, ITEM_BTN_ACTIONS)]
    )


def item_actions_keyboard(
    kind: items_mod.ItemKind, item_id: int, place: str, origin: str = ""
) -> InlineKeyboardMarkup:
    """Перечень действий (заменяет карточку или опись) и «Назад». Кнопка
    несёт намерение, а не «переключить»: состояние можно сменить и формой.
    ``origin`` — откуда открыт перечень (``ITEM_FROM_INVENTORY`` — из описи
    /items: «Назад» и действие возвращают опись)."""
    rows = [
        _item_button(item_id, action.label, origin + action.code)
        for action in kind.available(place)
    ]
    rows.append(_item_button(item_id, radio.ITEM_BACK_LABEL, origin + ITEM_BTN_BACK))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_actions_caption(kind: items_mod.ItemKind, place: str) -> str:
    return radio.ITEM_ACTIONS_CAPTION.format(
        name=html.escape(kind.name),
        where=radio.ITEM_PLACE_INSTALLED
        if place == items_mod.PLACE_DESK
        else radio.ITEM_PLACE_STORED,
    )


_NO_BUTTONS = InlineKeyboardMarkup(inline_keyboard=[])


def item_form_keyboard(item_id: int, action: items_mod.ItemAction) -> InlineKeyboardMarkup:
    """Форма тула item_action: согласие и «Оставить как есть»."""
    keep = ITEM_FROM_FORM + ITEM_BTN_KEEP + action.code
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _item_button(item_id, action.confirm, ITEM_FROM_FORM + action.code)
            + _item_button(item_id, radio.ITEM_FORM_KEEP, keep)
        ]
    )


def inventory_keyboard(items: list[dict[str, Any]]) -> InlineKeyboardMarkup | None:
    """Опись: по строке на вещь — принести карточку и, если вид объявил
    действия, «Действия» прямо из описи (особенные вещи — всегда)."""
    rows = []
    for item in items:
        kind = items_mod.KINDS.get(item["type"])
        if kind is None:
            continue
        row = _item_button(int(item["id"]), f"{kind.icon} {kind.name}", ITEM_BTN_CARD)
        if kind.actions:
            row += _item_button(
                int(item["id"]), radio.ITEM_ACTIONS_LABEL, ITEM_FROM_INVENTORY + ITEM_BTN_ACTIONS
            )
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def inventory_text(entries: list[tuple[items_mod.ItemKind, dict[str, Any], str]]) -> str:
    """Опись вещей гостя: (вид, вещь, место) → текст в голосе Альфреда."""
    if not entries:
        return items_mod.INVENTORY_EMPTY
    blocks = [items_mod.INVENTORY_TITLE]
    for kind, item, place in entries:
        line = items_mod.INVENTORY_LINE.format(
            icon=kind.icon,
            name=html.escape(kind.name),
            where=items_mod.PLACE_RU.get(place, place),
        )
        traits = kind.traits_ru(item.get("traits") or [])
        if traits:
            line += "\n" + items_mod.INVENTORY_TRAITS.format(traits=html.escape("; ".join(traits)))
        blocks.append(line)
    blocks.append(items_mod.INVENTORY_HINT)
    return "\n\n".join(blocks)


def parse_item_callback(data: str | None) -> tuple[int, str] | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != ITEM_CALLBACK_PREFIX or not parts[1].isdigit():
        return None
    return int(parts[1]), parts[2]


def grow_item(
    kind: items_mod.ItemKind,
    run: Run,
    decision: DirectorDecision | None,
    *,
    key_moment: bool,
) -> str | None:
    """Новая черта предмета сцены за ход: от Ведущего (уже проверена по
    словарю вида), а если он промолчал на переходе стадии или в финале —
    следующая по лестнице. Не больше одной за ход. Возвращает добавленную."""
    new = decision.item_trait if decision is not None else None
    if new in run.item_traits:
        new = None
    if new is None and key_moment:
        new = kind.next_trait(run.item_traits)
    if new is None:
        return None
    run.item_traits.append(new)
    return new


def finale_allowed(scenario: Scenario, run: Run) -> bool:
    """Финал — только с последней стадии и не раньше минимума ходов
    (считая текущий, уже отыгранный ход)."""
    return run.stage >= scenario.last_stage and run.turns_total + 1 >= (
        scenario.min_turns_before_finale
    )


def apply_decision(
    scenario: Scenario,
    run: Run,
    decision: DirectorDecision | None,
    *,
    choose: Callable[[tuple[str, ...]], str] = random.choice,
) -> str | None:
    """Рамки темпа поверх решения Ведущего. Возвращает текст эффекта для
    гостя (или None). ``decision=None`` — Ведущий не ответил: ход всё равно
    засчитывается, и мягкий потолок продвигает сцену сам."""
    allowed = finale_allowed(scenario, run)
    run.turns_total += 1
    run.turns_on_stage += 1
    if decision is not None and not decision.active:
        run.status = STATUS_IDLE
        return None
    effect = decision.effect if decision is not None else None
    if decision is not None:
        if decision.directive:
            run.directive = decision.directive
        if decision.note:
            run.notes.append(decision.note)
            del run.notes[:-NOTES_KEEP]
        if decision.mood:
            run.mood = decision.mood
    if run.finale:
        run.last_effect = effect or run.last_effect
        return effect

    wanted = decision.stage if decision is not None else run.stage
    if run.turns_on_stage < scenario.min_turns_on_stage:
        wanted = run.stage
    target = max(run.stage, min(wanted, run.stage + 1, scenario.last_stage))
    if (
        target == run.stage
        and run.stage < scenario.last_stage
        and run.turns_on_stage >= scenario.stage_soft_cap
    ):
        target = run.stage + 1
    if target != run.stage:
        run.stage = target
        run.turns_on_stage = 0
        # Директива Ведущего писалась под прошлую стадию — новая подсказка
        # лестницы важнее.
        if decision is None:
            run.directive = None

    wants_finale = decision is not None and decision.finale
    if allowed and (wants_finale or run.turns_on_stage >= scenario.stage_soft_cap):
        run.finale = True
        # Поломку можно задать заранее (вернуть гостю прежнюю после сброса
        # сцены) — тогда она главнее выдумки Ведущего, а его эффект, писанный
        # под свою поломку, заменяем шаблонным.
        preset = run.finale_fault
        run.finale_fault = preset or (
            decision.finale_fault
            if decision is not None and decision.finale_fault
            else choose(scenario.fallback_faults)
        )
        if preset or not effect:
            effect = f"Внутри радиостанции обнаруживается страшное: {run.finale_fault}."
    run.last_effect = effect or run.last_effect
    return effect


def build_scene_note(scenario: Scenario, run: Run, place: str | None = None) -> str:
    parts = [scenario.scene_frame]
    if place:
        parts.append(f"Где ты: {place}")
    kind = items_mod.KINDS.get(scenario.item_kind or "")
    if kind is not None and run.item_traits:
        parts.append(
            "Как сейчас выглядит радиостанция: " + "; ".join(kind.traits_ru(run.item_traits)) + "."
        )
    if run.finale:
        parts.append(scenario.finale_directive.format(fault=run.finale_fault))
    else:
        parts.append("Сейчас: " + scenario.ladder[min(run.stage, scenario.last_stage)])
        if scenario.nudges:
            parts.append(scenario.nudges[run.turns_total % len(scenario.nudges)])
    if run.directive:
        parts.append("Подсказка на этот ход: " + run.directive)
    if run.pending_effect:
        parts.append(
            "После твоего прошлого действия произошло вот что: "
            f"{run.pending_effect}\nРасскажи об этом собеседнику в этом ответе "
            "сам, своими словами — как то, что ты сейчас увидел, услышал или "
            "почувствовал. Не говори, что тебе это подсказали."
        )
    if run.transcript:
        lines = "\n".join(run.transcript[-NOTE_TRANSCRIPT_LINES:])
        parts.append("Что уже было в сцене (журнал):\n" + lines)
    return "\n\n".join(parts)


@dataclass
class TurnPlan:
    chat_id: int
    user_id: int | None
    user_text: str
    speech_clear: bool
    # Свежий флаг для каждого раунда запроса к llm (llm_chat.py): гость мог
    # сменить передатчик кнопкой, пока ход ещё идёт.
    speech_clear_now: Callable[[], Awaitable[bool]] | None = None
    note: str | None = None
    scenario: str | None = None
    scene: bool = False  # ход сцены: после ответа — Ведущий
    offered: bool = False  # после ответа — форма согласия
    force_swap_form: bool = False  # финал: форма смены гарантирована


@dataclass
class _Queued:
    forms: list[tuple[str, str]] = field(default_factory=list)  # (сценарий, форма)
    user_id: int | None = None  # чей ход: его следующее сообщение снимет кнопки
    # Формы тула item_action: (id вещи, код действия).
    items: list[tuple[int, str]] = field(default_factory=list)


@dataclass(frozen=True)
class _ItemShot:
    """Предмет в кадре: вырезка ``key`` на ноде llm, место ``place``,
    ``tag`` — версия облика (для ключа повторного показа общего вида)."""

    kind: items_mod.ItemKind
    key: str
    place: str
    tag: str


class Interactives:
    """Сервис интерактивов бота (один на процесс, см. app.py)."""

    def __init__(
        self,
        store: Store,
        notifier: Any,
        settings: Settings,
        get_node_link: Callable[[], ServiceLink | None],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        choose: Callable[[tuple[str, ...]], str] = random.choice,
        rng: Callable[[], float] = random.random,
        transylvania: Transylvania | None = None,
    ) -> None:
        self._transylvania = transylvania or Transylvania()
        self._photo_busy: set[int] = set()
        # Снимок по просьбе: задача на чат (ход /ai её дожидается, wait_photo)
        # и куда показывать его статусы, пока ход ждёт.
        self._photo_jobs: dict[int, asyncio.Task] = {}
        self._photo_status: dict[int, Callable[[str], Awaitable[None]]] = {}
        # Фоновый снимок (сцены), к которому присоединился ход с take_photo:
        # реплику к нему скажет Альфред этого хода (describe, dialogue_id,
        # trigger_message_id) — ответ приходит со снимком, а не до него.
        self._photo_join: dict[int, dict[str, Any]] = {}
        self._photo_described: set[int] = set()
        self._photo_tasks: set[asyncio.Task] = set()
        # Портрет к приветствию (start_greeting_portrait): чаты, где рисуется.
        self._portrait_busy: set[int] = set()
        self._store = store
        self._state = InteractiveStore(store)
        self._notifier = notifier
        self._settings = settings
        self._get_node_link = get_node_link
        self._now = now
        self._choose = choose
        self._rng = rng
        self._queued: dict[int, _Queued] = {}

    # --- эффект «чистая речь» ---

    async def speech_clear(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return await self._state.get_effect(radio.EFFECT_SPEECH_CLEAR, user_id) == "1"

    def _pinned(self, chat_id: int) -> bool:
        return chat_id in self._settings.llm.speech_therapy_pinned_chat_ids

    # --- ход /ai ---

    async def before_turn(
        self, chat_id: int, user_id: int | None, user_text: str, *, is_private: bool
    ) -> TurnPlan:
        plan = TurnPlan(
            chat_id=chat_id,
            user_id=user_id,
            user_text=user_text,
            speech_clear=await self.speech_clear(user_id),
            speech_clear_now=functools.partial(self.speech_clear, user_id),
            note=await self.photo_note(chat_id),
        )
        # Гость пишет, не нажав кнопок, — прежние формы проигнорированы.
        await self.dismiss_buttons(chat_id, user_id)
        if not is_private or user_id is None or self._pinned(chat_id):
            return plan
        if await self._state.is_opted_out(chat_id):
            return plan
        scenario = radio.RADIO
        run = await self._state.load_run(chat_id, scenario.id)
        run = await self._expire_offer(run)
        triggered = bool(scenario.trigger_re.search(user_text))

        if run is not None and run.status == STATUS_IDLE and triggered:
            run.status = STATUS_ACTIVE  # согласие уже было — продолжаем молча
            await self._state.save_run(run)
        if run is not None and run.status == STATUS_ACTIVE:
            plan.scenario = scenario.id
            plan.scene = True
            cab = await cabinet_mod.load(self._store, user_id)
            outside = await self._transylvania.outside(self._now())
            place = f"{cab.describe_ru()} Сейчас {outside.ru()}."
            scene_note = build_scene_note(scenario, run, place)
            plan.note = f"{scene_note}\n\n{plan.note}" if plan.note else scene_note
            plan.force_swap_form = run.finale and not run.finale_form_sent
            return plan
        if triggered and await self._may_offer(scenario, run, user_id):
            await self._offer(scenario, run, chat_id, user_id, user_text)
            plan.scenario = scenario.id
            plan.offered = True
        return plan

    async def _may_offer(self, scenario: Scenario, run: Run | None, user_id: int) -> bool:
        if await self._state.is_completed(scenario.id, user_id):
            return False  # дальше только переключатель через swap_radio
        if run is None:
            return True
        if run.status in (STATUS_OFFERED, STATUS_ACTIVE, STATUS_DONE):
            return False
        if run.status == STATUS_DECLINED:
            until = parse_iso(run.declined_until)
            return until is None or until <= self._now()
        return True  # paused / idle без триггера сюда не доходит

    async def _offer(
        self,
        scenario: Scenario,
        run: Run | None,
        chat_id: int,
        user_id: int,
        user_text: str | None,
    ) -> None:
        if run is None:
            run = Run(scenario=scenario.id, chat_id=chat_id, user_id=user_id)
        run.status = STATUS_OFFERED
        run.offered_at = iso(self._now())
        if user_text:
            run.log("Гость", user_text)
        await self._state.save_run(run)
        self._queue(chat_id, scenario.id, FORM_OFFER, user_id)

    async def _expire_offer(self, run: Run | None) -> Run | None:
        if run is None or run.status != STATUS_OFFERED:
            return run
        offered = parse_iso(run.offered_at)
        if offered is not None and offered + OFFER_TTL <= self._now():
            run.status = STATUS_DECLINED
            run.declined_until = iso(self._now() + DECLINE_COOLDOWN)
            await self._state.save_run(run)
        return run

    async def after_turn(
        self,
        plan: TurnPlan,
        reply: str,
        *,
        dialogue_id: int | None,
        message_thread_id: int | None = None,
    ) -> None:
        if plan.scenario is None:
            return
        scenario = REGISTRY[plan.scenario]
        run = await self._state.load_run(plan.chat_id, scenario.id)
        if run is None:
            return
        if plan.offered:
            run.log("Альфред", reply)
            await self._state.save_run(run)
            return
        if not plan.scene or run.status != STATUS_ACTIVE:
            return
        run.log("Гость", plan.user_text)
        run.log("Альфред", reply)
        # Прошлое событие Альфред уже пересказал этим ответом.
        run.pending_effect = None
        cab = await cabinet_mod.load(self._store, run.user_id)
        decision = await self._ask_director(scenario, run, cab)
        stage_before, finale_before = run.stage, run.finale
        if decision is not None and decision.cabinet_add:
            # Перечитываем перед записью: снимок в фоне мог дописать своё.
            cab = await cabinet_mod.load(self._store, run.user_id)
            # Первые особенности (кабинет ещё пуст) — постоянные, прочие —
            # следы сцены.
            if cab.add(list(decision.cabinet_add), scene=bool(cab.features)):
                await cabinet_mod.save(self._store, cab)
        effect = apply_decision(scenario, run, decision, choose=self._choose)
        if effect:
            run.log("Событие", effect)
            run.pending_effect = effect
        key_moment = run.stage != stage_before or run.finale != finale_before
        kind = items_mod.KINDS.get(scenario.item_kind or "")
        if kind is not None and run.status == STATUS_ACTIVE:
            if run.item_seed is None:
                run.item_seed = items_mod.new_seed()
            trait = grow_item(kind, run, decision, key_moment=key_moment)
            if trait is not None:
                log.info("interactives: у передатчика новая черта %r (chat=%s)", trait, run.chat_id)
        focus = scene_photo_focus(run, decision, key_moment=key_moment)
        if (
            focus is not None
            and run.status == STATUS_ACTIVE
            and not await self._photo_limit_reached(run.chat_id)
        ):
            started = await self._start_photo(
                run.chat_id,
                run.user_id,
                focus=focus,
                caption=PHOTO_SCENE_CAPTION,
                happening=run.last_effect,
                mood=run.mood,
                alfred=decision.alfred if decision is not None else None,
                outside=await self._transylvania.outside(self._now()),
                message_thread_id=message_thread_id,
                trigger_message_id=None,
                dialogue_id=dialogue_id,
            )
            if started:
                run.photo_turn = run.turns_total
        await self._state.save_run(run)

    async def _ask_director(
        self, scenario: Scenario, run: Run, cab: Cabinet
    ) -> DirectorDecision | None:
        node_link = self._get_node_link()
        if node_link is None:
            return None
        outside = await self._transylvania.outside(self._now())
        return await ask_director(
            node_link,
            Address(node=LLM_NODE, service=LLM_SERVICE),
            self._settings.llm.request_timeout_s,
            scenario,
            run,
            finale_allowed=finale_allowed(scenario, run),
            place=cab.describe_ru(),
            outside=outside.ru(),
            need_features=0 if cab.features else cabinet_mod.FIRST_FEATURES,
        )

    # --- формы ---

    def _queue(self, chat_id: int, scenario: str, form: str, user_id: int | None) -> None:
        queued = self._queued.setdefault(chat_id, _Queued())
        queued.user_id = user_id if user_id is not None else queued.user_id
        if (scenario, form) not in queued.forms:
            queued.forms.append((scenario, form))

    async def flush_forms(
        self,
        chat_id: int,
        plan: TurnPlan | None = None,
        *,
        dialogue_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> None:
        if plan is not None and plan.force_swap_form and plan.scenario is not None:
            run = await self._state.load_run(chat_id, plan.scenario)
            if run is not None and run.status == STATUS_ACTIVE and run.finale:
                self._queue(chat_id, plan.scenario, FORM_SWAP, plan.user_id)
        queued = self._queued.pop(chat_id, None)
        if queued is None:
            return
        outgoing: list[tuple[str, InlineKeyboardMarkup, dict[str, Any]]] = [
            (*self._render_form(scenario_id, form), {"scenario": scenario_id, "form": form})
            for scenario_id, form in queued.forms
        ]
        for item_id, code in queued.items:
            item = await self._store.item_by_id(item_id)
            kind = items_mod.KINDS.get(item["type"]) if item is not None else None
            action = kind.action(code) if kind is not None else None
            if action is not None:
                outgoing.append(
                    (action.form, item_form_keyboard(item_id, action), {"form": FORM_ITEM})
                )
        for text, markup, extra in outgoing:
            message_id = await self._notifier.send_direct(
                chat_id, text, reply_markup=markup, message_thread_id=message_thread_id
            )
            if message_id is None:
                log.warning("interactives: форма %s не ушла (chat=%s)", extra, chat_id)
                continue
            await self._remember_buttons(chat_id, message_id, queued.user_id, LIVE_FORM, **extra)
            scenario_id, form = extra.get("scenario", ""), extra["form"]
            if form == FORM_SWAP:
                run = await self._state.load_run(chat_id, scenario_id)
                if run is not None:
                    run.finale_form_sent = True
                    await self._state.save_run(run)
            await self._store.record_ai_turn(
                chat_id,
                message_id,
                dialogue_id if dialogue_id is not None else message_id,
                "assistant",
                _plain(text),
                self._now(),
            )

    def discard_forms(self, chat_id: int) -> None:
        self._queued.pop(chat_id, None)

    # --- живые кнопки: следующее сообщение гостя их гасит ---

    async def _live_buttons(self, chat_id: int) -> list[dict[str, Any]]:
        raw = await self._store.get_state(LIVE_BUTTONS_KEY.format(chat_id=chat_id))
        try:
            entries = json.loads(raw) if raw else []
        except ValueError:
            return []
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    async def _save_live_buttons(self, chat_id: int, entries: list[dict[str, Any]]) -> None:
        await self._store.set_state(
            LIVE_BUTTONS_KEY.format(chat_id=chat_id), json.dumps(entries, ensure_ascii=False)
        )

    async def _remember_buttons(
        self, chat_id: int, message_id: int, user_id: int | None, kind: str, **extra: Any
    ) -> None:
        entries = [e for e in await self._live_buttons(chat_id) if e.get("m") != message_id]
        entries.append({"m": message_id, "u": user_id, "k": kind, **extra})
        await self._save_live_buttons(chat_id, entries)

    async def _forget_buttons(self, chat_id: int, message_id: int | None) -> None:
        if message_id is None:
            return
        entries = await self._live_buttons(chat_id)
        left = [e for e in entries if e.get("m") != message_id]
        if len(left) != len(entries):
            await self._save_live_buttons(chat_id, left)

    async def dismiss_buttons(self, chat_id: int, user_id: int | None) -> int:
        """Гость написал, не нажав кнопку, — «проигнорировано гостем»: у форм
        кнопки снимаются (решение остаётся прежним; согласие на сцену можно
        будет спросить снова), раскрытый перечень действий сворачивается в
        карточку. Чужое сообщение в общем чате чужих форм не трогает.
        Возвращает, сколько сообщений погашено."""
        entries = await self._live_buttons(chat_id)
        if not entries:
            return 0
        mine = [e for e in entries if e.get("u") in (None, user_id)]
        if not mine:
            return 0
        await self._save_live_buttons(chat_id, [e for e in entries if e not in mine])
        for entry in mine:
            try:
                await self._dismiss_one(chat_id, entry)
            except Exception:  # noqa: BLE001 — косметика, ход гостя важнее
                log.info(
                    "interactives: кнопки не сняты (chat=%s, %s)", chat_id, entry, exc_info=True
                )
        return len(mine)

    async def _dismiss_one(self, chat_id: int, entry: dict[str, Any]) -> None:
        message_id = int(entry["m"])
        if entry.get("k") == LIVE_MENU:
            item = await self._store.item_by_id(int(entry.get("item") or 0))
            kind = items_mod.KINDS.get(item["type"]) if item is not None else None
            if item is None or kind is None:
                await self._notifier.edit_markup(chat_id, message_id, None)
                return
            owner = int(item["owner_user_id"])
            if entry.get("inv"):
                text, markup = await self.inventory(owner)
                await self._notifier.edit_caption(
                    chat_id, message_id, text, reply_markup=markup, photo=False
                )
                return
            place = await self.item_place(owner, item)
            await self._notifier.edit_caption(
                chat_id,
                message_id,
                item_caption(kind, item, place == items_mod.PLACE_DESK),
                reply_markup=item_keyboard(kind, int(item["id"])),
                photo=bool(entry.get("photo")),
            )
            return
        await self._notifier.edit_markup(chat_id, message_id, None)
        if entry.get("form") == FORM_OFFER and entry.get("scenario") in REGISTRY:
            # Согласие без ответа — как истёкшее, но без кулдауна: заговорит
            # гость о радиостанции снова — спросим снова.
            run = await self._state.load_run(chat_id, str(entry["scenario"]))
            if run is not None and run.status == STATUS_OFFERED:
                run.status = STATUS_DECLINED
                run.declined_until = iso(self._now())
                await self._state.save_run(run)

    @staticmethod
    def _render_form(scenario_id: str, form: str) -> tuple[str, InlineKeyboardMarkup]:
        scenario = REGISTRY[scenario_id]
        if form == FORM_OFFER:
            return scenario.offer_text, offer_keyboard(scenario_id)
        if form == FORM_SWAP:
            return radio.SWAP_FORM_TEXT, swap_keyboard(scenario_id)
        if form == FORM_RETURN:
            return radio.RETURN_FORM_TEXT, toggle_keyboard(scenario_id, BTN_RETURN_OLD, "Вернуть")
        return radio.REINSTALL_FORM_TEXT, toggle_keyboard(scenario_id, BTN_INSTALL_NEW, "Поставить")

    # --- тул swap_radio ---

    async def tool_swap_radio(
        self, chat_id: int | None, user_id: int | None, *, is_private: bool
    ) -> str:
        if chat_id is None or user_id is None:
            return radio.TOOL_UNAVAILABLE
        if self._pinned(chat_id):
            return radio.TOOL_PINNED
        scenario = radio.RADIO
        if await self._state.is_completed(scenario.id, user_id):
            # Прошедшим сцену до Этапа 49.3 — старое радио предметом.
            await self._owned_item(user_id, items_mod.RADIO)
            if await self.speech_clear(user_id):
                self._queue(chat_id, scenario.id, FORM_RETURN, user_id)
                return radio.TOOL_RETURN_FORM
            self._queue(chat_id, scenario.id, FORM_REINSTALL, user_id)
            return radio.TOOL_REINSTALL_FORM
        if not is_private or await self._state.is_opted_out(chat_id):
            return radio.TOOL_OPTED_OUT
        run = await self._state.load_run(chat_id, scenario.id)
        run = await self._expire_offer(run)
        if run is not None and run.status == STATUS_ACTIVE and run.finale:
            self._queue(chat_id, scenario.id, FORM_SWAP, user_id)
            return radio.TOOL_SWAP_FORM
        if run is not None and run.status in (STATUS_ACTIVE, STATUS_OFFERED):
            return radio.TOOL_NOT_YET
        if run is not None and run.status == STATUS_IDLE:
            run.status = STATUS_ACTIVE
            await self._state.save_run(run)
            return radio.TOOL_NOT_YET
        if await self._may_offer(scenario, run, user_id):
            await self._offer(scenario, run, chat_id, user_id, None)
            return radio.TOOL_OFFER
        return radio.TOOL_NOT_YET

    # --- тул take_photo: снимок кабинета (Этап 49.2) ---

    async def tool_take_photo(
        self,
        chat_id: int | None,
        user_id: int | None,
        args: dict[str, Any],
        *,
        message_thread_id: int | None = None,
        trigger_message_id: int | None = None,
        dialogue_id: int | None = None,
    ) -> str:
        """Альфред снимает то, что видит у себя в кабинете. Снимок рисуется в
        фоне (~30 с) и приходит сам — ответ Альфреда его не ждёт («сейчас
        сниму»). Кабинет у каждого гостя свой (cabinet.py); без особенностей
        первые придумывает Ведущий. Общий вид без изменений — повторный
        показ прежнего снимка, mycraft не будим."""
        if chat_id is None or user_id is None or not hasattr(self._notifier, "send_photo_ex"):
            return cabinet_mod.TOOL_PHOTO_UNAVAILABLE
        if chat_id in self._photo_busy:
            return await self._join_photo(chat_id, user_id, trigger_message_id, dialogue_id)
        focus = args.get("focus") if isinstance(args.get("focus"), str) else ""
        focus = " ".join(focus.split())
        caption = args.get("caption") if isinstance(args.get("caption"), str) else ""
        caption = " ".join(caption.split())[:80]
        selfie = args.get("selfie") is True or str(args.get("selfie")).lower() == "true"
        # «Сними себя» словом в focus/caption без selfie — тоже снимок себя.
        if cabinet_mod.SELFIE_FOCUS_RE.match(focus):
            selfie, focus = True, ""
        elif not focus and cabinet_mod.SELFIE_FOCUS_RE.match(caption):
            selfie = True
        # Модель не всегда заполняет focus/expect: «Вид из окна» только в
        # подписи давал общий вид кабинета, а меч без expect — несверенную
        # тарелку. Подпись о конкретном — это и есть focus, focus — то, что
        # должно выйти на снимке.
        if GENERAL_VIEW_RE.match(focus):
            focus = ""
        elif not focus and caption and not selfie and not GENERAL_VIEW_RE.match(caption):
            focus = caption
        caption = caption or (cabinet_mod.SELFIE_CAPTION if selfie else PHOTO_CAPTION)
        if not selfie and await self._wants_stored_item(user_id, f"{focus} {caption}"):
            await self.tool_manor_items(
                chat_id,
                user_id,
                show=items_mod.RADIO.type,
                message_thread_id=message_thread_id,
                trigger_message_id=trigger_message_id,
            )
            return radio.TOOL_PHOTO_STORED_ITEM
        expect = photo_expect(args.get("expect")) or ([focus] if focus else [])
        now = self._now()
        outside = await self._transylvania.outside(now)
        cab = await cabinet_mod.load(self._store, user_id)
        run = await self._state.load_run(chat_id, radio.SCENARIO_ID)
        in_scene = run is not None and run.status == STATUS_ACTIVE
        if not in_scene and cab.end_scene():
            await cabinet_mod.save(self._store, cab)
        happening = run.last_effect if in_scene else None
        mood = run.mood if in_scene else None
        reuse_key = None
        if cab.features:
            shot = await self._item_shot(chat_id, user_id, "", draw=False)
            reuse_key = photo_state_key(cab, outside, shot)
        if not selfie and not focus and not happening and not expect and reuse_key in cab.photos:
            image = await self._store.image_by_id(cab.photos[reuse_key])
            # Тот же кадр в тот же чат второй раз — дубль: просят снова —
            # снимаем заново (живая находка 2026-10-02).
            if image is not None and image["telegram_file_id"] and image["chat_id"] != chat_id:
                sent = await self._notifier.send_photo_ex(
                    chat_id,
                    image["telegram_file_id"],
                    caption=caption,
                    message_thread_id=message_thread_id,
                    reply_to_message_id=trigger_message_id,
                )
                if sent is not None:
                    seen = _image_params(image).get("seen")
                    where = await self._where_ru(cab, user_id, in_scene=in_scene)
                    result = cabinet_mod.TOOL_PHOTO_SENT.format(where=where, now=outside.ru())
                    if isinstance(seen, str) and seen:
                        await self._save_last_photo(chat_id, int(image["id"]), seen, [], [])
                        result += cabinet_mod.TOOL_PHOTO_SENT_SEEN.format(description=seen)
                    return result
        if await self._photo_limit_reached(chat_id):
            return cabinet_mod.TOOL_PHOTO_LIMIT
        started = await self._start_photo(
            chat_id,
            user_id,
            focus=focus,
            caption=caption,
            happening=happening,
            mood=mood,
            outside=outside,
            message_thread_id=message_thread_id,
            trigger_message_id=trigger_message_id,
            expect=expect,
            dialogue_id=dialogue_id,
            # Где Альфред (и какой передатчик на столе) — к рассказу о снимке.
            describe=await self._where_ru(cab, user_id, in_scene=in_scene, tools=False),
            selfie=selfie,
        )
        if not started:
            return cabinet_mod.TOOL_PHOTO_UNAVAILABLE
        return cabinet_mod.TOOL_PHOTO_STARTED

    async def _join_photo(
        self,
        chat_id: int,
        user_id: int,
        trigger_message_id: int | None,
        dialogue_id: int | None,
    ) -> str:
        """Камера занята снимком сцены (его начал Ведущий в фоне) — ход
        присоединяется к нему: ждёт со статусами и говорит реплику подписью
        (живая находка 2026-10-02: Альфред отвечал до снимка и пять раз
        подряд звал take_photo). Снимок, у которого реплика уже есть, — занят."""
        task = self._photo_jobs.get(chat_id)
        if (
            task is None
            or task.done()
            or chat_id in self._photo_described
            or chat_id in self._photo_join
        ):
            return cabinet_mod.TOOL_PHOTO_BUSY
        cab = await cabinet_mod.load(self._store, user_id)
        run = await self._state.load_run(chat_id, radio.SCENARIO_ID)
        in_scene = run is not None and run.status == STATUS_ACTIVE
        self._photo_join[chat_id] = {
            "describe": await self._where_ru(cab, user_id, in_scene=in_scene, tools=False),
            "dialogue_id": dialogue_id,
            "trigger_message_id": trigger_message_id,
        }
        return cabinet_mod.TOOL_PHOTO_STARTED

    async def _wants_stored_item(self, user_id: int, text: str) -> bool:
        """Просят снять старое радио, а оно убрано в чулан — в кабинете его
        нет, показать можно только карточкой (radio.TOOL_PHOTO_STORED_ITEM)."""
        kind = items_mod.RADIO
        if kind.stored_re is None or not kind.focus_re.search(text):
            return False
        if not kind.stored_re.search(text) or not await self.speech_clear(user_id):
            return False
        return bool(await self._store.items_of(user_id, kind.type))

    async def _where_ru(
        self, cab: Cabinet, user_id: int, *, in_scene: bool, tools: bool = True
    ) -> str:
        """Кабинет словами для Альфреда, после сцены — и какой передатчик
        стоит на столе (radio.RADIO_STATE_*). ``tools=False`` — для вызова
        без тулов (подпись к снимку): без подсказок «позови тул»."""
        where = cab.describe_ru()
        if in_scene or not await self._state.is_completed(radio.SCENARIO_ID, user_id):
            return where
        if await self.speech_clear(user_id):
            hint = radio.RADIO_STATE_NEW_TOOL_HINT if tools else ""
            return f"{where} {radio.RADIO_STATE_NEW}{hint}"
        owned = await self._store.items_of(user_id, items_mod.RADIO_TYPE)
        traits = items_mod.RADIO.traits_ru(owned[0]["traits"] or []) if owned else []
        return f"{where} " + radio.RADIO_STATE_OLD.format(
            traits=radio.RADIO_STATE_TRAITS.format(traits="; ".join(traits)) if traits else ""
        )

    async def _photo_limit_reached(self, chat_id: int) -> bool:
        if not PHOTO_DAILY_LIMIT:
            return False
        since = self._now() - timedelta(days=1)
        taken = await self._store.count_images_since(chat_id, since, PHOTO_PURPOSE)
        return taken >= PHOTO_DAILY_LIMIT

    async def _start_photo(
        self,
        chat_id: int,
        user_id: int,
        *,
        focus: str,
        caption: str,
        happening: str | None,
        mood: str | None = None,
        alfred: str | None = None,
        outside: Outside,
        message_thread_id: int | None,
        trigger_message_id: int | None,
        expect: list[str] | None = None,
        dialogue_id: int | None = None,
        describe: str | None = None,
        selfie: bool = False,
    ) -> bool:
        """Снимок в фоне. False — не начат: уже идёт другой или нет связи."""
        if chat_id in self._photo_busy or not hasattr(self._notifier, "send_photo_ex"):
            return False
        if self._get_node_link() is None:
            return False
        self._photo_busy.add(chat_id)
        task = asyncio.create_task(
            self._photo_job(
                chat_id,
                user_id,
                focus=focus,
                caption=caption,
                happening=happening,
                mood=mood,
                alfred=alfred,
                outside=outside,
                message_thread_id=message_thread_id,
                trigger_message_id=trigger_message_id,
                expect=expect,
                dialogue_id=dialogue_id,
                describe=describe,
                selfie=selfie,
            )
        )
        self._photo_tasks.add(task)
        task.add_done_callback(self._photo_tasks.discard)
        # Любой снимок — задача на чат: к фоновому (сцены) может
        # присоединиться ход с take_photo (_join_photo).
        self._photo_jobs[chat_id] = task
        task.add_done_callback(lambda _t: self._photo_jobs.pop(chat_id, None))
        if describe is not None:
            self._photo_described.add(chat_id)
        return True

    async def wait_photo(
        self, chat_id: int, on_status: Callable[[str], Awaitable[None]] | None = None
    ) -> None:
        """Ход /ai, закончившийся снимком (take_photo), ждёт его здесь —
        и показывает статусы съёмки (``on_status`` — черновик хода). Отмена
        хода снимок не отменяет: он дойдёт сам."""
        task = self._photo_jobs.get(chat_id)
        if task is None or task.done():
            return
        if on_status is not None:
            self._photo_status[chat_id] = on_status
        cfg = self._settings.llm
        try:
            await asyncio.wait(
                {task}, timeout=cfg.imagegen_request_timeout_s + cfg.request_timeout_s + 60
            )
        finally:
            if self._photo_status.get(chat_id) is on_status:
                self._photo_status.pop(chat_id, None)

    async def _show_photo_status(self, chat_id: int, text: str) -> None:
        on_status = self._photo_status.get(chat_id)
        if on_status is None:
            return
        try:
            await on_status(text)
        except Exception:  # noqa: BLE001 — статус вспомогательный
            log.debug("interactives: статус снимка не показан (chat=%s)", chat_id, exc_info=True)

    async def _follow_photo_phases(
        self, node_link: ServiceLink, dst: Address, chat_id: int, request_id: str
    ) -> None:
        """Фазы generate_image (llm/service.py::IMAGE_PHASE_*) — статусами
        хода: промптер — «наводит фотоаппарат», рисование — «проявляет»."""
        shown = None
        while True:
            await asyncio.sleep(PHOTO_PHASE_POLL_S)
            try:
                state = await node_link.command(
                    ACTION_CHAT_PROGRESS, {"request_id": request_id}, dst=dst, timeout=10
                )
            except (ServiceUnavailableError, ProtoError, TimeoutError, OSError):
                continue
            text = PHOTO_PHASE_STATUS.get(str(state.get("partial") or ""))
            if text is not None and text != shown:
                shown = text
                await self._show_photo_status(chat_id, text)
            if state.get("done"):
                return

    async def _photo_job(
        self,
        chat_id: int,
        user_id: int,
        *,
        focus: str,
        caption: str,
        happening: str | None,
        mood: str | None = None,
        alfred: str | None = None,
        outside: Outside,
        message_thread_id: int | None,
        trigger_message_id: int | None,
        expect: list[str] | None = None,
        dialogue_id: int | None = None,
        describe: str | None = None,
        selfie: bool = False,
    ) -> None:
        delivered = False
        if describe is not None:
            await self._mark_photo_pending(chat_id, message_thread_id, trigger_message_id)
        try:
            delivered = await self._photo(
                chat_id,
                user_id,
                focus=focus,
                caption=caption,
                happening=happening,
                mood=mood,
                alfred=alfred,
                outside=outside,
                message_thread_id=message_thread_id,
                trigger_message_id=trigger_message_id,
                expect=expect,
                dialogue_id=dialogue_id,
                describe=describe,
                selfie=selfie,
            )
        except Exception:
            log.exception("interactives: снимок кабинета не удался (chat=%s)", chat_id)
        finally:
            self._photo_busy.discard(chat_id)
            self._photo_described.discard(chat_id)
            join = self._photo_join.pop(chat_id, None)
            # Снимок обещан ходу (свой take_photo или присоединившийся к кадру
            # сцены), но не дошёл — сказать, а не молчать.
            if not delivered and (describe is not None or join is not None):
                reply_to = (join or {}).get("trigger_message_id") or trigger_message_id
                await self._photo_lost(chat_id, message_thread_id, reply_to)
            await self._clear_photo_pending(chat_id)

    # --- портрет Альфреда к приветствию (2026-10-05) ---

    def start_greeting_portrait(
        self,
        chat_id: int,
        user_id: int,
        *,
        message_thread_id: int | None = None,
        trigger_message_id: int | None = None,
    ) -> bool:
        """Альфред поздоровался — следом его портрет в кабинете гостя, с тем
        же временем суток и погодой, что у снимков кабинета. Рисуется в фоне
        (~30 с), приветствие его не ждёт. False — не начат (уже рисуется в
        этом чате или нет связи)."""
        if chat_id in self._portrait_busy or not hasattr(self._notifier, "send_photo_ex"):
            return False
        if self._get_node_link() is None:
            return False
        self._portrait_busy.add(chat_id)
        task = asyncio.create_task(
            self._greeting_portrait(chat_id, user_id, message_thread_id, trigger_message_id)
        )
        self._photo_tasks.add(task)
        task.add_done_callback(self._photo_tasks.discard)
        task.add_done_callback(lambda _t: self._portrait_busy.discard(chat_id))
        return True

    async def _greeting_portrait(
        self,
        chat_id: int,
        user_id: int,
        message_thread_id: int | None,
        trigger_message_id: int | None,
    ) -> None:
        node_link = self._get_node_link()
        if node_link is None:
            return
        dst = Address(node=LLM_NODE, service=LLM_SERVICE)
        cfg = self._settings.llm
        try:
            now = self._now()
            outside = await self._transylvania.outside(now)
            cab = await cabinet_mod.load(self._store, user_id)
            if not cab.features:
                new = await ask_features(
                    node_link,
                    dst,
                    cfg.request_timeout_s,
                    chat_id=chat_id,
                    place=cab.describe_ru(),
                    outside=outside.ru(),
                    count=cabinet_mod.FIRST_FEATURES,
                )
                if cab.add(list(new)):
                    await cabinet_mod.save(self._store, cab)
            description = portrait_description(cab, outside)
            result = await node_link.command(
                image_tools.ACTION_GENERATE_IMAGE,
                {
                    "description": description,
                    "mode": "free",
                    "chat_id": chat_id,
                    "negative_extra": cabinet_mod.PHOTO_NEGATIVE_EN,
                    "loras": [list(cabinet_mod.ALFRED_LORA)],
                    "light": outside.en(),
                },
                dst=dst,
                timeout=cfg.imagegen_request_timeout_s,
            )
            png = base64.b64decode(result["png_b64"])
            image_id = await self._store.add_image(
                chat_id=chat_id,
                author=None,
                prompt_ru=f"{cabinet_mod.PORTRAIT_CAPTION}: {outside.ru()}",
                prompt_en=str(result.get("prompt") or description),
                caption=cabinet_mod.PORTRAIT_CAPTION,
                width=int(result["width"]),
                height=int(result["height"]),
                colors=cfg.imagegen_colors,
                png=png,
                now=now,
                purpose=PORTRAIT_PURPOSE,
                params=json.dumps(
                    {
                        "location": cabinet_mod.LOCATION,
                        "user_id": user_id,
                        "state": photo_state_key(cab, outside, None),
                        "seed": result.get("seed"),
                    },
                    ensure_ascii=False,
                ),
            )
            sent = await self._notifier.send_photo_ex(
                chat_id,
                image_tools.upscale_png(png, cfg.imagegen_display_px),
                message_thread_id=message_thread_id,
                reply_to_message_id=trigger_message_id,
            )
        except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
            log.warning(
                "interactives: портрет к приветствию не нарисован (chat=%s): %s", chat_id, exc
            )
            return
        except Exception:
            log.exception("interactives: портрет к приветствию не удался (chat=%s)", chat_id)
            return
        if sent is None:
            log.warning("interactives: портрет #%s не ушёл в чат %s", image_id, chat_id)
            return
        await self._store.set_image_sent(image_id, sent[1], sent[0])

    async def _photo_lost(
        self, chat_id: int, message_thread_id: int | None, reply_to: int | None
    ) -> None:
        log.warning("interactives: обещанный снимок не дошёл (chat=%s)", chat_id)
        try:
            await self._notifier.send_direct(
                chat_id,
                self._choose(cabinet_mod.PHOTO_LOST_TEXTS),
                reply_to_message_id=reply_to,
                message_thread_id=message_thread_id,
            )
        except Exception:  # noqa: BLE001 — сообщение вспомогательное
            log.exception("interactives: не сказал о пропавшем снимке (chat=%s)", chat_id)

    async def _photo_pending(self) -> dict[str, Any]:
        raw = await self._store.get_state(cabinet_mod.PHOTO_PENDING_KEY)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    async def _mark_photo_pending(
        self, chat_id: int, message_thread_id: int | None, reply_to: int | None
    ) -> None:
        pending = await self._photo_pending()
        pending[str(chat_id)] = {"thread": message_thread_id, "reply_to": reply_to}
        await self._store.set_state(cabinet_mod.PHOTO_PENDING_KEY, json.dumps(pending))

    async def _clear_photo_pending(self, chat_id: int) -> None:
        pending = await self._photo_pending()
        if pending.pop(str(chat_id), None) is not None:
            await self._store.set_state(cabinet_mod.PHOTO_PENDING_KEY, json.dumps(pending))

    async def recover(self) -> None:
        """Старт бота: снимки, оборванные рестартом посреди съёмки, — сказать
        гостю, что не вышло (живая находка 2026-10-02: деплой во время снимка,
        и Альфред «снял», а снимка нет)."""
        pending = await self._photo_pending()
        if not pending:
            return
        await self._store.set_state(cabinet_mod.PHOTO_PENDING_KEY, "{}")
        for chat, where in pending.items():
            where = where if isinstance(where, dict) else {}
            await self._photo_lost(int(chat), where.get("thread"), where.get("reply_to"))

    async def _photo(
        self,
        chat_id: int,
        user_id: int,
        *,
        focus: str,
        caption: str,
        happening: str | None,
        mood: str | None = None,
        alfred: str | None = None,
        outside: Outside,
        message_thread_id: int | None,
        trigger_message_id: int | None,
        expect: list[str] | None = None,
        dialogue_id: int | None = None,
        describe: str | None = None,
        selfie: bool = False,
    ) -> bool:
        """Снимок нарисован и отправлен — True. ``selfie`` — в кадре сам
        Альфред (LoRA облика) в том же кабинете, свете и сцене."""
        node_link = self._get_node_link()
        if node_link is None:
            return False
        if GENERAL_VIEW_RE.match(focus):
            focus = ""  # Ведущий тоже просит «общий план кабинета»
        dst = Address(node=LLM_NODE, service=LLM_SERVICE)
        cfg = self._settings.llm
        cab = await cabinet_mod.load(self._store, user_id)
        if not cab.features:
            new = await ask_features(
                node_link,
                dst,
                cfg.request_timeout_s,
                chat_id=chat_id,
                place=cab.describe_ru(),
                outside=outside.ru(),
                count=cabinet_mod.FIRST_FEATURES,
            )
            if cab.add(list(new)):
                await cabinet_mod.save(self._store, cab)
        # Себя — без вставки предмета пикселями: место в кадре занимает Альфред.
        shot = None if selfie else await self._item_shot(chat_id, user_id, focus, draw=True)
        if selfie:
            description, context = selfie_description(cab, outside, focus, happening), ""
        else:
            description, context = photo_description(
                cab,
                outside,
                focus,
                happening,
                item=shot.kind if shot else None,
                item_place=shot.place if shot else None,
                alfred=alfred,
            )
        request: dict[str, Any] = {
            "description": description,
            "mode": "scene" if focus and not selfie else "free",
            "context": context,
            "chat_id": chat_id,
            # Этап 49.2.1: mycraft сохранит 512-оригинал и сверит его зрением.
            "keep_key": f"snap-{chat_id}-{uuid.uuid4().hex[:12]}",
            "expect": list(expect or []),
            "negative_extra": cabinet_mod.PHOTO_NEGATIVE_EN,
        }
        # Пересъёмка после промаха: то, чего не было на прошлом снимке,
        # промптер ставит главным (llm/image_prompt.py, emphasize).
        emphasize = retake_emphasis(await self._last_photo(chat_id), list(expect or []))
        if shot is not None:
            # Предмет вставят пикселями — словами его промптер нарисует
            # вторым, своим («пустой корпус передатчика», 2026-10-01).
            emphasize = [e for e in emphasize if not shot.kind.focus_re.search(e)]
        if emphasize:
            request["emphasize"] = emphasize
            log.info("interactives: пересъёмка, упор на %s (chat=%s)", emphasize, chat_id)
        loras: list[list[Any]] = [list(cabinet_mod.ALFRED_LORA)] if selfie else []
        preset = MOOD_PRESETS.get(mood or "")
        if preset is not None:
            model, lora, weight = preset
            request["model"] = model
            loras.append([lora, weight])
        if alfred in cabinet_mod.SCENE_ALFRED_EN and not selfie:
            # Альфред в кадре события — его LoRA облика рядом с LoRA настроения.
            loras.append(list(cabinet_mod.ALFRED_LORA))
        if loras:
            request["loras"] = loras
        # Свет вшивает служба вторым тегом: в хвосте промпта «dark window at
        # night» не держал ночь — ни у LoRA Альфреда, ни у пустого кабинета.
        window = bool(WINDOW_VIEW_RE.search(focus))
        closeup = (
            not selfie
            and not window
            and bool(focus or (shot is not None and shot.place == "closeup"))
        )
        request["light"] = outside.en(closeup=closeup)
        if outside.light == PHASE_NIGHT and not closeup:
            # Ночью свет, уже стоящий в промпте, переносится на второе место
            # (стенд 2026-10-10: ночь 86% против 75%); крупному плану вредит.
            request["light_move"] = True
        if shot is not None:
            # Этап 49.3: предмет — пикселями поверх готовой сцены.
            request["paste"] = {"key": shot.key, "place": shot.place, "hint": shot.kind.paste_hint}
        # Фазы — статусами хода, который ждёт снимок (свой или присоединился).
        request["request_id"] = uuid.uuid4().hex
        phases = asyncio.create_task(
            self._follow_photo_phases(node_link, dst, chat_id, request["request_id"])
        )
        try:
            result = await node_link.command(
                image_tools.ACTION_GENERATE_IMAGE,
                request,
                dst=dst,
                timeout=cfg.imagegen_request_timeout_s,
            )
        except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
            log.warning("interactives: снимок не нарисован (chat=%s): %s", chat_id, exc)
            if shot is not None and isinstance(exc, ProtoError):
                # Вырезки могло не оказаться на ноде (сменили диск, почистили
                # каталог) — следующий снимок нарисует портрет заново.
                await self._forget_item_cut(chat_id, user_id, shot)
            return False
        finally:
            phases.cancel()
        png = base64.b64decode(result["png_b64"])
        inspection = result.get("inspect") if isinstance(result.get("inspect"), dict) else None
        seen = str(inspection.get("description") or "") if inspection else ""
        missing = [m for m in (inspection or {}).get("missing") or [] if isinstance(m, str)]
        state = photo_state_key(cab, outside, shot)
        image_id = await self._store.add_image(
            chat_id=chat_id,
            author=None,
            prompt_ru=f"{caption}: {focus or cab.describe_ru()}",
            prompt_en=str(result.get("prompt") or description),
            caption=caption,
            width=int(result["width"]),
            height=int(result["height"]),
            colors=cfg.imagegen_colors,
            png=png,
            now=self._now(),
            purpose=PHOTO_PURPOSE,
            params=json.dumps(
                {
                    "location": cabinet_mod.LOCATION,
                    "user_id": user_id,
                    "state": state,
                    "focus": focus,
                    "selfie": selfie,
                    "mood": mood,
                    "alfred": alfred,
                    "seed": result.get("seed"),
                    "expect": list(expect or []),
                    "seen": seen,
                    "missing": missing,
                },
                ensure_ascii=False,
            ),
        )
        misses = 0
        if seen:
            misses = await self._save_last_photo(
                chat_id, image_id, seen, list(expect or []), missing
            )
        join = self._photo_join.pop(chat_id, None) if describe is None else None
        if join is not None:
            describe = join["describe"]
            dialogue_id = join["dialogue_id"]
            trigger_message_id = join["trigger_message_id"] or trigger_message_id
        line = ""
        if describe is not None:
            # Реплика — на то, что вышло на снимке, и сразу с ним.
            await self._show_photo_status(chat_id, cabinet_mod.PHOTO_STATUS_DEVELOPING)
            line = await self._photo_line(
                chat_id,
                user_id,
                photo_line_directive(
                    photo_subject(focus, selfie=selfie),
                    seen or focus or cab.describe_ru(),
                    describe,
                    missing if seen else [],
                    misses,
                ),
            )
        sent = await self._notifier.send_photo_ex(
            chat_id,
            image_tools.upscale_png(png, cfg.imagegen_display_px),
            caption=ALFRED_PHOTO_PREFIX + html.escape(line) if line else caption,
            message_thread_id=message_thread_id,
            reply_to_message_id=trigger_message_id,
        )
        if sent is None:
            log.warning("interactives: снимок #%s не ушёл в чат %s", image_id, chat_id)
            return False
        await self._store.set_image_sent(image_id, sent[1], sent[0])
        if line and dialogue_id is not None:
            # Подпись — ход Альфреда в треде: на неё можно ответить.
            await self._store.record_ai_turn(
                chat_id, sent[0], dialogue_id, "assistant", line, self._now()
            )
        if seen and missing:
            if describe is None:
                await self._react_to_miss(
                    chat_id,
                    seen,
                    missing,
                    misses,
                    photo_message_id=sent[0],
                    message_thread_id=message_thread_id,
                    trigger_message_id=trigger_message_id,
                    dialogue_id=dialogue_id,
                )
            # Промах — не общий вид кабинета: повторно его не показываем.
            return True
        if not focus and not happening and not selfie and not alfred:
            cab = await cabinet_mod.load(self._store, user_id)
            cab.remember_photo(photo_state_key(cab, outside, shot), image_id)
            await cabinet_mod.save(self._store, cab)
        return True

    # --- эмоциональное селфи к ответу (2026-10-05) ---

    async def _mood_selfie_gated(self, chat_id: int, reply: str) -> bool:
        """Дешёвые гейты без GPU: связь и отправка фото есть, реплика влезает
        в подпись, в чате не чаще раза в SELFIE_MOOD_GAP_H часов и
        SELFIE_MOOD_DAILY в сутки. True — можно спрашивать классификатор."""
        if not hasattr(self._notifier, "send_photo_ex") or self._get_node_link() is None:
            return False
        if chat_id in self._photo_busy or chat_id in self._photo_jobs:
            return False
        if not reply.strip() or len(reply) > cabinet_mod.PHOTO_LINE_MAX:
            return False
        if len(ALFRED_PHOTO_PREFIX + html.escape(reply)) > cabinet_mod.PHOTO_CAPTION_MAX:
            return False
        now = self._now()
        purpose = cabinet_mod.SELFIE_MOOD_PURPOSE
        gap = timedelta(hours=cabinet_mod.SELFIE_MOOD_GAP_H)
        if await self._store.count_images_since(chat_id, now - gap, purpose):
            return False
        day = await self._store.count_images_since(chat_id, now - timedelta(days=1), purpose)
        return day < cabinet_mod.SELFIE_MOOD_DAILY

    async def _classify_mood(
        self, chat_id: int, user_text: str, reply: str
    ) -> tuple[str, int, str] | None:
        """(эмоция, интенсивность 0..3, action) или None — сбой/мусор."""
        node_link = self._get_node_link()
        if node_link is None:
            return None
        args: dict[str, Any] = {
            "messages": [
                {
                    "role": "user",
                    "content": cabinet_mod.SELFIE_MOOD_INPUT.format(
                        user=user_text.strip()[:600], reply=reply.strip()[:900]
                    ),
                }
            ],
            "role": ROLE_DIRECTOR,
            "system": cabinet_mod.SELFIE_MOOD_SYSTEM,
            "reason": "off",
            "chat_id": chat_id,
        }
        try:
            result = await node_link.command(
                ACTION_CHAT,
                args,
                dst=Address(node=LLM_NODE, service=LLM_SERVICE),
                timeout=self._settings.llm.request_timeout_s,
            )
        except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
            log.warning("interactives: оценка эмоции не вышла (chat=%s): %s", chat_id, exc)
            return None
        data = _json_object(result.get("response", "") if isinstance(result, dict) else "")
        if data is None:
            return None
        emotion = data.get("emotion")
        intensity = data.get("intensity")
        if emotion not in cabinet_mod.SELFIE_EMOTIONS or isinstance(intensity, bool):
            return None
        try:
            level = int(intensity)
        except (TypeError, ValueError):
            return None
        action = data.get("action") if isinstance(data.get("action"), str) else ""
        action = " ".join(action.split())[: cabinet_mod.SELFIE_ACTION_MAX].rstrip(" .")
        return str(emotion), level, action

    async def mood_selfie(
        self,
        chat_id: int,
        user_id: int | None,
        user_text: str,
        reply: str,
        *,
        message_thread_id: int | None = None,
        trigger_message_id: int | None = None,
        on_status: Callable[[str], Awaitable[None]] | None = None,
    ) -> int | None:
        """Спонтанное селфи к ответу /ai: реплика Альфреда (``reply``) уходит
        подписью к его фото одним сообщением. Возвращает message_id фото или
        None — селфи нет (гейты, «none», не выпал бросок, сбой) и ответ надо
        слать обычным текстом. Никогда не бросает: ответ не должен теряться."""
        if user_id is None:
            return None
        try:
            if not await self._mood_selfie_gated(chat_id, reply):
                return None
            mood = await self._classify_mood(chat_id, user_text, reply)
            if mood is None:
                return None
            emotion, intensity, action = mood
            chance = cabinet_mod.SELFIE_MOOD_CHANCE.get(min(intensity, 3), 0.0)
            if intensity <= 1 or self._rng() >= chance:
                return None
            self._photo_busy.add(chat_id)
            try:
                return await self._mood_selfie_shot(
                    chat_id,
                    user_id,
                    reply,
                    emotion,
                    intensity,
                    action,
                    message_thread_id=message_thread_id,
                    trigger_message_id=trigger_message_id,
                    on_status=on_status,
                )
            finally:
                self._photo_busy.discard(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("interactives: селфи к ответу не удалось (chat=%s)", chat_id)
            return None

    async def _mood_selfie_shot(
        self,
        chat_id: int,
        user_id: int,
        reply: str,
        emotion: str,
        intensity: int,
        action: str,
        *,
        message_thread_id: int | None,
        trigger_message_id: int | None,
        on_status: Callable[[str], Awaitable[None]] | None,
    ) -> int | None:
        node_link = self._get_node_link()
        if node_link is None:
            return None
        dst = Address(node=LLM_NODE, service=LLM_SERVICE)
        cfg = self._settings.llm
        now = self._now()
        outside = await self._transylvania.outside(now)
        if on_status is not None:
            self._photo_status[chat_id] = on_status
        try:
            await self._show_photo_status(chat_id, cabinet_mod.PHOTO_STATUS_AIMING)
            cab = await cabinet_mod.load(self._store, user_id)
            if not cab.features:
                new = await ask_features(
                    node_link,
                    dst,
                    cfg.request_timeout_s,
                    chat_id=chat_id,
                    place=cab.describe_ru(),
                    outside=outside.ru(),
                    count=cabinet_mod.FIRST_FEATURES,
                )
                if cab.add(list(new)):
                    await cabinet_mod.save(self._store, cab)
            run = await self._state.load_run(chat_id, radio.SCENARIO_ID)
            in_scene = run is not None and run.status == STATUS_ACTIVE
            happening = run.last_effect if run is not None and in_scene else None
            mood = run.mood if run is not None and in_scene else None
            face = cabinet_mod.SELFIE_EMOTION_FACE_EN[emotion]
            focus = "; ".join(p for p in (face, action) if p)
            description = selfie_description(cab, outside, focus, happening)
            request: dict[str, Any] = {
                "description": description,
                "mode": "free",
                "context": "",
                "chat_id": chat_id,
                "negative_extra": cabinet_mod.PHOTO_NEGATIVE_EN,
                # Мимика — главным: без этого промптер выкинет её ради обстановки.
                "emphasize": [e for e in (face or action,) if e],
            }
            loras: list[list[Any]] = [list(cabinet_mod.ALFRED_LORA)]
            preset = MOOD_PRESETS.get(mood or "")
            if preset is not None:
                model, lora, weight = preset
                request["model"] = model
                loras.append([lora, weight])
            request["loras"] = loras
            request["light"] = outside.en()
            request["request_id"] = uuid.uuid4().hex
            phases = asyncio.create_task(
                self._follow_photo_phases(node_link, dst, chat_id, request["request_id"])
            )
            try:
                result = await node_link.command(
                    image_tools.ACTION_GENERATE_IMAGE,
                    request,
                    dst=dst,
                    timeout=cfg.imagegen_request_timeout_s,
                )
                png = base64.b64decode(result["png_b64"])
            except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
                log.warning("interactives: селфи к ответу не нарисовано (%s): %s", chat_id, exc)
                return None
            finally:
                phases.cancel()
            image_id = await self._store.add_image(
                chat_id=chat_id,
                author=None,
                prompt_ru=f"{cabinet_mod.SELFIE_MOOD_CAPTION}: {emotion}",
                prompt_en=str(result.get("prompt") or description),
                caption=cabinet_mod.SELFIE_MOOD_CAPTION,
                width=int(result["width"]),
                height=int(result["height"]),
                colors=cfg.imagegen_colors,
                png=png,
                now=now,
                purpose=cabinet_mod.SELFIE_MOOD_PURPOSE,
                params=json.dumps(
                    {
                        "location": cabinet_mod.LOCATION,
                        "user_id": user_id,
                        "selfie": True,
                        "emotion": emotion,
                        "intensity": intensity,
                        "action": action,
                        "mood": mood,
                        "seed": result.get("seed"),
                    },
                    ensure_ascii=False,
                ),
            )
            sent = await self._notifier.send_photo_ex(
                chat_id,
                image_tools.upscale_png(png, cfg.imagegen_display_px),
                caption=ALFRED_PHOTO_PREFIX + html.escape(reply),
                message_thread_id=message_thread_id,
                reply_to_message_id=trigger_message_id,
            )
            if sent is None:
                log.warning("interactives: селфи #%s не ушло в чат %s", image_id, chat_id)
                return None
            await self._store.set_image_sent(image_id, sent[1], sent[0])
            return int(sent[0])
        finally:
            if self._photo_status.get(chat_id) is on_status:
                self._photo_status.pop(chat_id, None)

    async def _photo_line(self, chat_id: int, user_id: int, directive: str) -> str:
        """Короткая реплика Альфреда к снимку — персонажем службы llm, с той
        же картавостью, что в ходе. Пусто — не вышло (подпись без неё)."""
        node_link = self._get_node_link()
        if node_link is None:
            return ""
        args: dict[str, Any] = {
            "messages": [wrap_system_directive(directive)],
            "tools": [],
            "reason": "off",
            "chat_id": chat_id,
            "user_id": user_id,
            "speech_clear": await self.speech_clear(user_id),
        }
        try:
            result = await node_link.command(
                ACTION_CHAT,
                args,
                dst=Address(node=LLM_NODE, service=LLM_SERVICE),
                timeout=self._settings.llm.request_timeout_s,
            )
        except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
            log.warning("interactives: подпись к снимку не вышла (chat=%s): %s", chat_id, exc)
            return ""
        line = str(result.get("response") or "").strip() if isinstance(result, dict) else ""
        return line[: cabinet_mod.PHOTO_LINE_MAX]

    async def _end_scene_in_cabinet(self, user_id: int) -> None:
        cab = await cabinet_mod.load(self._store, user_id)
        if cab.end_scene():
            await cabinet_mod.save(self._store, cab)

    # --- сверка снимка (Этап 49.2.1) ---

    async def _save_last_photo(
        self,
        chat_id: int,
        image_id: int,
        seen: str,
        expect: list[str],
        missing: list[str],
    ) -> int:
        """Запомнить, что вышло на последнем снимке чата (только для Альфреда).
        Возвращает число промахов подряд по тому же ``expect``."""
        key = cabinet_mod.LAST_PHOTO_KEY.format(chat_id=chat_id)
        prev = await self._last_photo(chat_id, fresh_only=False)
        misses = 0
        if missing:
            same = prev is not None and prev.get("expect") == expect
            misses = (int(prev.get("misses") or 0) if same else 0) + 1
        await self._store.set_state(
            key,
            json.dumps(
                {
                    "image_id": image_id,
                    "at": iso(self._now()),
                    "seen": seen,
                    "expect": expect,
                    "missing": missing,
                    "misses": misses,
                },
                ensure_ascii=False,
            ),
        )
        return misses

    async def _last_photo(self, chat_id: int, *, fresh_only: bool = True) -> dict[str, Any] | None:
        raw = await self._store.get_state(cabinet_mod.LAST_PHOTO_KEY.format(chat_id=chat_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        if fresh_only:
            at = parse_iso(data.get("at"))
            if at is None or at + timedelta(hours=cabinet_mod.LAST_PHOTO_TTL_H) <= self._now():
                return None
        return data

    async def photo_note(self, chat_id: int) -> str | None:
        """Заметка к ходу Альфреда: что на его последнем снимке."""
        data = await self._last_photo(chat_id)
        if data is None or not data.get("seen"):
            return None
        at = parse_iso(data.get("at"))
        when = at.astimezone(TRANSYLVANIA_TZ).strftime("%H:%M") if at else "недавно"
        note = cabinet_mod.PHOTO_SEEN_NOTE.format(at=when, description=data["seen"])
        missing = data.get("missing") or []
        if missing:
            note += cabinet_mod.PHOTO_SEEN_MISSING_NOTE.format(missing=", ".join(missing))
        return note

    async def _react_to_miss(
        self,
        chat_id: int,
        seen: str,
        missing: list[str],
        misses: int,
        *,
        photo_message_id: int | None,
        message_thread_id: int | None,
        trigger_message_id: int | None,
        dialogue_id: int | None,
    ) -> None:
        """На снимке нет обещанного — Альфред удивляется и предлагает
        переснять (ответом на снимок, в тот же диалог)."""
        template = cabinet_mod.PHOTO_MISS_DIRECTIVE
        if misses > 1:
            template = cabinet_mod.PHOTO_MISS_AGAIN_DIRECTIVE
        directive = template.format(missing=", ".join(missing), description=seen)
        where = await self._where(chat_id, trigger_message_id, message_thread_id)
        if dialogue_id is not None:
            where["dialogue_id"] = dialogue_id
        if photo_message_id is not None:
            where["trigger_message_id"] = photo_message_id
        log.info("interactives: на снимке нет %s (chat=%s, промах %d)", missing, chat_id, misses)
        await self._speak(chat_id, directive, where)

    # --- кнопки ---

    async def handle_click(
        self,
        chat_id: int,
        user_id: int,
        scenario_id: str,
        button: str,
        *,
        message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> tuple[str, str | None, bool]:
        result = await self._handle_click(
            chat_id,
            user_id,
            scenario_id,
            button,
            message_id=message_id,
            message_thread_id=message_thread_id,
        )
        if result[1] is not None or result[2]:
            await self._forget_buttons(chat_id, message_id)  # форма закрыта нажатием
        return result

    async def _handle_click(
        self,
        chat_id: int,
        user_id: int,
        scenario_id: str,
        button: str,
        *,
        message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> tuple[str, str | None, bool]:
        """Нажатие кнопки. Возвращает (текст для callback.answer, новый текст
        формы или None — не править, убрать ли клавиатуру).

        ``message_id``/``message_thread_id`` — сообщение с формой: реплика
        Альфреда после кнопки уходит в тот же тред и тот же диалог (живой баг
        2026-09-28: без них ответ улетал в общий топик лички)."""
        scenario = REGISTRY[scenario_id]
        run = await self._state.load_run(chat_id, scenario_id)
        where = await self._where(chat_id, message_id, message_thread_id)
        if button in (BTN_PLAY, BTN_LATER, BTN_NEVER):
            return await self._click_offer(run, user_id, button, where)
        if button == BTN_EXIT:
            if run is None or run.user_id != user_id:
                return "Эта кнопка не для вас.", None, False
            if run.status != STATUS_ACTIVE:
                return "Сценка уже закончилась.", None, True
            run.status = STATUS_PAUSED
            await self._state.save_run(run)
            return EXIT_ALERT, None, True
        if button in (BTN_SWAP, BTN_KEEP):
            if run is None or run.user_id != user_id:
                return "Эта форма не для вас.", None, False
            if run.status != STATUS_ACTIVE or not run.finale:
                return "Уже решено.", None, True
            if button == BTN_KEEP:
                return "Хорошо.", radio.SWAP_KEPT_TEXT, True
            await self._set_clear(chat_id, user_id, True)
            await self._state.mark_completed(scenario.id, user_id)
            run.status = STATUS_DONE
            await self._state.save_run(run)
            await self._end_scene_in_cabinet(user_id)
            await self._speak(chat_id, radio.AFTER_SWAP_DIRECTIVE, where)
            if scenario.item_kind in items_mod.KINDS:
                self._spawn(
                    self._grant_item(chat_id, user_id, run, where.get("message_thread_id")),
                    "выдача предмета",
                )
            return "Готово.", radio.SWAP_ACCEPTED_TEXT, True
        if button in (BTN_RETURN_OLD, BTN_INSTALL_NEW, BTN_TOGGLE_KEEP):
            if not await self._state.is_completed(scenario.id, user_id):
                return "Эта форма не для вас.", None, False
            if button == BTN_TOGGLE_KEEP:
                return "Оставили как есть.", None, True
            target = button == BTN_INSTALL_NEW
            if await self.speech_clear(user_id) == target:
                return "Уже так.", None, True
            await self._set_clear(chat_id, user_id, target)
            await self._speak(
                chat_id,
                radio.AFTER_SWAP_DIRECTIVE if target else radio.AFTER_RETURN_DIRECTIVE,
                where,
            )
            done_text = radio.REINSTALL_ACCEPTED_TEXT if target else radio.RETURN_ACCEPTED_TEXT
            return "Готово.", done_text, True
        return "Неизвестная кнопка.", None, False

    async def _where(
        self, chat_id: int, message_id: int | None, message_thread_id: int | None
    ) -> dict[str, Any]:
        """meta задачи tasks: куда доставить реплику Альфреда после кнопки
        (bot/node_events.py::_handle_task_result, как у tool_remind)."""
        dialogue_id = None
        if message_id is not None:
            turn = await self._store.ai_turn(chat_id, message_id)
            if turn is not None:
                dialogue_id = turn.get("dialogue_id")
        return {
            "dialogue_id": dialogue_id,
            "trigger_message_id": message_id,
            "message_thread_id": message_thread_id,
        }

    async def _click_offer(
        self, run: Run | None, user_id: int, button: str, where: dict[str, Any]
    ) -> tuple[str, str | None, bool]:
        if run is None or run.user_id != user_id:
            return "Эта форма не для вас.", None, False
        if run.status != STATUS_OFFERED:
            return "Уже решено.", None, True
        offer_text = REGISTRY[run.scenario].offer_text
        run = await self._expire_offer(run)
        if run is None or run.status != STATUS_OFFERED:
            return "Вопрос уже неактуален.", offer_text + OFFER_EXPIRED_SUFFIX, True
        if button == BTN_PLAY:
            run.status = STATUS_ACTIVE
            await self._state.save_run(run)
            transcript = "\n".join(run.transcript[-NOTE_TRANSCRIPT_LINES:]) or "—"
            await self._speak(
                run.chat_id, radio.AFTER_AGREE_DIRECTIVE.format(transcript=transcript), where
            )
            return "Хорошо.", offer_text + OFFER_YES_SUFFIX, True
        # «Нет» — только пауза (DECLINE_COOLDOWN), не запрет насовсем: гость,
        # раз отказавшись, сам не знал про /interactives on (решение
        # пользователя 2026-10-02). Запрет — только командой /interactives off.
        # BTN_LATER — у форм, разосланных до v0.115.2 («Не сейчас»).
        run.status = STATUS_DECLINED
        run.declined_until = iso(self._now() + DECLINE_COOLDOWN)
        await self._state.save_run(run)
        return "Хорошо.", offer_text + OFFER_NO_SUFFIX, True

    async def _set_clear(self, chat_id: int, user_id: int, clear: bool) -> None:
        """Источник правды — БД бота, пишется сразу. Служба llm — зеркало:
        пуш best-effort, а если mycraft спит, зеркало починит первый же
        живой запрос гостя (он несёт speech_clear, llm/service.py::
        _speech_target)."""
        await self._state.set_effect(radio.EFFECT_SPEECH_CLEAR, user_id, "1" if clear else "0")
        node_link = self._get_node_link()
        if node_link is None:
            return
        try:
            await node_link.command(
                ACTION_SET_SPEECH_CLEAR,
                {"user_id": user_id, "clear": clear},
                dst=Address(node=LLM_NODE, service=LLM_SERVICE),
                timeout=SET_SPEECH_TIMEOUT_S,
            )
        except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
            log.info("interactives: зеркало речи не обновлено сразу (chat=%s): %s", chat_id, exc)

    async def _speak(self, chat_id: int, directive: str, where: dict[str, Any]) -> None:
        """Реплика Альфреда после кнопки — задачей службы tasks (как речь
        форм Этапа 45). Best-effort: не вышло — форма уже сказала главное."""
        node_link = self._get_node_link()
        if node_link is None:
            return
        # Импорт здесь: bot/tools.py сам импортирует этот пакет (тул swap_radio).
        from sa_home_bot.bot import tools as ai_tools

        try:
            await ai_tools.schedule_agent_dialogue(
                node_link,
                chat_id,
                [wrap_system_directive(directive)],
                reminder_reason(self._settings.llm),
                self._settings.llm.request_timeout_s,
                meta_extra=where,
            )
        except (ServiceUnavailableError, ProtoError) as exc:
            log.info("interactives: реплика после смены не поставлена (chat=%s): %s", chat_id, exc)

    # --- сюжетные предметы (Этап 49.3) ---

    def _spawn(self, coro: Awaitable[Any], what: str) -> None:
        async def run() -> None:
            try:
                await coro
            except Exception:
                log.exception("interactives: %s не удалась", what)

        task = asyncio.create_task(run())
        self._photo_tasks.add(task)
        task.add_done_callback(self._photo_tasks.discard)

    async def _portrait(
        self, chat_id: int, key: str, kind: items_mod.ItemKind, traits: list[str], seed: int
    ) -> dict[str, Any] | None:
        """Портрет предмета на ноде llm (item_portrait): рисунок + вырезка
        для вставки. None — не вышло (кадры идут без предмета)."""
        node_link = self._get_node_link()
        if node_link is None:
            return None
        try:
            result = await node_link.command(
                ACTION_ITEM_PORTRAIT,
                {
                    "prompt": kind.portrait_prompt(traits),
                    "key": key,
                    "seed": seed,
                    "checks": list(kind.checks),
                    "chat_id": chat_id,
                    **({"restyle": restyle} if (restyle := kind.restyle(traits)) else {}),
                },
                dst=Address(node=LLM_NODE, service=LLM_SERVICE),
                # До трёх попыток turbo плюс очередь GPU за чужими картинками:
                # 2026-10-02 портрет пришёл через 193 с, бот бросил его на 180.
                timeout=self._settings.llm.imagegen_request_timeout_s * 2,
            )
        except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
            log.warning("interactives: портрет %s не нарисован (chat=%s): %s", key, chat_id, exc)
            return None
        if not isinstance(result, dict) or not result.get("png_b64"):
            return None
        log.info(
            "interactives: портрет %s, зерно %s, черты %s, нет %s",
            key, result.get("seed"), traits, result.get("missing"),
        )
        return result

    async def _save_portrait(
        self,
        chat_id: int,
        kind: items_mod.ItemKind,
        key: str,
        traits: list[str],
        result: dict[str, Any],
    ) -> int:
        png = base64.b64decode(result["png_b64"])
        return await self._store.add_image(
            chat_id=chat_id,
            author=None,
            prompt_ru=" ".join([kind.name, *kind.traits_ru(traits)]),
            prompt_en=str(result.get("full_prompt") or kind.portrait_prompt(traits)),
            caption=kind.name,
            width=int(result.get("width") or 0),
            height=int(result.get("height") or 0),
            colors=self._settings.llm.imagegen_colors,
            png=png,
            now=self._now(),
            purpose=ITEM_PURPOSE,
            params=json.dumps(
                {
                    "cut_key": key,
                    "seed": result.get("seed"),
                    "traits": traits,
                    "seen": result.get("seen"),
                    "missing": result.get("missing"),
                },
                ensure_ascii=False,
            ),
        )

    async def _owned_item(self, user_id: int, kind: items_mod.ItemKind) -> dict[str, Any] | None:
        """Предмет вида у гостя. Прошедшим сцену до Этапа 49.3 он выдаётся
        здесь же (ленивая выдача): обычное радио без черт, портрет — когда
        понадобится."""
        owned = await self._store.items_of(user_id, kind.type)
        if owned:
            return owned[0]
        if kind.type != items_mod.RADIO_TYPE or not await self._state.is_completed(
            radio.SCENARIO_ID, user_id
        ):
            return None
        item_id = await self._store.add_item(
            type=kind.type,
            owner_user_id=user_id,
            traits=[],
            image_id=None,
            origin="radio_scene_before_items",
            now=self._now(),
        )
        log.info("interactives: гостю %s выдано радио задним числом (#%s)", user_id, item_id)
        return await self._store.item_by_id(item_id)

    async def _item_cut(
        self, chat_id: int, item: dict[str, Any], kind: items_mod.ItemKind, *, draw: bool
    ) -> tuple[str, int] | None:
        """(ключ вырезки, id портрета) предмета; нет портрета — нарисовать
        (``draw``), иначе None."""
        if item.get("image_id"):
            image = await self._store.image_by_id(int(item["image_id"]))
            key = _image_params(image).get("cut_key") if image else None
            if isinstance(key, str) and key:
                return key, int(item["image_id"])
        if not draw:
            return None
        key = items_mod.new_cut_key(kind, int(item["owner_user_id"]))
        result = await self._portrait(chat_id, key, kind, item["traits"], items_mod.new_seed())
        if result is None:
            return None
        image_id = await self._save_portrait(chat_id, kind, key, item["traits"], result)
        await self._store.set_item_image(int(item["id"]), image_id, item["traits"])
        item["image_id"] = image_id
        return key, image_id

    async def _item_shot(
        self, chat_id: int, user_id: int, focus: str, *, draw: bool
    ) -> _ItemShot | None:
        """Какой предмет вставить в кадр кабинета. Общий вид — радио на
        столе, кадр «про передатчик» — крупно; прочие крупные планы — без
        предмета. Источник облика: идущая сцена (черты копятся), иначе
        предмет гостя — старое радио, если стоит, или новое, если старое
        убрано. ``draw=False`` — только узнать версию облика, ничего не
        рисуя: портрета ещё нет — метка ``ITEM_PENDING_TAG`` (прежний
        снимок без предмета повторно не покажется)."""
        kind = items_mod.RADIO
        if focus and not kind.focus_re.search(focus):
            return None
        place = "closeup" if focus else "desk"
        run = await self._state.load_run(chat_id, radio.SCENARIO_ID)
        if run is not None and run.status == STATUS_ACTIVE and run.item_seed is not None:
            drawn = list(kind.drawn_key(run.item_traits))
            if run.item_key is None or run.item_drawn != drawn:
                if not draw:
                    return _ItemShot(kind, "", place, ITEM_PENDING_TAG)
                key = run.item_key or items_mod.new_cut_key(kind, user_id)
                result = await self._portrait(chat_id, key, kind, run.item_traits, run.item_seed)
                if result is None:
                    return None
                # Перечитываем: ход сцены мог сохранить Run, пока шёл портрет.
                fresh = await self._state.load_run(chat_id, radio.SCENARIO_ID) or run
                fresh.item_key, fresh.item_drawn = key, drawn
                if isinstance(result.get("seed"), int):
                    fresh.item_seed = result["seed"]
                await self._state.save_run(fresh)
                run = fresh
            return _ItemShot(kind, str(run.item_key), place, f"{run.item_key}:{','.join(drawn)}")
        item = await self._owned_item(user_id, kind)
        if item is None:
            return None
        if await self.speech_clear(user_id):
            # Старое радио убрано — на столе новый передатчик, один на всех.
            if not await self._store.get_state(NEW_RADIO_READY_KEY):
                if not draw:
                    return _ItemShot(kind, "", place, ITEM_PENDING_TAG)
                result = await self._portrait(chat_id, NEW_RADIO_CUT_KEY, kind, [], NEW_RADIO_SEED)
                if result is None:
                    return None
                await self._store.set_state(NEW_RADIO_READY_KEY, iso(self._now()))
            return _ItemShot(kind, NEW_RADIO_CUT_KEY, place, NEW_RADIO_CUT_KEY)
        cut = await self._item_cut(chat_id, item, kind, draw=draw)
        if cut is None:
            return None if draw else _ItemShot(kind, "", place, ITEM_PENDING_TAG)
        key, image_id = cut
        return _ItemShot(kind, key, place, f"item{item['id']}:{image_id}")

    async def _forget_item_cut(self, chat_id: int, user_id: int, shot: _ItemShot) -> None:
        if shot.key == NEW_RADIO_CUT_KEY:
            await self._store.set_state(NEW_RADIO_READY_KEY, "")
            return
        run = await self._state.load_run(chat_id, radio.SCENARIO_ID)
        if run is not None and run.item_key == shot.key:
            run.item_drawn = ["-"]  # не совпадёт ни с каким набором — перерисуем
            await self._state.save_run(run)

    async def _grant_item(
        self, chat_id: int, user_id: int, run: Run, message_thread_id: int | None
    ) -> None:
        """Финал сцены: старый передатчик со всеми чертами — гостю, карточкой."""
        kind = items_mod.KINDS[REGISTRY[run.scenario].item_kind or ""]
        if await self._store.items_of(user_id, kind.type):
            return  # уже выдан (повторная сцена в другом чате)
        key = run.item_key or items_mod.new_cut_key(kind, user_id)
        seed = run.item_seed if run.item_seed is not None else items_mod.new_seed()
        result = await self._portrait(chat_id, key, kind, run.item_traits, seed)
        image_id = None
        if result is not None:
            image_id = await self._save_portrait(chat_id, kind, key, run.item_traits, result)
        item_id = await self._store.add_item(
            type=kind.type,
            owner_user_id=user_id,
            traits=list(run.item_traits),
            image_id=image_id,
            origin="radio_scene",
            now=self._now(),
            note=run.finale_fault,
        )
        item = await self._store.item_by_id(item_id)
        if item is not None:
            await self._send_item_card(chat_id, user_id, item, message_thread_id=message_thread_id)

    async def _send_item_card(
        self,
        chat_id: int,
        user_id: int,
        item: dict[str, Any],
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> bool:
        kind = items_mod.KINDS.get(item["type"])
        if kind is None:
            return False
        installed = not await self.speech_clear(user_id)
        caption = item_caption(kind, item, installed)
        markup = item_keyboard(kind, int(item["id"]))
        image = await self._store.image_by_id(int(item["image_id"])) if item["image_id"] else None
        if image is None or not hasattr(self._notifier, "send_photo_ex"):
            message_id = await self._notifier.send_direct(
                chat_id, caption, reply_markup=markup, message_thread_id=message_thread_id
            )
            return message_id is not None
        photo: bytes | str = image["telegram_file_id"] or image_tools.upscale_png(
            image["png"], self._settings.llm.imagegen_display_px
        )
        sent = await self._notifier.send_photo_ex(
            chat_id,
            photo,
            caption=caption,
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            reply_markup=markup,
        )
        if sent is None:
            return False
        if not image["telegram_file_id"]:
            await self._store.set_image_sent(int(image["id"]), sent[1], sent[0])
        return True

    async def tool_manor_items(
        self,
        chat_id: int | None,
        user_id: int | None,
        *,
        show: str = "",
        message_thread_id: int | None = None,
        trigger_message_id: int | None = None,
    ) -> str:
        """Тул manor_items (Этап 49.3.6): опись особенных вещей поместья —
        Альфреду, не в чат: он отвечает гостю своими словами. ``show`` —
        гость просит показать вещь: её карточка с картинкой уходит в чат."""
        if chat_id is None or user_id is None:
            return radio.TOOL_ITEMS_UNAVAILABLE
        await self._owned_item(user_id, items_mod.RADIO)  # ленивая выдача
        owned = [it for it in await self._store.items_of(user_id) if it["type"] in items_mod.KINDS]
        if not owned:
            return radio.TOOL_ITEMS_NONE
        lines = await self.manor_lines(user_id, owned)
        if not show.strip():
            return radio.TOOL_ITEMS_LIST.format(lines=lines)
        kind = items_mod.find_kind(show)
        chosen = [it for it in owned if kind is not None and it["type"] == kind.type]
        if not chosen:
            return radio.TOOL_ITEMS_UNKNOWN.format(lines=lines)
        names, drawing = [], []
        for it in chosen[:ITEMS_SHOWN_MAX]:
            name = items_mod.KINDS[it["type"]].name
            if await self._show_card(
                chat_id, user_id, it, message_thread_id, reply_to_message_id=trigger_message_id
            ):
                names.append(name)
            else:
                drawing.append(name)
        if drawing and not names:
            return radio.TOOL_ITEMS_DRAWING.format(items=", ".join(drawing))
        return radio.TOOL_ITEMS_SENT.format(items=", ".join(names + drawing))

    async def manor_lines(self, user_id: int, owned: list[dict[str, Any]]) -> str:
        """Опись для Альфреда: где вещь, приметы, беда и что с ней можно —
        из действий вида, доступных сейчас."""
        lines = []
        for item in owned:
            kind = items_mod.KINDS[item["type"]]
            place = await self.item_place(user_id, item)
            traits = kind.traits_ru(item.get("traits") or [])
            note = (item.get("note") or "").rstrip(". ")
            actions = "; ".join(
                radio.TOOL_ITEMS_ACTION.format(what=action.tool_ru, name=action.name)
                for action in kind.available(place)
            )
            lines.append(
                radio.TOOL_ITEMS_LINE.format(
                    name=kind.name,
                    where=items_mod.PLACE_RU.get(place, place),
                    traits=radio.TOOL_ITEMS_TRAITS.format(traits="; ".join(traits))
                    if traits
                    else "",
                    fault=radio.TOOL_ITEMS_FAULT.format(fault=note) if note else "",
                    actions=actions or radio.TOOL_ITEMS_NO_ACTIONS,
                )
            )
        return "\n".join(lines)

    async def tool_item_action(
        self, chat_id: int | None, user_id: int | None, *, item: str, action: str
    ) -> str:
        """Тул item_action (Этап 49.3.7): гость просит сделать что-то с вещью
        поместья — форма подтверждения из данных действия уходит после речи
        Альфреда (flush_forms). Делает кнопка, не модель."""
        if chat_id is None or user_id is None:
            return radio.TOOL_ACTION_UNAVAILABLE
        await self._owned_item(user_id, items_mod.RADIO)  # ленивая выдача
        owned = [it for it in await self._store.items_of(user_id) if it["type"] in items_mod.KINDS]
        if not owned:
            return radio.TOOL_ITEMS_NONE
        lines = await self.manor_lines(user_id, owned)
        kind = items_mod.find_kind(item)
        chosen = next((it for it in owned if kind is not None and it["type"] == kind.type), None)
        if chosen is None or kind is None:
            return radio.TOOL_ITEMS_UNKNOWN.format(lines=lines)
        act = kind.action(action)
        if act is None:
            return radio.TOOL_ACTION_UNKNOWN.format(lines=lines)
        if act.pinned_locked and self._pinned(chat_id):
            return radio.TOOL_PINNED
        place = await self.item_place(user_id, chosen)
        if act not in kind.available(place):
            return radio.TOOL_ACTION_ALREADY.format(where=items_mod.PLACE_RU.get(place, place))
        queued = self._queued.setdefault(chat_id, _Queued())
        queued.user_id = user_id
        if (int(chosen["id"]), act.code) not in queued.items:
            queued.items.append((int(chosen["id"]), act.code))
        return radio.TOOL_ACTION_FORM.format(form=_plain(act.form))

    async def item_place(self, user_id: int, item: dict[str, Any]) -> str:
        """Где вещь. У радио правда — речь Альфреда: старый передатчик стоит,
        пока картавость не снята (speech_clear), иначе он в чулане."""
        if item["type"] == items_mod.RADIO_TYPE and not await self.speech_clear(user_id):
            return items_mod.PLACE_DESK
        return items_mod.PLACE_STOREROOM

    async def inventory(self, user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
        """Опись вещей гостя (/items): текст и кнопки «принести карточку»."""
        await self._owned_item(user_id, items_mod.RADIO)  # ленивая выдача
        owned = [it for it in await self._store.items_of(user_id) if it["type"] in items_mod.KINDS]
        entries = [
            (items_mod.KINDS[it["type"]], it, await self.item_place(user_id, it)) for it in owned
        ]
        return inventory_text(entries), inventory_keyboard(owned)

    async def send_inventory(
        self,
        chat_id: int,
        user_id: int,
        *,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> bool:
        text, markup = await self.inventory(user_id)
        sent = await self._notifier.send_direct(
            chat_id,
            text,
            reply_to_message_id=reply_to_message_id,
            reply_markup=markup,
            message_thread_id=message_thread_id,
        )
        return sent is not None

    async def _show_card(
        self,
        chat_id: int,
        user_id: int,
        item: dict[str, Any],
        message_thread_id: int | None,
        *,
        reply_to_message_id: int | None = None,
    ) -> bool:
        """Карточка вещи; портрета ещё нет — рисуется в фоне, карточка придёт
        сама (False)."""
        kind = items_mod.KINDS[item["type"]]
        if item["image_id"]:
            await self._send_item_card(
                chat_id,
                user_id,
                item,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
            )
            return True
        self._spawn(
            self._draw_and_send_card(chat_id, user_id, item, kind, message_thread_id),
            "карточка предмета",
        )
        return False

    async def _draw_and_send_card(
        self,
        chat_id: int,
        user_id: int,
        item: dict[str, Any],
        kind: items_mod.ItemKind,
        message_thread_id: int | None,
    ) -> None:
        await self._item_cut(chat_id, item, kind, draw=True)
        await self._send_item_card(chat_id, user_id, item, message_thread_id=message_thread_id)

    async def handle_item_click(
        self,
        chat_id: int,
        user_id: int,
        item_id: int,
        button: str,
        *,
        message_id: int | None = None,
        message_thread_id: int | None = None,
        photo: bool = False,
    ) -> tuple[str, InlineKeyboardMarkup | None, str | None]:
        """Кнопка карточки, описи или формы вещи. Возвращает (текст для
        callback.answer, новая клавиатура, новая подпись/текст) — None: не
        трогать.

        Карточка: «Действия» → сообщение сменяется перечнем действий вида с
        «Назад»; действие или «Назад» возвращают карточку. Из описи /items
        (префикс ``ITEM_FROM_INVENTORY``) — то же, но возвращается опись.
        Форма тула (префикс ``ITEM_FROM_FORM``) закрывается итогом. Раскрытый
        перечень помнится живыми кнопками: следующее сообщение гостя его
        свернёт. ``photo`` — карточка с картинкой (свернуть — правкой подписи)."""
        item = await self._store.item_by_id(item_id)
        if item is None or int(item["owner_user_id"]) != user_id:
            return radio.ITEM_NOT_YOURS, None, None
        kind = items_mod.KINDS.get(item["type"])
        if button == ITEM_BTN_CARD and kind is not None:
            shown = await self._show_card(chat_id, user_id, item, message_thread_id)
            return (radio.ITEM_CARD_SHOWN if shown else radio.ITEM_CARD_DRAWING), None, None
        if kind is None or not kind.actions:
            return "Неизвестная кнопка.", None, None
        origin = ""
        if len(button) > 1 and button[0] in (ITEM_FROM_INVENTORY, ITEM_FROM_FORM):
            origin, button = button[0], button[1:]
        place = await self.item_place(user_id, item)
        if button == ITEM_BTN_ACTIONS and origin != ITEM_FROM_FORM:
            if message_id is not None:
                await self._remember_buttons(
                    chat_id,
                    message_id,
                    user_id,
                    LIVE_MENU,
                    item=item_id,
                    photo=photo,
                    inv=origin == ITEM_FROM_INVENTORY,
                )
            return (
                "",
                item_actions_keyboard(kind, item_id, place, origin),
                item_actions_caption(kind, place),
            )
        if button == ITEM_BTN_BACK and origin != ITEM_FROM_FORM:
            await self._forget_buttons(chat_id, message_id)
            return ("", *await self._item_view(user_id, kind, item, origin, place))
        if origin == ITEM_FROM_FORM and button.startswith(ITEM_BTN_KEEP):
            kept = kind.action(button[len(ITEM_BTN_KEEP) :])
            await self._forget_buttons(chat_id, message_id)
            text = radio.ITEM_FORM_KEPT.format(form=kept.form) if kept is not None else None
            return "Оставили как есть.", _NO_BUTTONS, text
        action = kind.action(button)
        if action is None:
            return "Неизвестная кнопка.", None, None
        if action.pinned_locked and self._pinned(chat_id):
            return radio.ITEM_PINNED, None, None
        await self._forget_buttons(chat_id, message_id)
        answer = "Уже так."
        if action in kind.available(place):
            await getattr(self, self._ITEM_EFFECTS[action.effect])(chat_id, user_id, item, action)
            if action.event:
                await self._store.add_item_event(
                    item_id,
                    action.event,
                    user_id=user_id,
                    chat_id=chat_id,
                    data=None,
                    now=self._now(),
                )
            if action.directive:
                where = await self._where(chat_id, message_id, message_thread_id)
                await self._speak(chat_id, action.directive, where)
            answer = action.done
            place = await self.item_place(user_id, item)
        if origin == ITEM_FROM_FORM:
            return (
                answer,
                _NO_BUTTONS,
                radio.ITEM_FORM_DONE.format(form=action.form, confirm=action.confirm),
            )
        return (answer, *await self._item_view(user_id, kind, item, origin, place))

    async def _item_view(
        self,
        user_id: int,
        kind: items_mod.ItemKind,
        item: dict[str, Any],
        origin: str,
        place: str,
    ) -> tuple[InlineKeyboardMarkup, str]:
        """Куда вернуться из перечня: опись /items или карточка вещи."""
        if origin == ITEM_FROM_INVENTORY:
            text, markup = await self.inventory(user_id)
            return markup or _NO_BUTTONS, text
        card = item_keyboard(kind, int(item["id"])) or _NO_BUTTONS
        return card, item_caption(kind, item, place == items_mod.PLACE_DESK)

    # Эффекты действий вещей (ItemAction.effect → метод). Новый рычаг —
    # новый метод здесь и действие в данных вида (items.py).
    _ITEM_EFFECTS = {"radio_desk": "_effect_radio_desk"}

    async def _effect_radio_desk(
        self, chat_id: int, user_id: int, item: dict[str, Any], action: items_mod.ItemAction
    ) -> None:
        """Старая, проклятая радиостанция на столе — картавость есть
        (speech_clear выключен); убрана — речь чистая."""
        await self._set_clear(chat_id, user_id, action.arg == "remove")

    # --- запрет в переписке ---

    async def set_opted_out(self, chat_id: int, opted_out: bool) -> None:
        await self._state.set_opted_out(chat_id, opted_out)

    async def is_opted_out(self, chat_id: int) -> bool:
        return await self._state.is_opted_out(chat_id)


def photo_state_key(cab: Cabinet, outside: Outside, shot: _ItemShot | None) -> str:
    """Ключ повторного показа общего вида: обстановка и свет, а если на стол
    вставляется предмет — ещё и версия его облика (Этап 49.3)."""
    key = cab.state_key(outside.key)
    if shot is not None and shot.place == "desk":
        key += f"#{shot.tag}"
    return key


def item_caption(kind: items_mod.ItemKind, item: dict[str, Any], installed: bool) -> str:
    traits = kind.traits_ru(item.get("traits") or [])
    note = item.get("note")
    return radio.ITEM_CARD_CAPTION.format(
        name=html.escape(kind.name),
        traits=radio.ITEM_CARD_TRAITS.format(traits=html.escape("; ".join(traits)))
        if traits
        else "",
        fault=radio.ITEM_CARD_FAULT.format(fault=html.escape(note.rstrip(". "))) if note else "",
        where=radio.ITEM_CARD_INSTALLED if installed else radio.ITEM_CARD_STORED,
    )


def photo_line_directive(
    focus: str, description: str, where: str, missing: list[str], misses: int
) -> str:
    """Директива подписи к снимку: что вышло — или чего не вышло (промах)."""
    template = cabinet_mod.PHOTO_READY_DIRECTIVE
    if missing:
        template = cabinet_mod.PHOTO_READY_MISS_DIRECTIVE
        if misses > 1:
            template = cabinet_mod.PHOTO_READY_MISS_AGAIN_DIRECTIVE
    return template.format(
        subject=focus or cabinet_mod.PHOTO_SUBJECT_ROOM,
        description=description,
        where=where,
        missing=", ".join(missing),
    )


def photo_expect(raw: Any) -> list[str]:
    """``expect`` тула take_photo → 1-3 непустых пункта."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    items = [" ".join(item.split()) for item in raw if isinstance(item, str) and item.strip()]
    return items[:3]


def _image_params(image: dict[str, Any]) -> dict[str, Any]:
    try:
        params = json.loads(image.get("params") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return params if isinstance(params, dict) else {}


def scene_photo_focus(
    run: Run, decision: DirectorDecision | None, *, key_moment: bool
) -> str | None:
    """Нужен ли кадр после хода сцены и что в нём крупно ("" — общий вид).
    Переход стадии и финал снимаются всегда; по желанию Ведущего — не чаще
    раза в PHOTO_SCENE_GAP_TURNS ходов."""
    wanted = decision.photo if decision is not None else None
    if key_moment:
        return wanted or ""
    if wanted is None:
        return None
    if run.photo_turn is not None and run.turns_total - run.photo_turn < PHOTO_SCENE_GAP_TURNS:
        return None
    return wanted


def retake_emphasis(prev: dict[str, Any] | None, expect: list[str]) -> list[str]:
    """Что из ``expect`` не вышло на прошлом снимке чата — снимаем снова.
    Модель при пересъёмке может перефразировать пункт, поэтому сверка без
    регистра и по вхождению (как в llm/photo_check.parse)."""
    if not prev or not expect:
        return []
    missed = [m.lower() for m in prev.get("missing") or [] if isinstance(m, str) and m.strip()]
    return [
        item
        for item in expect
        if any(m == item.lower() or m in item.lower() or item.lower() in m for m in missed)
    ]


def photo_description(
    cab: Cabinet,
    outside: Outside,
    focus: str,
    happening: str | None,
    *,
    item: items_mod.ItemKind | None = None,
    item_place: str | None = None,
    alfred: str | None = None,
) -> tuple[str, str]:
    """Описание снимка для художника-промптера (llm/image_prompt.py) и
    контекст сцены. Общий вид — особенности гостя первыми (самые свежие:
    промпт ограничен 77 токенами; случившееся в сцене — ещё раньше), потом
    канон и свет; крупный план —
    предмет, а кабинет со светом — контекстом.

    ``item_place`` (Этап 49.3) — в кадр вставят предмет ``item`` пикселями
    (``desk`` — на стол общего вида, ``closeup`` — крупно): словами его в
    описании быть не должно, иначе промптер нарисует второй, свой."""
    visible_features = cab.visible_features()
    if item is not None and item_place is not None:
        visible_features = [f for f in visible_features if not item.focus_re.search(f)]
        if happening and item.focus_re.search(happening):
            happening = None
        if item_place == "closeup":
            focus = ITEM_CLOSEUP_RU
    # Альфред спиной/боком (кадр события сцены): его фраза идёт сразу после
    # главного, а места ей даём, убрав особенности кабинета до одной.
    alfred_en = cabinet_mod.SCENE_ALFRED_EN.get(alfred or "")
    features = visible_features[-(1 if alfred_en else PHOTO_FEATURES_IN_FRAME) :]
    light = outside.en(closeup=bool(focus))
    if focus:
        # Крупный план — только предмет, комната и свет: особенности кабинета
        # в контексте промптер тащил в кадр (снимок картины с рунами и
        # туманом, живая находка 2026-09-30).
        context = f"{cabinet_mod.CANON_EN}; {light}"
        if happening:
            context += f"; just happened: {happening}"
        if alfred_en:
            focus = f"{focus}. Also in frame: {alfred_en}."
        return focus, context
    # Порядок — по важности: промптер ставит первое главным, а хвост
    # срезается под 77 токенов CLIP.
    parts = []
    if happening:
        parts.append(f"Main subject, just happened: {happening}")
    if alfred_en:
        parts.append(f"In frame: {alfred_en}.")
    if features:
        parts.append("Must be clearly visible: " + "; ".join(features) + ".")
    # Композицию «стол в нижней трети» под вставку дописывает служба llm
    # (item_paste.DESK_COMPOSITION) — промптер её пересказывал и терял.
    parts.append(f"Room: {cabinet_mod.CANON_EN}.")
    parts.append(f"Light: {light}.")
    return " ".join(parts), ""


def photo_subject(focus: str, *, selfie: bool) -> str:
    """Что снято — словами для подписи (photo_line_directive)."""
    if not selfie:
        return focus
    if focus:
        return cabinet_mod.PHOTO_SUBJECT_SELF_DOING.format(focus=focus)
    return cabinet_mod.PHOTO_SUBJECT_SELF


def selfie_description(
    cab: Cabinet, outside: Outside, focus: str, happening: str | None
) -> str:
    """Снимок себя (take_photo selfie): Альфред и что он делает — первым,
    случившееся в сцене — следом, дальше как у портрета."""
    subject = (
        cabinet_mod.SELFIE_ACTION_EN.format(action=focus.rstrip(" ."))
        if focus
        else cabinet_mod.SELFIE_SUBJECT_EN
    )
    if happening:
        subject += f" Just happened around him: {happening}."
    pose = bool(focus) and bool(cabinet_mod.SELFIE_POSE_RE.search(focus))
    return portrait_description(cab, outside, subject=subject, pose=pose)


def portrait_description(
    cab: Cabinet,
    outside: Outside,
    *,
    subject: str = cabinet_mod.PORTRAIT_SUBJECT_EN,
    pose: bool = False,
) -> str:
    """Описание портрета к приветствию для промптера: сам Альфред первым,
    потом свет (время суток и погода — как у снимков кабинета), потом
    обстановка гостя. Особенностей — меньше, чем у снимка: место в 77
    токенах CLIP занимает Альфред. ``pose`` — селфи в ракурсе: голая
    комната без особенностей (cabinet.SELFIE_POSE_RE)."""
    parts = [subject, f"Light: {outside.en()}."]
    if pose:
        parts.append(f"Room: {cabinet_mod.SELFIE_POSE_ROOM_EN}.")
        return " ".join(parts)
    features = cab.visible_features()[-1:]
    if features:
        parts.append("Also visible: " + "; ".join(features) + ".")
    parts.append(f"Room: {cabinet_mod.CANON_EN}.")
    return " ".join(parts)


def _plain(text_html: str) -> str:
    return html.unescape(_TAG_RE.sub("", text_html))
