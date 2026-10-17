from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.tools.core import ToolContext, ToolSpec
from sa_home_bot.config import reminder_reason
from sa_home_bot.llm.prompt import wrap_system_directive
from sa_home_bot.proto.addresses import (
    LLM_NODE,
    LLM_SERVICE,
    TASKS_ADDRESS,
)
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.tasks import protocol as task_protocol
from sa_home_bot.utils.clock import utc_now

# Литералы, не импорт из node/service.py (та же причина, что у
# wake_core.py::_CHECK_UPDATE_ACTION — тот модуль тяжёлый, тянет
# супервизор/пиров/discovery, а тулу remind нужна только пара строк-имён
# событий для after_event).
EVENT_UPDATE_FINISHED = "update_finished"
EVENT_RESTART_APPLIED = "restart_applied"

# Страховочный дедлайн для remind(after_event=...), если событие так и не
# придёт (нода не поднялась) — без него задача ждала бы вечно, что хуже
# честного «не подтвердилось» (тот же принцип, что у FIRE_GRACE_S в
# tasks/service.py: лучше сработать с опозданием/сдаться, чем не
# сработать никогда). Модель это число не указывает — иначе легко
# промахнётся: сама нода не знает, сколько реально займёт её рестарт.
RESTART_EVENT_FALLBACK_S = 300.0


_AFTER_EVENT_TYPES = (EVENT_RESTART_APPLIED, EVENT_UPDATE_FINISHED)


def _close_pending_tool_calls(
    history: list[dict[str, Any]], self_result: str | None = None
) -> list[dict[str, Any]]:
    """Снимок ``ctx.history`` для remind делается ИЗ СЕРЕДИНЫ раунда
    tool-calling (llm_chat.run_chat_loop::122-148 вызывает тулы строго
    ПОСЛЕ того, как допишет в history единственное сообщение "assistant"
    с tool_calls за весь раунд, и допишет ответное "tool" на каждый вызов
    только ПОСЛЕ того, как его handler вернётся) — то есть в момент, когда
    remind() строит снимок, вызов remind (и любые другие вызовы того же
    раунда, идущие после него) там висят без ответного "tool"-сообщения.
    Такая история при повторном проигрывании (служба tasks) кладёт перед
    моделью assistant/tool_calls без ответа, за которым сразу новый user —
    невалидная форма для Ollama (живой сбой 2026-08-04: HTTP 400 на /api/
    chat, разобрано только благодаря телу ответа, см. llm/ollama.py).
    Дозаполняем недостающие "tool"-ответы, не трогая сам живой ``history``
    (та же мутируемая ссылка нужна остальным вызовам этого раунда).

    ``self_result`` — живой баг 2026-08-05: node_manage зовёт remind
    (after_event) ИЗНУТРИ СВОЕГО ЖЕ handler'а, до того как его собственный
    результат попадёт в history — снимок раньше глушил его синтетической
    заглушкой "(результат не сохранён)", и разбуженная модель не знала, что
    её же update реально удался, терялась и вместо restart_node звала
    get_time/remind по кругу. Первый висящий вызов в раунде — это ВСЕГДА
    сам текущий (см. порядок обработки tool_calls в run_chat_loop), поэтому
    именно ему подставляется настоящий результат, если он передан;
    остальные (если раунд был из нескольких вызовов) — по-прежнему
    заглушкой, их результат правда ещё не известен."""
    for i in range(len(history) - 1, -1, -1):
        msg = history[i]
        if msg.get("role") == "tool":
            continue
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            return history
        pending = msg["tool_calls"][len(history) - i - 1 :]
        snapshot = list(history)
        for idx, call in enumerate(pending):
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            content = "(результат не сохранён)"
            if idx == 0 and self_result is not None:
                content = self_result
            snapshot.append({"role": "tool", "content": content, "name": fn.get("name", "")})
        return snapshot
    return history


