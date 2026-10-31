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
  (`mailbox_retention_days`); непрочитанные не удаляются никогда. Вызов по расписанию — в 60.3.4.
