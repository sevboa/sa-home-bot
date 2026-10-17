from __future__ import annotations

import json
from typing import Any

from sa_home_bot import wake_core
from sa_home_bot.bot.tools.core import (
    NO_SWARM_LINK,
    ActionRight,
    ToolContext,
    ToolSpec,
    VariantRights,
    call_service,
    text_arg,
)
from sa_home_bot.bot.tools.remind import EVENT_RESTART_APPLIED, EVENT_UPDATE_FINISHED, tool_remind
from sa_home_bot.bot.tools.swarm import own_state
from sa_home_bot.proto.addresses import (
    NODE_SERVICE,
)
from sa_home_bot.proto.messages import Address

# --- node_manage: обновить/перезапустить ноду роя ---
#
# Те же действия, что кнопки в карточке ноды (/nodes, bot/handlers/node.py) и
# nodectl check_update/update/restart_node — права ровно те же (`действие@node`),
# Альфред не умеет больше, чем сам собеседник. check_update ничего не меняет;
# update ставит файлы на диск БЕЗ рестарта процесса (node/update.py) — новая
# версия заработает только после отдельного restart_node. Само действие
# restart_node перезапускает ноду-супервизор целиком (не одну службу); если
# это своя же нода — та, где сейчас исполняется этот разговор, — Альфред сам
# ненадолго пропадёт и вернётся (POWER_DELAY_S, лайфсайкл-«снова на посту»),
# поэтому об этом стоит честно предупредить, а не молча выполнить.
#
# Сервер сам отсекает действие, которого нода не поддерживает (dev-чекаут без
# update_source, нода без колбэка restart_node) — ERR_UNKNOWN_ACTION на этапе
# _run_command (proto/server.py), до run_command, поэтому здесь не нужно
# заранее знать, что умеет конкретная нода.


NODE_ACTION_CHECK_UPDATE = "check_update"
NODE_ACTION_CHECK_ALL = "check_all"
NODE_ACTION_UPDATE = "update"
NODE_ACTION_RESTART = "restart_node"


async def _node_manage_dst(ctx: ToolContext, wanted_node: str | None) -> Address | str | None:
    """dst для command(): ``None`` — своя нода, ``Address`` — чужая, ``str`` —
    текст отказа для модели. Чужая нода сверяется со свежим списком роя (как у
    swarm_status), а не улетает наугад — иначе вместо понятной ошибки был бы
    голый таймаут на несуществующее имя."""
    if not wanted_node:
        return None
    own = await own_state(ctx)
    if own is None:
        return "недоступно: своя нода не отвечает"
    reports = await wake_core.collect_reports(ctx.node_link, own, with_monitor=False)
    known = {r.node_id for r in reports}
    if wanted_node not in known:
        known_text = ", ".join(sorted(known)) or "нет данных"
        return f"нет такой ноды: {wanted_node} (известны: {known_text})"
    return Address(node=wanted_node, service=NODE_SERVICE)


async def _auto_await_event(
    ctx: ToolContext, node: str, event_type: str, text: str, *, self_result: str
) -> str:
    """Поставить remind(after_event=...) САМОМУ, а не просить модель сделать
    это ещё одним вызовом тула. Решение пользователя 2026-08-05, живой
    инцидент: модель словами пообещала «прослежу за процессом», но сам тул
    remind не позвала — ждать её слов ненадёжно (та же природа, что у
    известной проблемы с get_time), а раз событие для продолжения и так
    известно детерминированно (сразу после update/restart_node), пусть его
    ставит код, а не просьба в тексте ответа.

    ``self_result`` — живой баг 2026-08-05 (часть 2): remind вызывается
    ИЗНУТРИ handler'а node_manage, до того как его СОБСТВЕННЫЙ результат
    попадёт в ctx.history — без self_result снимок глушил его заглушкой
    "(результат не сохранён)", и разбуженная модель не знала, что update
    реально удался, терялась и вместо restart_node звала get_time/remind
    по кругу заново (см. _close_pending_tool_calls)."""
    remind_args = {
        "after_event": {"node": node, "event": event_type},
        "text": text,
        "_self_result": self_result,
    }
    outcome = await tool_remind(ctx, remind_args)
    if outcome.startswith(("ошибка", "внутренняя ошибка")):
        return f" (не удалось поставить автоматическое ожидание для {node}: {outcome})"
    return f" Ожидание подтверждения для {node} поставлено автоматически — сообщу, когда придёт."


