# Хаб мастерской `devlab_hub`

Этап 60.3.1 (2026-10-31): каркас службы и токены. Очередь, ящик, git, API исполнителя и MCP
добавляются в 60.3.2–60.3.5 на этом же каркасе.

## Что есть
- `devlab/protocol.py` — имена действий, ролей (`client`, `executor`, `gateway`), статусов задания,
  видов сообщений ящика и HTTP-путей. Остальные задачи берут имена только отсюда.
- `ServiceSpec devlab_hub` (singleton, сокет `devlab_hub.socket`), конфиг `[devlab_hub]`
  (см. `docs/platform/config-reference.md`).
- `devlab/hub/http.py` — aiohttp-приложение. Middleware проверяет `Authorization: Bearer`;
  нет токена, плохой или отозванный — 401. Маршрут `/health` помечен `@public` и открыт.
- `devlab/hub/store.py` — SQLite `<data_dir>/hub.sqlite`, таблица `tokens`
  (name, role, hash, created, revoked).
- `devlab/hub/service.py` — действия ноды `issue_token name= role=` и `revoke_token name=`
  (`nodectl call devlab_hub issue_token name=... role=...`).
  Они объявлены только в `capabilities`, не в `actions`: `actions` бот рисует кнопками панели роя,
  а токен не должен выпускаться и показываться в Telegram (решение владельца).

## Решения
- Токен `dlh_` + `secrets.token_urlsafe(32)`, в БД только sha256. Открытый вид возвращается один раз
  в ответе `issue_token` и нигде не хранится и не логируется.
- Имя токена уникально. Повторный выпуск под активным именем отклоняется, под отозванным
  заменяет строку.
- Ограничение по ролям: декоратор `@roles(...)` на обработчике. Без него маршрут доступен любой
  роли с действующим токеном; чужая роль получает 403. `/mcp`, `/git/`, `/api/` повесят свои роли так же.
- Адрес: `bind_host` либо `tailscale ip -4`. Нет адреса: служба не стартует, наружу не слушаем.
- aiohttp отдельной строкой в `pyproject.toml` не заведён: приходит с aiogram, уже используется
  в `vpn/subweb.py`.

## Очередь и ящик (Этап 60.3.2, 2026-10-31)
Чистая логика без сети: `devlab/hub/queue.py` (`HubQueue`), схема таблиц `projects`, `jobs`,
`messages` лежит там же и создаётся в `hub.sqlite` вместе с `tokens` (`HubStore.queue`).
Ошибки правил — `HubError` (`devlab/hub/errors.py`). Таблица переходов — `protocol.TRANSITIONS`.

### Задание
`id` — 6 символов без похожих знаков (`base/<id>` — ветка базы), вид `agent` (`test` зарезервирован
под Этап 61 и отклоняется), проект (должен быть в `projects`; bare-репо создаёт 60.3.3), клиент
(имя токена), модель, `task.md`, приоритет (больше — раньше, по умолчанию 0), статус, `executor`,
времена `created/started/finished`, расход `tokens` и `cost_usd`.

### Статусы и переходы
| из \ в | running | waiting_owner | done / failed | stopped | queued |
|---|---|---|---|---|---|
| queued | `claim` | нет | нет | `stop` | нет |
| running | нет | `question` | `done` / `failed` | `stop` | нет |
| waiting_owner | `answer` | нет | `failed` | `stop` | `resume` |
| done / failed / stopped | нет | нет | нет | нет | `resume` |

Всё остальное — `HubError`, статус не меняется и сообщение не пишется.

### Сообщения ящика
Номер `seq` растёт глобально. Расход в `payload` (`tokens`, `cost_usd`) суммируется в задание,
значения в сообщении — приращения, отрицательные и нечисловые отклоняются.

| вид | направление | эффект |
|---|---|---|
| `started`, `step_done` | к клиенту | только в статусе running, статус не меняют |
| `question` | к клиенту | running → waiting_owner |
| `done`, `failed`, `stopped` | к клиенту | финальный статус |
| `answer` | к исполнителю | waiting_owner → running |
| `resume` | к исполнителю | → queued; `payload.model` меняет модель задания |
| `stop` | к исполнителю | → stopped |
| `release` | к исполнителю | только задание в `waiting_owner`, статус не меняется: исполнитель перестаёт держать mycraft бодрым, не дожидаясь `owner_wait_s` (60.6) |

