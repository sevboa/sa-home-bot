# Зона ai-flow: диалог /ai (Альфред)

Знания, которые раньше жили в комментариях и докстрингах модулей `bot/ai_flow.py`,
`bot/handlers/ai.py`, `bot/dialogue_context.py`, `bot/rich_stream.py`, `llm_chat.py`,
`bot/voice_*.py`, `bot/pending_actions.py`, `bot/tool_debug.py`, `memory/*`,
`graph_memory/*`, `people/*`. Темы, а не файлы.

- [turn-flow.md](turn-flow.md) — путь одного хода: хендлеры, топики, presence/wake, router и think, роспуск, сбои в образе.
- [context-note.md](context-note.md) — скрытая заметка перед ходом: время, люди, память, ответ на картинку, длина ответа.
- [context-window.md](context-window.md) — Этап 50: окно контекста, сжатие истории, дословный возврат, цикл тулов.
- [rich-stream.md](rich-stream.md) — Rich-ответы и щадящий стрим черновиков (Этап 34).
- [voice.md](voice.md) — голосовые сообщения: STT, синтез ответа, голосовой режим.
- [pending-actions.md](pending-actions.md) — формы подтверждения (Этап 45/46) и дебаг-канал тулов.
- [memory-services.md](memory-services.md) — службы memory и graph_memory, карточки людей (Этапы 41, 42, 54, 58).

## Карта модулей

- `bot/ai_flow.py` — `request_alfred` (тонкая обёртка над `_AlfredTurn`: подготовка заметки, план окна контекста, ожидание готовности службы, router/persona-проходы, страховка `_rescue`), `ActiveAiChats`, роспуск, эпизод диалога.
- `bot/ai_context.py` — заметка перед ходом, память, карточки людей, `display_name`, `ALFRED_TIMEZONE`.
- `bot/ai_lines.py` — реплики Альфреда и персонажей сбоев (HTML/MD/plain-пары), статусы тулов.
- `bot/ai_turns.py` — запись хода пользователя и сборка истории для текста/фото/стикера/голоса, служебные директивы.
- `bot/ai_reply.py` — доставка ответа (текст, rich, голос, селфи), хвост хода, `TurnServices`.
- `bot/handlers/ai.py` — фильтры и обработчики aiogram; `_ask_and_reply` — тонкая обёртка над `ai_reply.ask_and_reply` для обработчиков (`bot/away_return.py` вызывает `ai_reply.ask_and_reply` напрямую).
- `bot/dialogue_context.py`, `llm_chat.py`, `bot/rich_stream.py`, `bot/voice_*.py`, `bot/pending_actions.py`, `bot/tool_debug.py`, `bot/people_cards.py` — как в темах выше.
- `bot/alfred_voice.py` — префиксы «Альфред:» (HTML и markdown), `bot/service_errors.py` — кортеж `UNREACHABLE_ERRORS`.
- `memory/`, `graph_memory/` (+ `client.py::add_episode`), `people/`, `text_search.py` (слова и FTS-запрос), `chat_scope.py` (`chat_id` из аргументов службы).
