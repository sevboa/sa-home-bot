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

# meta.dialogue_id может отсутствовать у chat_loop-задачи. Обычно (self-
# scheduled remind, bot/tools.py::tool_remind) он есть всегда — задача
# продолжает СВОЙ тред. Его отсутствие — сигнал другого сценария:
# проактивное первое сообщение ЧУЖОМУ собеседнику, которого сейчас ни о чём
# не спрашивали (bot/tools.py::schedule_agent_dialogue, Этап 44.1, задел под
# агента установки связи между гостями, 42.6). Рождение нового dialogue_id
# на срабатывании — забота получателя события task_result, не этой службы
# (bot/node_events.py::_handle_task_result, Этап 44.2): служба tasks meta не
# читает и не интерпретирует (см. докстринг tasks/service.py), только хранит
# и возвращает целиком.

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

# deliver_message: {chat_id, html: str, plain: str, message_thread_id?} —
# служба tasks не умеет говорить с Telegram напрямую (см. докстринг модуля
# tasks/service.py), но тулы tell/notify_guest, вызванные ВНУТРИ сработавшей
# chat_loop-задачи (self-scheduled remind — живой запрос пользователя
# 2026-08-06: "напомни мне как граф, что пора спать" реально означает
# remind, а не живой /ai), должны уметь отправить готовое сообщение. Бот —
# единственный, у кого есть настоящий Notifier — берёт это на себя (см.
# bot/node_events.py::_handle_deliver_message), как уже делает для
# task_result. ``html`` — то, что реально уходит в Telegram (уже
# отрендировано, с разметкой), ``plain`` — сырой текст без обёртки, для
# записи в ai_turns получателя (та же пара, что render(target)/text у
# bot/tools.py::_deliver_personal_message в живом /ai). Доставка отсюда —
# fire-and-forget: служба tasks не ждёт подтверждения (round-trip запрос-
# ответ в протокол не заведён, тот же компромисс, что и у task_result),
# поэтому тул отвечает моделью "передано" оптимистично, не дожидаясь
# реального ухода сообщения.
EVENT_DELIVER_MESSAGE = "deliver_message"

# respond_relationship: {relationship_id, accepted: bool, responder_chat_id} —
# тот же мост, что EVENT_DELIVER_MESSAGE, но для записи guest_relationships
# (Этап 42.6.2), а не отправки сообщения. confirm_relationship/
# reject_relationship, вызванные ВНУТРИ проактивной сессии агента установки
# связи (schedule_agent_dialogue — Этап 44, служба tasks), не имеют доступа
# ни к Store бота (guest_relationships там же, где ai_turns — не в БД tasks),
# ни к Notifier — прочитать/записать статус и уведомить инициатора умеет
# только бот, см. bot/node_events.py::_handle_respond_relationship.
# responder_chat_id — серверный (ctx.chat_id этой сессии, не аргумент модели)
# — бот-сторона всё равно сверяет его с guest_b записи перед записью, но
# доверие к нему уже установлено тем, что сессию создал schedule_agent_dialogue
# именно под этого гостя (см. bot/tools.py::_respond_relationship).
EVENT_RESPOND_RELATIONSHIP = "respond_relationship"
