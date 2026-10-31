# API исполнителя, шлюзов и MCP клиента

## API исполнителя и шлюзов (60.3.4, 2026-10-31)

Тот же HTTP-сервер и bearer-токен, не proto роя. Код: `devlab/hub/executor_api.py`; пути и поля —
константы `protocol.HTTP_*_PATH`, `protocol.FIELD_*`. Все методы `POST` с JSON-объектом.
`HubError` (недопустимый переход, чужое задание, плохое тело) превращается в `400 {"error": "..."}`
общим `error_middleware`; нет токена — 401, чужая роль — 403.

| путь | роль | тело | ответ |
|---|---|---|---|
| `/api/claim` | executor | `models`: список имён, доступных исполнителю | `job`: задание (поля `Job`) или `null`; задание уже `running`, `executor` = имя токена |
| `/api/post` | executor | `job_id`, `kind` (из `EXECUTOR_MESSAGES`), `payload` | `seq` сообщения; переходы статусов и расход (`tokens`, `cost_usd`) считает `HubQueue.post_from_executor` |
| `/api/poll` | executor | `since` (по умолчанию 0) | `messages` (answer, resume, stop, release по заданиям этого исполнителя), `cursor` |
| `/api/heartbeat` | executor, gateway | исполнитель: `busy`, `jobs`; шлюз: `models` и `limits` | `ok` |
| `/api/introspect` | gateway | `token`: токен, предъявленный шлюзу; `address`: адрес клиента у шлюза (должен быть IP ноды токена) | `name`, `role`; 404 для неизвестного, отозванного и любого не-executor токена |

- `poll`: `cursor` — `seq` последнего выданного, его исполнитель передаёт в следующем `since`
  (курсор хранит исполнитель; выданное помечается прочитанным, это основа `purge`). Исполнитель
  видит только задания, где `executor` = имя его токена, и в `post` чужое задание тоже отклоняется.
- `heartbeat` хранится в таблице `heartbeats` (одна строка на токен: роль, JSON, время). Новый
  заменяет старый. У шлюза `models` — список объектов с непустым `name`; остальные поля
  (local/remote, цена, окно) хаб не разбирает, а отдаёт как есть.
- Каталог моделей: `HubStore.models()` — объединение `models` шлюзов, чей heartbeat свежее
  `HEARTBEAT_FRESH_S` (120 с); у каждой записи добавлены `gateway` (имя токена шлюза) и `limits`
  шлюза. Одна модель у нескольких шлюзов даёт несколько записей, выбор по приоритету — за клиентом
  и исполнителем. Из каталога читает MCP-инструмент `devlab_models` (60.3.5).
- Проверки `allow_remote_models` и remote-моделей на хабе нет: это клиент и исполнитель.
- Очистка ящика: `app.purge_loop` — фоновая задача `run_hub`, раз в сутки (`PURGE_INTERVAL_S`)
  зовёт `HubQueue.purge(mailbox_retention_days)`; сбой пишется в журнал и не останавливает цикл.

## MCP клиента (60.3.5, 2026-10-31)

`POST /mcp` на том же сервере, только роль `client` (`@roles`): без токена 401, executor и gateway
получают 403. `GET /mcp` отвечает 405 (потока сервер→клиент нет). Код: `devlab/hub/mcp_http.py`
(JSON-RPC) и `devlab/hub/mcp_tools.py` (инструменты, ресурсы). Имена инструментов, ресурсов и
методов — константы `protocol.TOOL_*`, `protocol.RESOURCE_*`, `protocol.MCP_*`.

### Почему свой минимальный Streamable HTTP
Серверная часть библиотеки `mcp` построена на ASGI/Starlette, а хаб — aiohttp на слабом alfred.
Библиотека к тому же стоит в необязательном экстра `mcp`, и хабу не нужна. Поэтому: stateless
JSON-RPC 2.0 через POST, ответ `application/json` (без SSE и без сессий). Поддержаны `initialize`
(версия протокола — запрошенная клиентом из `MCP_PROTOCOL_VERSIONS`, иначе первая),
`notifications/initialized` (202), `ping`, `tools/list`, `tools/call`, `resources/list`,
`resources/read`. Хаб не импортирует `mcp`; настоящий клиент из библиотеки используется только в
тесте `tests/unit/test_devlab_mcp.py`.

### Инструменты
Клиент — имя токена: задания и ящик видны только свои, чужое задание отвечает «задания нет».
Ни один инструмент не ждёт mycraft. `HubError` превращается в результат с `isError: true` и текстом
причины, не в 500. Неверные аргументы (тип, лишний или недостающий параметр) — тоже `isError`;
неизвестный инструмент — JSON-RPC ошибка `-32602`. Результат — JSON-текст.

| инструмент | параметры | что делает |
|---|---|---|
| `devlab_models` | — | каталог живых моделей (`HubStore.models()`) |
| `devlab_project_add` | `name` | регистрирует проект и bare-репозиторий; в ответе `git_path` |
| `devlab_projects` | — | список проектов |
| `devlab_submit` | `project`, `model`, `task`, `base_branch` (`base/...`), `priority` | ставит задание; `warnings`, если модели нет в каталоге (задание всё равно ждёт её) |
| `devlab_list` | `statuses` | свои задания без текста task.md |
| `devlab_status` | `id` | своё задание целиком |
| `devlab_inbox` | `since`, `limit` | сообщения к клиенту с курсором `cursor` |
| `devlab_ack` | `upto` | помечает прочитанным |
| `devlab_answer` | `id`, `text` | ответ на `question` (waiting_owner → running) |
| `devlab_resume` | `id`, `text`, `model` | в очередь, с замечаниями и/или другой моделью (с `warnings`) |
| `devlab_stop` | `id` | остановка |
| `devlab_release` | `id` | `release`: только для waiting_owner |

`answer`, `resume`, `stop`, `release` возвращают `seq` сообщения и новый `status`. Текст `answer` и
`resume` лежит в `payload.text` (`protocol.PAYLOAD_TEXT`).

`base_branch` теперь параметр `HubQueue.submit` (должна начинаться с `base/`, без `..`);
не задана — прежнее `base/<id>`. Клиент до постановки не знает `id`, поэтому пушит базу под своим
именем. `devlab_submit` проверяет, что ветка есть в bare-репозитории проекта
(`repos.branch_exists`, `git rev-parse --verify`): клиент после постановки часто офлайн, поэтому
ошибка с подсказкой `git push <git_path> HEAD:<base>` приходит сразу, а не у исполнителя.

### Ресурсы
`devlab://task-template` (шаблон task.md) и `devlab://usage` (цикл: проект → push `base/<x>` →
submit → inbox/answer → fetch `agent/<id>` → ревью → resume или приём → удаление веток). Тексты —
`devlab/hub/resources/*.md`, включены в пакет через `package-data` в `pyproject.toml`.

### Подключение в Claude Code
```
claude mcp add --transport http devlab http://alfred.tail61d88b.ts.net:8720/mcp \
  --header "Authorization: Bearer <токен>"
```
Синтаксис сверен с `claude mcp add --help` (2026-11-01).
Порт 8720 взят из задания, реальный — `[devlab_hub] port`. На ноде роя эту команду выполняет
`nodectl devlab connect` ([tokens.md](tokens.md)); запросы принимаются только с IP ноды, чьё имя у токена.