async def tool_remind(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.chat_id is None or ctx.dialogue_id is None or ctx.trigger_message_id is None:
        return "ошибка: отложенные задачи недоступны вне диалога"
    if ctx.node_link is None:
        return "ошибка: служба задач недоступна"
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return "ошибка: не указано, что сделать/сказать (text)"

    after_event = args.get("after_event")
    event_node: str | None = None
    event_type: str | None = None
    if after_event is not None:
        if not isinstance(after_event, dict):
            return "ошибка: after_event должен быть объектом {node, event}"
        event_node = str(after_event.get("node") or "").strip()
        event_type = str(after_event.get("event") or "").strip()
        if not event_node or event_type not in _AFTER_EVENT_TYPES:
            return (
                "ошибка: after_event.node обязателен, after_event.event — одно из "
                + ", ".join(_AFTER_EVENT_TYPES)
            )
        # Живой инцидент 2026-08-05: словесный запрет в директиве ("не зови
        # remind на то же событие снова") модель систематически игнорирует —
        # разбуженная по (node, event_type) заново ставит remind на ТО ЖЕ
        # самое (node, event_type), бесконечно откладывая реальное действие.
        # Жёсткий отказ вместо тихого согласия: разбуженный ход обязан либо
        # выполнить порученное, либо честно сказать, что не вышло — не
        # переносить решение на потом, когда "потом" уже наступило.
        if ctx.woken_by == (event_node, event_type):
            return (
                f"ошибка: событие «{event_type}» от «{event_node}» уже наступило — "
                "именно из-за него тебя сейчас разбудили. Ждать его снова нельзя "
                "(зациклишься) — вызови действие, которое тебя просили сделать, "
                "или честно сообщи, что не получилось."
            )

    when_raw = args.get("when")
    if when_raw is None and after_event is None:
        return "ошибка: не указано время (when, ISO 8601)"
    if when_raw is not None:
        if not isinstance(when_raw, str) or not when_raw.strip():
            return "ошибка: не указано время (when, ISO 8601)"
        try:
            due_at = datetime.fromisoformat(when_raw)
        except ValueError:
            return "ошибка: 'when' должен быть в формате ISO 8601, например 2026-07-24T21:30:00"
        # Наивную дату-время (без смещения) считаем локальным временем процесса —
        # именно в нём отдана строка "текущее время" в контексте промпта
        # (bot/ai_flow.py::_build_context_note), так что модель обычно отвечает
        # тем же способом, без явного смещения.
        if due_at.tzinfo is None:
            due_at = due_at.astimezone()
        due_at_utc = due_at.astimezone(UTC)
        if due_at_utc <= utc_now():
            return "ошибка: указанное время уже прошло"
    else:
        # after_event без when: дедлайн — страховка на случай, если событие
        # не придёт вовсе, а не то, что модель должна угадывать (см.
        # RESTART_EVENT_FALLBACK_S).
        due_at_utc = utc_now() + timedelta(seconds=RESTART_EVENT_FALLBACK_S)
        due_at = due_at_utc.astimezone()

    # Директива дописывается в снимок ТЕКУЩЕЙ истории (ctx.history — то, что
    # модель видит прямо сейчас, см. докстринг ToolContext) — служба tasks
    # прогоняет ровно этот список через llm.chat заново в момент срабатывания,
    # без доступа к ai_turns бота (решение пользователя 2026-07-24: снимок
    # делается здесь, при создании задачи, а не реконструируется позже).
    if after_event is not None:
        # Срабатывание могло прийти и по событию (раньше due_at, см.
        # bot/node_events.py::_maybe_fire_event_waiter), и по таймауту —
        # отсюда модель не знает, что именно случилось, и должна честно
        # сверить состояние сама (check_update/node_manage), а не считать
        # успех гарантированным. Живой баг 2026-08-05: конкретное время
        # дедлайна в тексте (было f"{due_at:%H:%M}") маленькая модель
        # (Gemma на mycraft) читала как приглашение самой сверить часы —
        # звала get_time по кругу, выжигала весь бюджет раундов и на
        # принудительном "дожатии" без тулов выдавала обрывок СВОЕГО
        # внутреннего формата вызова тула как обычный текст. Решение
        # пользователя: время из директивы убрать вовсе — само решение
        # "сработало по событию или по таймауту" уже принято системой ДО
        # пробуждения модели, ей нечего в нём проверять.
        directive = (
            f"Ты ждал(а) событие «{event_type}» от ноды «{event_node}», после "
            f"чего тебя попросили сделать вот что: «{text.strip()}». Прежде чем "
            "продолжать, сверь реальное состояние (например, node_manage/"
            "check_update) — событие могло не прийти вовсе, тогда честно скажи, "
            "что не подтвердилось, не выдавай желаемое за случившееся. Просто "
            "вызови нужный тул — время сейчас проверять не нужно. НЕ зови "
            f"remind(after_event={{\"node\": \"{event_node}\", \"event\": "
            f'"{event_type}"}}) снова — это то самое событие, по которому тебя '
            "только что разбудили, ставить его ожидание заново — зациклиться."
        )
    else:
        # "Настало время" привязано к due_at, а не к моменту создания задачи —
        # due_at и есть момент фактического срабатывания (с точностью до
        # интервала опроса службы tasks).
        directive = (
            f"Настало время ({due_at:%Y-%m-%d %H:%M}), на которое тебя раньше "
            f"попросили сделать вот что: «{text.strip()}». Сделай/скажи это "
            "сейчас, от своего имени, в характере — как будто сам вспомнил, а не "
            "отвечаешь на прямой вопрос. Если нужно что-то посчитать или узнать "
            "(погоду, курс) — пользуйся инструментами, не полагайся на память."
        )
    # "tools" здесь не кладём: комплект собирается заново в момент
    # срабатывания, по правам собеседника на ТОТ момент (llm_chat.run_chat_loop
    # → tools_for). Раньше сюда клался снимок TOOL_DECLARATIONS, но его никто
    # не читал — цикл всегда подставлял свой список, а снимок лишь раздувал
    # args_json задачи.
    # Ключ только для внутреннего вызова из _auto_await_event (node_manage
    # зовёт remind изнутри своего же handler'а) — не часть публичной схемы
    # тула, модель его никогда не передаёт.
    history = _close_pending_tool_calls(ctx.history, self_result=args.get("_self_result"))
    # Уровень рассуждения на СРАБАТЫВАНИИ напоминания: router-прохода в тот
    # момент нет (вопрос был задан когда-то раньше), берём фиксированную
    # политику из legacy-полей — config.reminder_reason. Перевод уровня в
    # конкретный параметр Ollama делает профиль модели на стороне службы llm
    # (llm/model_profiles.py) — здесь про механизм знать не нужно, поэтому и
    # ушли прежние баги с явным think=true → 400 на gemma (2026-08-05/08-10).
    task_args = {
        "messages": [*history, wrap_system_directive(directive)],
        "reason": reminder_reason(ctx.settings.llm),
        "chat_id": ctx.chat_id,
    }
    meta = {
        "kind": task_protocol.TASK_KIND_LLM_CHAT,
        "chat_id": ctx.chat_id,
        "dialogue_id": ctx.dialogue_id,
        "trigger_message_id": ctx.trigger_message_id,
        # Живой баг 2026-08-05: без этого ответ tasks (bot/node_events.py::
        # _handle_task_result/_handle_task_prewake) уезжал в общий топик
        # личного чата, а не туда, где реально шла переписка (см. докстринг
        # Notifier.send_direct про message_thread_id).
        "message_thread_id": ctx.message_thread_id,
    }
    if after_event is not None:
        # Восстанавливается в ToolContext.woken_by на срабатывании
        # (tasks/service.py::_fire_chat_loop) — см. проверку выше про
        # запрет повторного remind на то же (node, event_type).
        meta["awaited_node"] = event_node
        meta["awaited_event"] = event_type
    dst = TASKS_ADDRESS
    create_args: dict[str, Any] = {
        "due_at": due_at_utc.isoformat(),
        "dst_node": LLM_NODE,
        "dst_service": LLM_SERVICE,
        "action": task_protocol.ACTION_CHAT_LOOP,
        "args": task_args,
        "timeout_s": ctx.settings.llm.request_timeout_s,
        "meta": meta,
    }
    if after_event is not None:
        # Ожидание живёт в самой службе tasks (не в БД бота — решение
        # пользователя 2026-08-05, живой баг: remind, вызванный ИЗ УЖЕ
        # СРАБОТАВШЕГО хода (тот код исполняется внутри tasks), не имел
        # доступа к ctx.store вовсе — именно там чаще всего и нужно
        # ставить следующее ожидание при обновлении нескольких нод по
        # цепочке). См. tasks/protocol.py::ACTION_MATCH_EVENT.
        create_args["await_event"] = {"node": event_node, "event_type": event_type}
    try:
        await ctx.node_link.command(task_protocol.ACTION_CREATE, create_args, dst=dst)
    except (ServiceUnavailableError, ProtoError) as exc:
        return f"внутренняя ошибка: не удалось поставить задачу ({exc})"

    if after_event is not None:
        return (
            f"жду событие «{event_type}» от «{event_node}», страховочный срок — "
            f"{due_at.strftime('%H:%M')} (местное время)"
        )
    return f"задача поставлена на {due_at.strftime('%Y-%m-%d %H:%M')} (местное время)"


async def schedule_agent_dialogue(
    node_link: ServiceLink,
    chat_id: int,
    messages: list[dict[str, Any]],
    reason: str,
    timeout_s: float,
    *,
    meta_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Поставить chat_loop-задачу, которая на срабатывании начинает НОВЫЙ
    тред у произвольного собеседника — не продолжает свой, как tool_remind
    (тот всегда self-scheduled: несёт ctx.dialogue_id/trigger_message_id/
    message_thread_id, смысл — "напомни МНЕ в ЭТОМ треде"). Здесь meta
    заведомо не содержит dialogue_id/trigger_message_id/message_thread_id —
    рождение нового dialogue_id при доставке первого сообщения делает
    bot/node_events.py::_handle_task_result (Этап 44.2, отдельный подэтап).

    НЕ публичный ToolSpec — модель в живом /ai её вызвать не может, это
    внутренняя инфраструктура: речь Альфреда перед формой подтверждения
    адресату и перед оповещением инициатору об итоге (Этап 45, bot/
    pending_actions.py — ``meta_extra`` несёт pending_action_id/stage).

    due_at = сейчас, не await_event: триггер уже наступил на стороне
    вызывающего кода (например, гость A подтвердил предложение связи) —
    ждать больше нечего, обычный fire-loop (tasks/service.py::_fire_due)
    подхватит задачу на ближайшем тике. Ошибки (ServiceUnavailableError/
    ProtoError) не проглатываются, как у tool_remind (там — русский текст
    для модели) — это не тул, а функция для кода, вызывающая сторона решает
    сама, как сообщить о сбое."""
    due_at_utc = utc_now()
    dst = TASKS_ADDRESS
    create_args: dict[str, Any] = {
        "due_at": due_at_utc.isoformat(),
        "dst_node": LLM_NODE,
        "dst_service": LLM_SERVICE,
        "action": task_protocol.ACTION_CHAT_LOOP,
        "args": {"messages": messages, "reason": reason, "chat_id": chat_id},
        "timeout_s": timeout_s,
        "meta": {
            "kind": task_protocol.TASK_KIND_LLM_CHAT,
            "chat_id": chat_id,
            # Этап 45: pending_action_id/pending_action_stage — речь,
            # после которой бот обязан прислать форму/оповещение (см.
            # bot/node_events.py::_handle_task_result).
            **(meta_extra or {}),
        },
    }
    return await node_link.command(task_protocol.ACTION_CREATE, create_args, dst=dst)


_DECL_REMIND: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "remind",
        "description": (
            "Поставить отложенную задачу в этом же чате — НЕ готовый текст, а "
            "то, что нужно СДЕЛАТЬ или СКАЗАТЬ, когда придёт время: тогда тебя "
            "вызовут заново и ты сам сформулируешь ответ, при необходимости "
            "пользуясь другими инструментами (например, посмотреть погоду "
            "именно в тот момент, а не сейчас). Ровно ОДНО из двух — when ИЛИ "
            "after_event. when — переведи то, что попросил пользователь "
            "('через 20 минут', 'завтра в 9 утра'), в точную дату-время сам, "
            "используя текущее время из контекста разговора. after_event — "
            "проснуться по событию ноды, а не по времени: НЕ сиди и не "
            "жди/не переспрашивай состояние сам, а поставь задачу "
            "проснуться, когда нода реально подтвердит нужное (страховочный "
            "срок на случай, если событие не придёт, считается сам, без "
            "when). Для update/restart_node ноды (node_manage) это НЕ "
            "нужно — они уже сами ставят такое ожидание, вызывай remind "
            "только для СВОИХ похожих случаев ожидания события."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "when": {
                    "type": "string",
                    "description": "Точная дата-время в ISO 8601, например 2026-07-24T21:30:00",
                },
                "after_event": {
                    "type": "object",
                    "description": (
                        "Проснуться по событию ноды, а не по времени — для "
                        "node_manage(restart_node)/update, когда нужно дождаться "
                        "реального результата, не выдумывая его заранее."
                    ),
                    "properties": {
                        "node": {
                            "type": "string",
                            "description": "Имя ноды, чьего события ждать (например: arch-t480)",
                        },
                        "event": {
                            "type": "string",
                            "enum": list(_AFTER_EVENT_TYPES),
                            "description": (
                                "restart_applied — нода реально перезапустилась на новой "
                                "версии (после restart_node); update_finished — файлы "
                                "обновления легли на диск (после update, ДО рестарта)."
                            ),
                        },
                    },
                    "required": ["node", "event"],
                },
                "text": {
                    "type": "string",
                    "description": "Что нужно сделать или сказать в момент срабатывания",
                },
            },
            "required": ["text"],
        },
    },
}


REMIND = ToolSpec(
    name="remind", handler=tool_remind, declaration=_DECL_REMIND, telegram_only=True
)
