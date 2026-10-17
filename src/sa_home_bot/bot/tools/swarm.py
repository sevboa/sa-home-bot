from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sa_home_bot import wake_core
from sa_home_bot.bot import (
    commands,
)
from sa_home_bot.bot.monitor_state import parse_disk_summary, parse_health_state
from sa_home_bot.bot.tools.core import (
    NO_SWARM_LINK,
    CommandRight,
    ToolContext,
    ToolSpec,
    VariantRights,
    bounded_limit,
    text_arg,
)
from sa_home_bot.node.kind import traits_for
from sa_home_bot.utils.clock import utc_now

# --- swarm_status: read-only состояние роя (LLM_INTEGRATION_PLAN.md §8.3) ---
#
# Сбор данных переиспользует wake_core.collect_reports — тот же веерный опрос,
# что и у сводки /swarm (bot/swarm_view.py), только без Telegram-рендеринга:
# модель получает JSON, а не строку с эмодзи. Второго пути к данным здесь нет.

WHAT_NODES = "nodes"
WHAT_HEALTH = "health"
WHAT_DISKS = "disks"


async def own_state(ctx: ToolContext) -> dict[str, Any] | None:
    if ctx.node_link is None:
        return None
    return await wake_core.fetch_state(ctx.node_link, None)


def _node_summary(report: wake_core.NodeReport) -> dict[str, Any]:
    traits = traits_for(report.kind)
    summary: dict[str, Any] = {
        "node": report.node_id,
        "kind": report.kind or "неизвестно",
        "online": report.alive and report.state is not None,
    }
    if not summary["online"]:
        # Различие «спит — это норма» vs «пропала машина, обязанная быть в
        # сети» — правило роя (ARCHITECTURE §11 п. 4). Отдаём полем, а не
        # эмодзи: решение, как это сказать, остаётся за персонажем.
        summary["sleeping_is_normal"] = not traits.always_on
        return summary
    state = report.state or {}
    services = state.get("services", [])
    summary["version"] = state.get("version", "?")
    summary["services_running"] = sum(1 for s in services if s.get("status") == "running")
    summary["services_total"] = len(services)
    if state.get("system_uptime_s") is not None:
        summary["uptime_s"] = state["system_uptime_s"]
    return summary


def _health_summary(report: wake_core.NodeReport) -> dict[str, Any]:
    entry: dict[str, Any] = {"node": report.node_id}
    if report.monitor is None:
        entry["error"] = "монитор не отвечает"
        return entry
    components = []
    for raw in report.monitor.get("health", []):
        try:
            state = parse_health_state(raw)
        except KeyError:
            continue  # монитор старой версии — пропускаем, а не роняем тул
        components.append(
            {
                "label": state.label,
                "kind": state.kind,
                "status": state.status,
                "temperature_c": state.temperature_c,
            }
        )
    entry["components"] = components
    if report.monitor.get("requirements"):
        entry["requirements_unmet"] = [r.get("id") for r in report.monitor["requirements"]]
    return entry


def _disks_summary(report: wake_core.NodeReport) -> dict[str, Any]:
    entry: dict[str, Any] = {"node": report.node_id}
    if report.monitor is None:
        entry["error"] = "монитор не отвечает"
        return entry
    disks = []
    for raw in report.monitor.get("disks", []):
        try:
            disk = parse_disk_summary(raw)
        except KeyError:
            continue
        disks.append(
            {
                "label": disk.label,
                "kind": disk.kind,
                "health": disk.health,
                "temperature_c": disk.temperature_c,
                "free_bytes": disk.free_bytes,
                "total_bytes": disk.total_bytes,
            }
        )
    entry["disks"] = disks
    if report.monitor.get("uptime_s") is not None:
        entry["monitor_uptime_s"] = report.monitor["uptime_s"]
    return entry