async def _node_check_all(ctx: ToolContext) -> str:
    """Сводка обновлений по всему рою — ядро в wake_core.check_updates_summary
    (общее с кнопкой «Проверить обновления» панели /swarm,
    bot/handlers/swarm_panel.py)."""
    if ctx.node_link is None:
        return NO_SWARM_LINK
    return await wake_core.check_updates_summary(ctx.node_link)


async def tool_node_manage(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.node_link is None:
        return NO_SWARM_LINK
    action = text_arg(args, "action")
    # Права уже проверены при сборке комплекта (tools_for), но модель может
    # передать что угодно, поэтому сверяемся ещё раз, по той же подписке.
    refusal = _NODE_MANAGE_VARIANTS.refusal_for(ctx.subscription, action)
    if refusal is not None:
        return refusal

    if action == NODE_ACTION_CHECK_ALL:
        # Не одноадресное действие — не проходит через _node_manage_dst/
        # command(action, dst=...), сама опрашивает весь рой.
        return await _node_check_all(ctx)

    wanted_node = args.get("node")
    wanted_node = str(wanted_node).strip() if wanted_node else None
    dst = await _node_manage_dst(ctx, wanted_node)
    if isinstance(dst, str):
        return dst

    where = wanted_node or "своя нода"
    result = await call_service(
        ctx, action, {}, dst, unavailable_subject=f"{where} не ответил(а)"
    )
    if isinstance(result, str):
        return result

    if action == NODE_ACTION_RESTART:
        who = wanted_node or "своя нода"
        text = f"Принято: {who} перезапустится через {result.get('delay_s', '?')} с."
        if wanted_node is None:
            text += " Это моя нода — я ненадолго пропаду из этого разговора и вернусь сам."
        else:
            # Авто-ожидание restart_applied — та же причина, что у update
            # ниже: не полагаться на то, что модель сама позовёт remind.
            #
            # Живой баг 2026-08-05 (третий заход): раньше директива добавляла
            # "и продолжи задачу обновления роя (следующая нода, если она
            # есть)" — при обновлении НЕСКОЛЬКИХ нод разом (update() уже
            # вызывается на всех сразу в одном исходном ходе, см. ветку
            # NODE_ACTION_UPDATE ниже) у каждой ноды заводился СВОЙ
            # self-scheduled remind на restart_applied, и КАЖДЫЙ из них
            # заново пытался "перейти к следующей ноде" — несколько
            # независимых цепочек лезли проверять/перезапускать одни и те же
            # чужие ноды одновременно. Живая гонка: цепочка jeeves застала
            # mycraft, которую в этот момент перезапускала её же СОБСТВЕННАЯ
            # цепочка — поймала "соединение закрыто" и это осело в финальном
            # тексте ("похоже, нода просто не отвечает"), хотя сама mycraft
            # уже через несколько секунд подтвердила v0.76.2 — модель просто
            # не сверилась с более поздним своим же результатом. Теперь
            # директива только про СВОЮ ноду — переход к следующей не нужен:
            # update() на все нуждающиеся ноды уже поставлен в исходном ходе,
            # каждая доводит себя до конца сама по своей же цепочке
            # update_finished -> restart_node -> restart_applied.
            text += await _auto_await_event(
                ctx,
                wanted_node,
                EVENT_RESTART_APPLIED,
                f"подтверди, что {wanted_node} поднялась на новой версии, и доложи "
                "об этом — на другие ноды переходить не нужно, у каждой уже своя "
                "цепочка ожидания",
                self_result=text,
            )
        return text
    if action == NODE_ACTION_UPDATE:
        if result.get("up_to_date"):
            version = result.get("version", "?")
            if result.get("restart_required"):
                # Файлы на диске уже v{version}, но исполняется всё ещё
                # старая версия — раньше здесь честно не проверялось (баг
                # 2026-08-04), и "готово" звучало так, будто нода уже
                # работает на новой версии, хотя это не так.
                return (
                    f"Файлы уже последней версии v{version} лежат на диске, но нода "
                    "ЕЩЁ ИСПОЛНЯЕТ старый код — update не нужен, но нужен ещё "
                    "action=«restart_node», иначе новая версия не заработает."
                )
            return f"Уже последняя версия v{version}, нода её и исполняет — делать нечего."
        # update — фоновая операция (node/service.py::_schedule_update): этот
        # ответ приходит МГНОВЕННО, а файлы на диск лягут только спустя
        # какое-то время. Живой баг 2026-08-04 (часть 2): без явного запрета
        # модель звала restart_node в ТОМ ЖЕ ходе, не дожидаясь реального
        # завершения — нода перезапускалась и поднималась на СТАРОМ коде,
        # который update ещё не успел заменить. remind(after_event=
        # update_finished) — тот же механизм, что уже развязал ожидание
        # restart_node/restart_applied, просто на шаг раньше в цепочке.
        target_node = wanted_node
        if target_node is None:
            own = await own_state(ctx)
            target_node = (own or {}).get("node")
        target_version = result.get("target_version", "?")
        note = (
            f"Обновление до v{target_version} поставлено на диск В ФОНЕ (сам процесс "
            "не тронут, установка идёт асинхронно) — файлы ещё не готовы. НЕ вызывай "
            "restart_node прямо сейчас, иначе нода перезапустится на СТАРОМ коде."
        )
        if target_node:
            note += await _auto_await_event(
                ctx,
                target_node,
                EVENT_UPDATE_FINISHED,
                f'вызови node_manage(action="restart_node", node="{target_node}") и '
                "продолжи задачу обновления роя",
                self_result=note,
            )
        else:
            note += " Не удалось определить имя ноды для авто-ожидания, дождись подтверждения сам."
        return note
    return json.dumps({"node": wanted_node or "своя", **result}, ensure_ascii=False)


_NODE_MANAGE_VARIANTS = VariantRights(
    param="action",
    rights=(
        # Право на действие — то же `действие@node`, что и у кнопки в карточке
        # ноды: Альфред не расширяет доступ. check_all — то же самое право,
        # что check_update (то же самое read-only "видеть, есть ли новое"),
        # просто сразу по всему рою, а не по одной ноде за раз.
        (NODE_ACTION_CHECK_UPDATE, ActionRight(NODE_ACTION_CHECK_UPDATE, NODE_SERVICE)),
        (NODE_ACTION_CHECK_ALL, ActionRight(NODE_ACTION_CHECK_UPDATE, NODE_SERVICE)),
        (NODE_ACTION_UPDATE, ActionRight(NODE_ACTION_UPDATE, NODE_SERVICE)),
        (NODE_ACTION_RESTART, ActionRight(NODE_ACTION_RESTART, NODE_SERVICE)),
    ),
)

_DECL_NODE_MANAGE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "node_manage",
        "description": (
            "Обновить или перезапустить ноду домашнего роя — те же действия, "
            "что кнопки в /nodes. check_update — посмотреть, есть ли новая "
            "версия у ОДНОЙ ноды (своей или указанной в node), ничего не "
            "меняя; check_all — то же самое, но сразу по всему рою: "
            "последняя доступная версия и список нод, которым нужен update "
            "или хотя бы restart_node (node здесь не нужен и игнорируется); "
            "update — поставить новую версию на диск БЕЗ перезапуска "
            "процесса (это ФОНОВАЯ операция — ответ приходит мгновенно, а "
            "файлы дописываются ещё какое-то время); restart_node — "
            "перезапустить саму ноду (не отдельную службу) — после update "
            "это обязательно, иначе новый код не заработает, но звать его "
            "СРАЗУ после update — тоже ошибка: файлы могут быть ещё не "
            "готовы, и нода перезапустится на старом коде. Про это НЕ НУЖНО "
            "заботиться самому: и update, и restart_node САМИ ставят "
            "ожидание подтверждения (об этом скажет текст ответа) — не "
            "зови remind ради этого сам, не проверяй/жди руками, просто "
            "сообщи пользователю, что запущено, и (если обновляешь "
            "несколько нод) сразу переходи к update следующей — ждать, "
            "пока одна полностью закончит цикл, не нужно. Без node (для "
            "check_update/update/restart_node) "
            "действие идёт на "
            "ту ноду, где сейчас исполняюсь я сам — restart_node на ней "
            "ненадолго оборвёт этот же разговор."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    # enum подставляется под права собеседника (см. tools_for).
                    "enum": [v for v, _ in _NODE_MANAGE_VARIANTS.rights],
                    "description": (
                        "check_update — есть ли обновление у одной ноды; "
                        "check_all — сводка по всему рою сразу; update — "
                        "поставить его на диск; restart_node — перезапустить "
                        "ноду"
                    ),
                },
                "node": {
                    "type": "string",
                    "description": (
                        "Имя конкретной ноды (например: alfred, mycraft). Без "
                        "этого — своя нода. Игнорируется при action=check_all."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}


NODE_MANAGE = ToolSpec(
    name="node_manage",
    handler=tool_node_manage,
    declaration=_DECL_NODE_MANAGE,
    variants=_NODE_MANAGE_VARIANTS,
)
