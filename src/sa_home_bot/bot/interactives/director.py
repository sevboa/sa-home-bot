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

from sa_home_bot.bot.interactives.base import Run, Scenario
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.proto.messages import Address, ProtoError

log = logging.getLogger(__name__)

ROLE_DIRECTOR = "director"
ACTION_CHAT = "chat"

EFFECT_MAX_CHARS = 600
DIRECTIVE_MAX_CHARS = 600


@dataclass(frozen=True)
class DirectorDecision:
    active: bool
    stage: int
    finale: bool
    effect: str | None
    directive: str | None
    finale_fault: str | None
    note: str | None


def _clean(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def parse_decision(raw: str, current_stage: int) -> DirectorDecision | None:
    """JSON Ведущего → решение. Модель может обернуть JSON в ```-блок или
    дописать текст вокруг — берём первый {...}."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
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
    )


def build_director_input(scenario: Scenario, run: Run, *, finale_allowed: bool) -> str:
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
    return (
        f"Сценарий: {scenario.title}\n"
        f"Лестница подсказок Альфреду (стадии):\n{ladder}\n"
        f"Текущая стадия: {run.stage}. Ходов сцены: {run.turns_total}, "
        f"из них на этой стадии: {run.turns_on_stage}.\n"
        f"{finale_line}\n"
        f"Твои заметки с прошлых ходов:\n{notes}\n\n"
        f"Журнал сцены (последняя реплика — только что сказанное Альфредом):\n"
        f"{transcript}\n\n"
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
        "потусторонняя поломка передатчика,\n"
        ' "note": str|null — короткая заметка себе на будущее}'
    )


async def ask_director(
    node_link: ServiceLink,
    dst: Address,
    timeout: float,
    scenario: Scenario,
    run: Run,
    *,
    finale_allowed: bool,
) -> DirectorDecision | None:
    args: dict[str, Any] = {
        "messages": [
            {
                "role": "user",
                "content": build_director_input(scenario, run, finale_allowed=finale_allowed),
            }
        ],
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
    decision = parse_decision(raw, run.stage)
    if decision is None:
        log.warning("interactives: Ведущий вернул не JSON (chat=%s): %r", run.chat_id, raw[:300])
    return decision