async def tool_swarm_status(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.node_link is None:
        return NO_SWARM_LINK
    what = text_arg(args, "what")
    wanted_node = args.get("node")
    wanted_node = str(wanted_node).strip() if wanted_node else None

    # Права уже проверены при сборке комплекта (tools_for): значений, которых
    # собеседнику не положено, в enum не было. Но модель может передать
    # что угодно, поэтому сверяемся ещё раз — по той же подписке.
    refusal = _SWARM_VARIANTS.refusal_for(ctx.subscription, what)
    if refusal is not None:
        return refusal

    own = await own_state(ctx)
    if own is None:
        return "недоступно: своя нода не отвечает"
    with_monitor = what in (WHAT_HEALTH, WHAT_DISKS)
    reports = await wake_core.collect_reports(ctx.node_link, own, with_monitor=with_monitor)
    if wanted_node is not None:
        picked = [r for r in reports if r.node_id == wanted_node]
        if not picked:
            known = ", ".join(r.node_id for r in reports) or "нет данных"
            return f"нет такой ноды: {wanted_node} (известны: {known})"
        reports = picked

    if what == WHAT_NODES:
        return json.dumps({"nodes": [_node_summary(r) for r in reports]}, ensure_ascii=False)
    if what == WHAT_HEALTH:
        return json.dumps({"health": [_health_summary(r) for r in reports]}, ensure_ascii=False)
    return json.dumps({"disks": [_disks_summary(r) for r in reports]}, ensure_ascii=False)


_SWARM_VARIANTS = VariantRights(
    param="what",
    rights=(
        # Право на данные — то же, чем гейтится команда бота с теми же данными
        # (/nodes, /status): Альфред не расширяет доступ, а повторяет его.
        (WHAT_NODES, CommandRight(commands.NODES.name)),
        (WHAT_HEALTH, CommandRight(commands.STATUS.name)),
        (WHAT_DISKS, CommandRight(commands.STATUS.name)),
    ),
)

_DECL_SWARM_STATUS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "swarm_status",
        "description": (
            "Узнать реальное состояние домашнего роя машин: какие ноды сейчас "
            "в сети или спят, температуры и здоровье железа, диски и место на "
            "них. Используй для ЛЮБОГО вопроса про то, как себя чувствуют "
            "машины и что с ними происходит — не отвечай по памяти, состояние "
            "меняется постоянно. Про торренты (что качается, место под "
            "закачки) — отдельный инструмент torrents, не этот. "
            "Значения what перечислены в enum: то, чего там нет, ты не умеешь."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "what": {
                    "type": "string",
                    # enum подставляется под права собеседника (см. tools_for).
                    "enum": [v for v, _ in _SWARM_VARIANTS.rights],
                    "description": (
                        "nodes — состав роя, кто в сети и кто спит; "
                        "health — температуры и здоровье компонентов; "
                        "disks — диски, место и их состояние"
                    ),
                },
                "node": {
                    "type": "string",
                    "description": (
                        "Имя конкретной ноды (например: alfred), если "
                        "спрашивают про одну машину. Без этого — по всему рою."
                    ),
                },
            },
            "required": ["what"],
        },
    },
}


# --- swarm_events: журнал того, что уже произошло (в отличие от swarm_status —
# текущего состояния) — bot/node_events.py::store.record_event пишет туда те
# же строки, что уходят в рассылку. Решение пользователя 2026-08-04: до этого
# у Альфреда не было доступа даже к тому, что уже случилось, не то что к
# системным логам — только к состоянию /nodes прямо сейчас. Право — то же,
# чем гейтится /nodes (CommandRight, не отдельное): это те же данные о рое,
# только в прошедшем времени, не повод заводить новое право.

DEFAULT_SWARM_EVENTS_LIMIT = 20
MAX_SWARM_EVENTS_LIMIT = 100


async def tool_swarm_events(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.store is None:
        return "недоступно: журнал событий здесь не подключён"
    node = args.get("node")
    node = str(node).strip() or None if node else None

    since = None
    hours_raw = args.get("hours")
    if hours_raw is not None:
        try:
            hours = float(hours_raw)
        except (TypeError, ValueError):
            return f"hours должен быть числом: {hours_raw!r}"
        since = utc_now() - timedelta(hours=max(hours, 0.0))

    limit = bounded_limit(args.get("limit"), DEFAULT_SWARM_EVENTS_LIMIT, MAX_SWARM_EVENTS_LIMIT)
    if isinstance(limit, str):
        return limit

    events = await ctx.store.recent_events(node=node, since=since, limit=limit)
    if not events:
        return "в журнале ничего не нашлось за этот запрос"
    return "\n".join(f"{e['created_at']} [{e['event_type']}] {e['text']}" for e in events)


_DECL_SWARM_EVENTS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "swarm_events",
        "description": (
            "Журнал того, что уже ПРОИЗОШЛО в домашнем рое — не текущее "
            "состояние (для него swarm_status), а история: нода пропадала "
            "или возвращалась, обновлялась, служба-синглтон переезжала "
            "между нодами, плюс админские алерты (заявка на VPN-трафик, "
            "автовыключение отложено из-за открытой SSH-сессии и т.п.). "
            "Используй для вопросов вида «что случилось с X», «что было "
            "ночью», «когда mycraft последний раз пропадала». Личные "
            "события конкретных гостей (их собственные VPN-квоты) сюда не "
            "попадают — это не про них."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node": {
                    "type": "string",
                    "description": (
                        "Только события про эту ноду (например: mycraft). "
                        "Без этого — про весь рой."
                    ),
                },
                "hours": {
                    "type": "number",
                    "description": (
                        "Только события за последние N часов. Без этого — "
                        "без ограничения по времени (просто последние по limit)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Сколько последних событий вернуть "
                        f"(по умолчанию {DEFAULT_SWARM_EVENTS_LIMIT}, "
                        f"максимум {MAX_SWARM_EVENTS_LIMIT})."
                    ),
                },
            },
        },
    },
}


SWARM_STATUS = ToolSpec(
    name="swarm_status",
    handler=tool_swarm_status,
    declaration=_DECL_SWARM_STATUS,
    variants=_SWARM_VARIANTS,
)

SWARM_EVENTS = ToolSpec(
    name="swarm_events",
    handler=tool_swarm_events,
    declaration=_DECL_SWARM_EVENTS,
    requires=CommandRight(commands.NODES.name),
    telegram_only=True,
)
