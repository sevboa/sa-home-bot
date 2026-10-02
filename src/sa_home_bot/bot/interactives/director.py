"""Ведущий интерактивов (Этап 47): отдельный LLM-вызов после ответа Альфреда.

Ведущий — режиссёр, не персонаж. Он видит журнал сцены, интерпретирует
действие Альфреда (``effect`` — что из этого вышло, уходит гостю отдельным
сообщением рассказчика), решает, пора ли усилить подсказку Альфреду
(``stage``), и в финале придумывает поломку (``finale_fault``). Решения
Ведущего — только предложения: рамки темпа накладывает движок
(engine.apply_decision), чтобы Альфред не нашёл разгадку слишком быстро и
сцена не тянулась бесконечно.

Вызов идёт в службу llm с role=director (llm/service.py::_director_chat):
свой system, JSON-ответ, без Логопеда. Любой сбой (служба спит, битый JSON)
— ``None``: ход сцены пропускается, диалог не ломается.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sa_home_bot.bot.interactives import items as items_mod
from sa_home_bot.bot.interactives.base import Run, Scenario
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.proto.messages import Address, ProtoError

log = logging.getLogger(__name__)

ROLE_DIRECTOR = "director"
ACTION_CHAT = "chat"

EFFECT_MAX_CHARS = 600
DIRECTIVE_MAX_CHARS = 600
CABINET_ADD_MAX = 3
PHOTO_FOCUS_MAX = 200

# Настроение кадра — Ведущий выбирает, код переводит в модель и LoRA
# (engine.MOOD_PRESETS). Русские слова — как их видит Ведущий.
MOOD_PLAIN = "plain"
MOODS = {
    "обычно": MOOD_PLAIN,
    "жуть": "horror",
    "гниль": "rot",
    "потустороннее": "eldritch",
}

# Особенности кабинета вне сцены (первый снимок до всякой сцены) — отдельный
# маленький вызов той же роли. Нестрогий промпт: вариантов не перечисляем,
# у каждого гостя кабинет свой (решение пользователя 2026-09-30).
FEATURES_SYSTEM = (
    "Ты — Ведущий: придумываешь обстановку мира, в котором живёт Альфред — "
    "старый дворецкий в замке в Трансильвании. Пиши по-русски. Отвечай строго "
    "одним JSON-объектом."
)


@dataclass(frozen=True)
class DirectorDecision:
    active: bool
    stage: int
    finale: bool
    effect: str | None
    directive: str | None
    finale_fault: str | None
    note: str | None
    # Новые устойчивые детали кабинета гостя (cabinet.py) — остаются после сцены.
    cabinet_add: tuple[str, ...] = ()
    # Кадр гостю в ключевой момент: что снять крупно; "" — общий вид; None — не надо.
    photo: str | None = None
    # Настроение кадров сцены (MOODS); None — Ведущий не сказал, остаётся прежнее.
    mood: str | None = None
    # Новая черта предмета сцены (ключ из items.ItemKind.traits) или None.
    item_trait: str | None = None


def _clean(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def _json_object(raw: str) -> dict | None:
    """Модель может обернуть JSON в ```-блок или дописать текст вокруг —
    берём первый {...}."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _str_list(value: Any, limit: int) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    items = (_clean(v, EFFECT_MAX_CHARS) for v in value)
    return tuple(v for v in items if v)[:limit]


def _photo(value: Any) -> str | None:
    if value is True:
        return ""
    return _clean(value, PHOTO_FOCUS_MAX)


def _mood(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    if text in MOODS.values():
        return text
    return MOODS.get(text)


def _item_trait(value: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().strip("«»\"'").casefold()
    return text if text in keys else None


def parse_decision(
    raw: str, current_stage: int, trait_keys: tuple[str, ...] = ()
) -> DirectorDecision | None:
    """JSON Ведущего → решение. ``trait_keys`` — допустимые черты предмета
    сцены: прочее (придуманное Ведущим) отбрасывается."""
    data = _json_object(raw)
    if data is None:
        return None
    stage = data.get("stage")
    if not isinstance(stage, int) or isinstance(stage, bool):
        stage = current_stage
    return DirectorDecision(
        active=data.get("active") is not False,
        stage=stage,
        finale=data.get("finale") is True,
        effect=_clean(data.get("effect"), EFFECT_MAX_CHARS),
        directive=_clean(data.get("directive"), DIRECTIVE_MAX_CHARS),
        finale_fault=_clean(data.get("finale_fault"), EFFECT_MAX_CHARS),
        note=_clean(data.get("note"), 300),
        cabinet_add=_str_list(data.get("cabinet_add"), CABINET_ADD_MAX),
        photo=_photo(data.get("photo")),
        mood=_mood(data.get("mood")),
        item_trait=_item_trait(data.get("item_trait"), trait_keys),
    )


def _cabinet_block(place: str, outside: str, need: int) -> str:
    """Где идёт сцена — кабинет гостя и что за окном. ``need`` > 0 — у
    кабинета ещё нет особенностей, пусть Ведущий придумает первые."""
    if need:
        task = (
            f"У этого кабинета ещё нет своих особенностей — придумай {need} в "
            "cabinet_add: необычные, но уместные в кабинете старого дворецкого, "
            "с лёгкой жутью или странностью, заметные глазу. Каждая — "
            "конкретный предмет и где он, 3-6 слов («чучело совы на шкафу», "
            "«треснувший портрет над камином»): без процессов и настроения, "
            "без запахов и звуков."
        )
    else:
        task = (
            "Если в этом ходе в кабинете появилось или обнаружилось что-то, что "
            "останется надолго и видно на фотографии, — добавь это в cabinet_add "
            "(0–2 пункта). Только видимое и постоянное: предмет, след, пятно, "
            "свечение — конкретная вещь и где она, 3-6 слов («чёрное пятно на "
            "столе»), без процессов («медленно стекает», «стало ритмичным»). "
            "Не пиши запахи, звуки, температуру и мгновенные события "
            "(«часы на миг остановились») — это для effect. Не повторяй и не "
            "противоречь уже известным особенностям."
        )
    return f"Место сцены: {place}\nЗа окном сейчас: {outside}.\n{task}\n\n"


def build_features_input(place: str, outside: str, count: int) -> str:
    return (
        _cabinet_block(place, outside, count)
        + 'Ответь ОДНИМ JSON-объектом: {"cabinet_add": [str, ...]} — каждая '
        "деталь одной короткой фразой."
    )


def build_director_input(
    scenario: Scenario,
    run: Run,
    *,
    finale_allowed: bool,
    place: str | None = None,
    outside: str | None = None,
    need_features: int = 0,
) -> str:
    ladder = "\n".join(f"  {i}. {hint}" for i, hint in enumerate(scenario.ladder))
    notes = "\n".join(f"- {n}" for n in run.notes) or "—"
    transcript = "\n".join(run.transcript) or "—"
    if run.finale:
        finale_line = (
            f"ФИНАЛ УЖЕ НАСТУПИЛ. Поломка: {run.finale_fault}. Альфред должен "
            "предлагать сменить устройство; любые альтернативы гостя не "
            "срабатывают — придумай почему и возвращай к замене."
        )
    elif finale_allowed:
        finale_line = "Финал РАЗРЕШЁН: можешь поставить finale=true, если сцена созрела."
    else:
        finale_line = "Финал пока ЗАПРЕЩЁН (рано) — finale=false."
    cabinet = _cabinet_block(place, outside or "неизвестно", need_features) if place else ""
    cabinet_field = (
        (
            ',\n "cabinet_add": [str] — новые устойчивые видимые детали кабинета (или [])'
            ',\n "photo": str|null — кадр гостю: что снять крупно, коротко по-русски '
            '("" — общий вид кабинета). Только в ключевой момент сцены: впервые '
            "видимая странность, заметная перемена в кабинете, переход на новую "
            "стадию, финал. Обычно null."
            ',\n "mood": str — настроение кадров сейчас, одно из: «обычно», «жуть» '
            "(кошмар, плоть сливается с техникой), «гниль» (плесень, тлен, "
            "ржавчина), «потустороннее» (нездешнее, щупальца, иной мир). Держи "
            "«обычно», пока в кабинете не творится действительно жуткое."
        )
        if place
        else ""
    )
    item_block, item_field = _item_block(scenario, run)
    return (
        f"Сценарий: {scenario.title}\n"
        f"Лестница подсказок Альфреду (стадии):\n{ladder}\n"
        f"Текущая стадия: {run.stage}. Ходов сцены: {run.turns_total}, "
        f"из них на этой стадии: {run.turns_on_stage}.\n"
        f"{finale_line}\n"
        f"Твои заметки с прошлых ходов:\n{notes}\n\n"
        f"Журнал сцены (последняя реплика — только что сказанное Альфредом):\n"
        f"{transcript}\n\n"
        f"{cabinet}"
        f"{item_block}"
        "Ответь ОДНИМ JSON-объектом с полями:\n"
        '{"active": bool — сцена продолжается (false, если гость явно ушёл '
        "в другую тему),\n"
        ' "effect": str|null — что произошло в ответ на действие Альфреда '
        "(1–2 предложения, от лица рассказчика; null, если Альфред ничего не "
        "делал),\n"
        ' "stage": int — стадия на следующий ход (можно оставить или +1),\n'
        ' "directive": str — скрытая подсказка Альфреду на следующий ход '
        "(1–2 предложения),\n"
        ' "finale": bool,\n'
        ' "finale_fault": str|null — только при finale=true: смешная '
        "потусторонняя поломка радиостанции,\n"
        ' "note": str|null — короткая заметка себе на будущее'
        f"{cabinet_field}{item_field}}}"
    )


def _item_block(scenario: Scenario, run: Run) -> tuple[str, str]:
    """Облик предмета сцены (Этап 49.3): какие черты уже есть и какие можно
    добавить — только из списка, по одной."""
    kind = items_mod.KINDS.get(scenario.item_kind or "")
    if kind is None:
        return "", ""
    have = "; ".join(kind.traits_ru(run.item_traits)) or "пока ничего особенного"
    free = [k for k in kind.traits if k not in run.item_traits]
    if not free:
        return f"Как выглядит радиостанция: {have}.\n\n", ""
    options = "; ".join(f"«{k}» — {kind.traits[k].ru}" for k in free)
    block = (
        f"Как выглядит радиостанция: {have}.\n"
        f"Чем его облик может обрасти по ходу сцены (ключ — что видно): {options}.\n\n"
    )
    field_text = (
        ',\n "item_trait": str|null — ключ ОДНОЙ новой черты облика радиостанции из '
        "списка, если в этом ходе она проявилась (опиши её и в effect); обычно null. "
        "На стадиях 0 и 1 — только неприметное (пыль, трещина)"
    )
    return block, field_text


async def ask_director(
    node_link: ServiceLink,
    dst: Address,
    timeout: float,
    scenario: Scenario,
    run: Run,
    *,
    finale_allowed: bool,
    place: str | None = None,
    outside: str | None = None,
    need_features: int = 0,
) -> DirectorDecision | None:
    content = build_director_input(
        scenario,
        run,
        finale_allowed=finale_allowed,
        place=place,
        outside=outside,
        need_features=need_features,
    )
    args: dict[str, Any] = {
        "messages": [{"role": "user", "content": content}],
        "role": ROLE_DIRECTOR,
        "system": scenario.director_prompt,
        "chat_id": run.chat_id,
    }
    try:
        result = await node_link.command(ACTION_CHAT, args, dst=dst, timeout=timeout)
    except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
        log.warning("interactives: Ведущий недоступен (chat=%s): %s", run.chat_id, exc)
        return None
    raw = result.get("response", "") if isinstance(result, dict) else ""
    kind = items_mod.KINDS.get(scenario.item_kind or "")
    decision = parse_decision(raw, run.stage, tuple(kind.traits) if kind else ())
    if decision is None:
        log.warning("interactives: Ведущий вернул не JSON (chat=%s): %r", run.chat_id, raw[:300])
    return decision


async def ask_features(
    node_link: ServiceLink,
    dst: Address,
    timeout: float,
    *,
    chat_id: int,
    place: str,
    outside: str,
    count: int,
) -> tuple[str, ...]:
    """Первые особенности кабинета вне сцены. Сбой — пусто: снимок и без них
    получится, особенности допишутся в следующий раз."""
    args: dict[str, Any] = {
        "messages": [{"role": "user", "content": build_features_input(place, outside, count)}],
        "role": ROLE_DIRECTOR,
        "system": FEATURES_SYSTEM,
        "chat_id": chat_id,
    }
    try:
        result = await node_link.command(ACTION_CHAT, args, dst=dst, timeout=timeout)
    except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
        log.warning("interactives: Ведущий недоступен для кабинета (chat=%s): %s", chat_id, exc)
        return ()
    raw = result.get("response", "") if isinstance(result, dict) else ""
    data = _json_object(raw)
    if data is None:
        return ()
    return _str_list(data.get("cabinet_add"), count)