- `claim(executor, models)` атомарно берёт queued-задание: приоритет по убыванию, затем порядок
  постановки, только модели из списка; ставит `running`, `executor`, `started` (сообщение `started`
  исполнитель шлёт сам).
- `stop` на queued сразу даёт клиенту `stopped` (исполнителя нет). `stop` на running/waiting_owner
  сразу переводит в `stopped` и кладёт `stop` исполнителю; его позднее `stopped` только
  записывается в ящик, остальные сообщения исполнителя по остановленному заданию отклоняются.
- `resume` с другой моделью: новый агент стартует с `wip/<id>`, task.md и журнала шагов,
  диалог старой модели не переносится. `executor` остаётся прежним до нового `claim`.
- Клиент видит и меняет только свои задания (`job.client == имя токена`).

### Курсор
- Клиент: `inbox(client, since)` — сообщения его заданий к клиенту с `seq > since`;
  `ack(client, upto)` помечает прочитанными `seq <= upto`. Курсор хранит клиент, хаб — только `read_at`.
- Исполнитель: `poll(since, job_id | executor)` — сообщения к исполнителю; выданное помечается
  прочитанным, но повторный `poll` с прежним `since` вернёт его снова.
- `purge(retention_days)`: удаляются сообщения с `read_at` старше срока
  (`mailbox_retention_days`); непрочитанные не удаляются никогда. Вызов по расписанию — см. «API исполнителя и шлюзов».

## Git через хаб (60.3.3)

Bare-репозитории лежат в `<data_dir>/git/<имя>.git`. Проект заводит `HubStore.add_project(имя)`
(MCP-обёртка `devlab_project_add` — 60.3.5): строка в `projects`, `git init --bare`, хук
`pre-receive`. Имя: `a-z`, `0-9`, `-`, `_`, до 64 символов, без точек и путей. Если репозиторий
создать не удалось, строка проекта откатывается.

### Адрес и настройка клиента
`http://alfred.tail61d88b.ts.net:8720/git/<имя>.git`. Токен лежит в локальном конфиге клиента:

    git remote add devlab http://alfred.tail61d88b.ts.net:8720/git/<имя>.git
    git config http.http://alfred.tail61d88b.ts.net:8720/.extraHeader "Authorization: Bearer <токен>"

### Как устроено
- `devlab/hub/git_http.py`: `/git/<имя>.git/...` запускает `git http-backend` как CGI
  (asyncio subprocess). Тело запроса и ответ идут потоком кусками по 64 КиБ, pack целиком в память
  не читается. Одновременно не больше 4 процессов (alfred слабый).
- Без действующего токена 401 (общий middleware), неизвестный проект или кривой путь 404.
  Fetch доступен любой роли, push ограничивает хук.
- В окружение бэкенда уходят `REMOTE_USER` = имя токена и `DEVLAB_ROLE` = роль
  (`protocol.GIT_ENV_USER`, `GIT_ENV_ROLE`). Из окружения хаба пробрасываются только
  `PATH`, `PYTHONPATH`, `LANG`, `LC_ALL`.
- `Content-Encoding: gzip`: aiohttp разжимает тело сам, поэтому `CONTENT_LENGTH` и
  `HTTP_CONTENT_ENCODING` бэкенду не передаются, он читает stdin до конца. Проверено тестом.
- В репозитории включены `http.receivepack` и разрешено удаление веток на уровне git
  (`receive.denyDeletes=false`): удаление регулирует хук.

### Права по ролям
| роль | ветки |
|---|---|
| `executor` | `wip/*` создать, обновить, удалить; `agent/*` создать, обновить |
| `client` | `base/*` создать, обновить, удалить; `agent/*` только удалить |
| `gateway`, неизвестная роль | git закрыт целиком: 403 ещё до `http-backend` (шлюзу код проектов не нужен), хук тоже отклоняет push |

`master`, `main`, теги, чужие префиксы и пустое имя после префикса (`wip/`) отклоняются
с сообщением `devlab: отклонено (<имя>): ...`. Если отклонена хоть одна ветка, не проходит весь push.

### Почему хук вызывает общую функцию
Хук `hooks/pre-receive` состоит из двух строк: `exec <python службы> -m sa_home_bot.devlab.hub.refpolicy`.
Правила живут только в `devlab/hub/refpolicy.py` (`check_ref`, таблица `POLICY`): тесты проверяют
те же правила, что исполняет хук, копий на shell нет. Путь интерпретатора подставляется при
создании репозитория (`sys.executable` службы); при переезде venv хуки переустанавливаются
через `repos.install_hook`.

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
