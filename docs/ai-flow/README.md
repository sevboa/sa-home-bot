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
