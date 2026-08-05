"""Константы протокола службы tasks — намеренно без единого импорта внутри
пакета проекта (только строковые литералы).

Живут отдельно от tasks/service.py, чтобы bot/tools.py (создаёт задачи
через тул remind) мог их импортировать, не утягивая саму службу (у той уже
есть свой цикл tool-calling, а значит и bot.tools — импорт service.py из
tools.py и обратно был бы циклом).
"""

from __future__ import annotations

SERVICE_NAME = "tasks"

# Нода, на которой развёрнута служба tasks — тот же приём, что LLM_NODE в
# bot/ai_flow.py и bot/tools.py (известный, фиксированный узел роя, не
# динамическое обнаружение). Должно совпадать с [node].id/hostname той
# ноды, где "tasks" присутствует в [node].assignments.
NODE_ID = "alfred"

ACTION_CREATE = "create"
# Спец-действие: не форвардится как обычная протокольная команда — служба
# tasks сама прогоняет messages/tools/think через полный цикл tool-calling
# поверх llm.chat (sa_home_bot.llm_chat.run_chat_loop). Единственный сейчас
# существующий "богатый" тип задачи.
ACTION_CHAT_LOOP = "chat_loop"

# Разбудить задачу РАНЬШЕ due_at — по её id, а не по будильнику. Внутренний
# примитив: используется match_event (ниже), а не вызывается напрямую
# извне — оставлен отдельным действием, потому что match_event поверх него
# просто lookup+вызов.
ACTION_FIRE_NOW = "fire_now"

# Ждать событие роя (node, event_type), а не время — remind(after_event=...)
# в bot/tools.py; сама задача создаётся обычным ACTION_CREATE с полем
# await_event (см. _create). Матчинг живёт ЗДЕСЬ, а не в bot/node_events.py
# (решение пользователя 2026-08-05, живой баг): у бота нет доступа к
# ожиданию, если задача-продолжение сама вызывает node_manage/remind ИЗ
# УЖЕ СРАБОТАВШЕГО хода (тот код исполняется внутри службы tasks — своей
# БД бота там нет, см. bot/tools.py::ToolContext) — а именно оттуда чаще
# всего и нужно поставить следующее ожидание (обновил одну ноду →
# перезапустил → жду подтверждения → перехожу к следующей). due_at у
# задачи остаётся страховкой: если событие не пришло, задача всё равно
# сработает по таймауту обычным путём (tasks/service.py::_fire_due).
ACTION_MATCH_EVENT = "match_event"

# task_prewake: {task_id, meta, status: "waking"|"ready"|"failed", reason?}
# — прогресс попытки разбудить dst заранее (см. tasks/service.py).
EVENT_TASK_PREWAKE = "task_prewake"
# task_result: {task_id, meta, ok: bool, result?: dict, error?: str} —
# итог исполнения задачи в момент due_at.
EVENT_TASK_RESULT = "task_result"

# tool_call: {name: str, args: dict, result: str} — факт вызова инструмента
# моделью ВНУТРИ сработавшей chat_loop-задачи (self-scheduled remind).
# Используется bot/node_events.py для дебаг-уведомлений (см. bot/
# lifecycle.py::notify_tool_call — args/result дают кнопку «развернуть», как
# и у живого /ai), не для доставки ответа пользователю. Живой баг 2026-08-05:
# раньше уходило только имя — кнопке «развернуть» было нечего показывать,
# разбор self-scheduled remind сводился к чтению логов процесса tasks.
EVENT_TOOL_CALL = "tool_call"

# meta.kind — единственный сейчас распознаваемый потребителями (bot/
# node_events.py) вид задачи: результат/неудачу нужно доставить в Telegram
# как ответ Альфреда, продолжающий диалог meta.dialogue_id.
TASK_KIND_LLM_CHAT = "llm_chat"
