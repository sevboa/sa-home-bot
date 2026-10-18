# Пакет agent/ (Этап 42.4, эксперимент)

Решение владельца по судьбе пакета не получено: он не подключён к живому /ai и не входит в `[project.scripts]`. Код оставлен, упрощён и документирован здесь.

## MCP-сервер умений роя (`agent/mcp_tools.py`)
Вторая попытка: первая (HTTP + bearer-токены на Telegram-подписку на alfred) была неправильной формой и откачена (IMPLEMENTATION_PLAN.md, план `vast-skipping-crane.md`). Реальный клиент — не человек с внешним MCP-клиентом, а инференс-стек агента (Phase 2: Pydantic AI + Ollama) на mycraft, рядом с Ollama и отдельно от бота на alfred. `bot.tools` переиспользуем без aiogram (его уже использует служба `tasks`, `ToolContext.emit`): любой процесс со своим `node_link` (`ServiceLink` к `settings.node.socket`, как `tasks/app.py::run_tasks`) достаёт `Address(node, service)` до любой службы роя. mycraft несёт `memory`/`graph_memory`/`net` (Этап 42.1) и свой `node.socket` — сетевой мост на alfred для большинства тулов не нужен.

- Транспорт — MCP `stdio`: агент и процесс на одной машине, доверие — сам факт, что один процесс породил другой; сети и токенов нет (в HTTP-версии обязателен был bearer `swarm.token`). stdout зарезервирован под MCP JSON-RPC, логи идут в stderr (`configure_logging`) — смешение сломало бы протокол.
- Права — фиксированная `Subscription` агента: обычная владельческая подписка `[[subscriptions]]` с `chat_id = AGENT_SENTINEL_CHAT_ID` (`-1`), какие права дать, решает правка `config.toml` + рестарт (пример в `config.example.toml`). Подписка резолвится один раз при старте. Нет подписки — `tools_for(None)` даёт fail-closed набор. Значение `-1` выбрано намеренно: `0` занят другим сентинелом (`vpn/service.py::NODE_SENTINEL_CHAT_ID` — «весь канал ноды» в квотах; `node/fixups.py::VPN_PROBE_CHAT_ID` — пробник `vpn_check`), реальный Telegram `chat_id` равным `-1` не бывает.
- `TELEGRAM_ONLY_TOOLS` (вычисляется из `ToolSpec.telegram_only`) исключается из `tools/list` и проверяется в `tools/call` (клиент мог запомнить имя из старого списка либо выдумать). Причины по тулам:
  - `dismiss` мутирует `ctx.dismissal` и исполняется после ответа хендлером `bot/handlers/ai.py` — без живого Telegram-хода исполнять нечего;
  - `remind` требует `chat_id`/`dialogue_id`/`trigger_message_id` живого треда и снимает `ctx.history`;
  - `tell`/`notify_guest` доставляют через `ctx.notifier`/`ctx.emit` в чужой Telegram-чат, получателя ищут по `ctx.book`;
  - `voice_mode` переключает формат ответа Telegram-чата;
  - `look_at_photo` читает `ctx.store.latest_photo_turn` — историю присланных в тред фото;
  - `guests_list` — `ctx.book.guests()`, внутренняя ACL-модель Telegram-развёртывания;
  - `recall_tool_result` — кэш живёт в памяти одного прохода `run_chat_loop`, у дискретных вызовов MCP его нет;
  - `vpn` — ветки с секретами (issue/reissue/apk) шлют их через `ctx.notifier`; тул неразделим по действиям на уровне `ToolSpec`, поэтому исключён целиком;
  - `swarm_events` читает `ctx.store.recent_events` — журнал бота на alfred (в отличие от `swarm_status`, опрашивающего рой живьём);
  - `act`, `search`, `walk`, `go` — живое состояние интерактивов бота (Этап 59.x).
  Эмулировать мост к этим тулам — отдельная более поздняя задача (Phase 2 плана).

## CLI-чат (`agent/chat.py`)
Интерактивный CLI для ручной проверки агента: Pydantic AI + Ollama + MCP-тулы того же процесса `python -m sa_home_bot.agent.mcp_tools` (дочерний, `fastmcp.client.transports.StdioTransport`). Не подключается к Telegram и не трогает живой /ai (`ai_flow`/`llm_chat`/`tasks/service`/`llm/service`) — отдельный путь для дог-фудинга, пока не решено, как агент будет запускаться по-настоящему (по требованию, отдельный action службы `llm` либо отдельная служба роя — не решено). Персонажный промпт — `settings.llm.persona_prompt` (в живом /ai это ровно `system[0]`); `llm/service.py` целиком не тянем (Coqui/faster-whisper), при пустом поле берём лёгкий `DEFAULT_PERSONA_PROMPT` из `llm/prompt.py` (свежий чекаут/CI).
