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

# Разбудить задачу РАНЬШЕ due_at — по приходу события роя, а не по
# будильнику (remind after_event, bot/tools.py; матчинг события — на
# стороне бота, bot/node_events.py, у него одного есть доступ ко всем
# событиям роя). due_at у задачи остаётся страховкой: если fire_now не
# пришёл вовсе (событие не случилось), задача всё равно сработает по
# таймауту обычным путём (tasks/service.py::_fire_due). Решение
# пользователя 2026-08-04.
ACTION_FIRE_NOW = "fire_now"

# task_prewake: {task_id, meta, status: "waking"|"ready"|"failed", reason?}
# — прогресс попытки разбудить dst заранее (см. tasks/service.py).
EVENT_TASK_PREWAKE = "task_prewake"
# task_result: {task_id, meta, ok: bool, result?: dict, error?: str} —
# итог исполнения задачи в момент due_at.
EVENT_TASK_RESULT = "task_result"

# tool_call: {name: str} — факт вызова инструмента моделью ВНУТРИ
# сработавшей chat_loop-задачи (self-scheduled remind). Только имя, без
# аргументов/результата — используется bot/node_events.py для дебаг-
# уведомлений (см. bot/lifecycle.py::notify_tool_call), не для доставки
# ответа пользователю.
EVENT_TOOL_CALL = "tool_call"

# meta.kind — единственный сейчас распознаваемый потребителями (bot/
# node_events.py) вид задачи: результат/неудачу нужно доставить в Telegram
# как ответ Альфреда, продолжающий диалог meta.dialogue_id.
TASK_KIND_LLM_CHAT = "llm_chat"
