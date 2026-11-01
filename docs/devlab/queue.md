# Очередь и ящик (Этап 60.3.2, 2026-10-31)

Чистая логика без сети: `devlab/hub/queue.py` (`HubQueue`), схема таблиц `projects`, `jobs`,
`messages` лежит там же и создаётся в `hub.sqlite` вместе с `tokens` (`HubStore.queue`).
Ошибки правил — `HubError` (`devlab/hub/errors.py`). Таблица переходов — `protocol.TRANSITIONS`.

### Задание
`id` — 6 символов без похожих знаков (`base/<id>` — ветка базы), вид `agent` или `test` (см. «Задания test»), проект (должен быть в `projects`; bare-репо создаёт 60.3.3), клиент
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
| `stalled` | к клиенту | кладёт сам хаб (`flag_stalled`), не исполнитель; статус не меняет; см. «Застой» ниже |
| `answer` | к исполнителю | waiting_owner → running |
| `resume` | к исполнителю | → queued; `payload.model` меняет модель задания, `payload.text` кладётся в `params.remarks` задания (прошлые замечания заменяются) |
| `stop` | к исполнителю | → stopped |
| `release` | к исполнителю | только задание в `waiting_owner`, статус не меняется: исполнитель перестаёт держать mycraft бодрым, не дожидаясь `owner_wait_s` (60.6) |

- `claim(executor, models, kinds)` атомарно берёт queued-задание: сначала `test`, затем приоритет по
  убыванию и порядок постановки; `agent` только с моделью из списка и только если `agent` есть в `kinds`; ставит `running`, `executor`, `started` (сообщение `started`
  исполнитель шлёт сам).
- `stop` на queued сразу даёт клиенту `stopped` (исполнителя нет). `stop` на running/waiting_owner
  сразу переводит в `stopped` и кладёт `stop` исполнителю; его позднее `stopped` только
  записывается в ящик, остальные сообщения исполнителя по остановленному заданию отклоняются.
- Замечания `resume` едут в `params.remarks` вместе с `claim` (60.5.6): `claim` может обогнать `poll`, а сообщение `resume` исполнителю нужно только чтобы забыть ждущую запись.
- `resume` с другой моделью: новый агент стартует с `wip/<id>`, task.md и журнала шагов,
  диалог старой модели не переносится. `executor` остаётся прежним до нового `claim`.
- Клиент видит и меняет только свои задания (`job.client == имя токена`).

### Курсор
- Клиент: `inbox(client, since)` — сообщения его заданий к клиенту с `seq > since`;
  `ack(client, upto)` помечает прочитанными `seq <= upto`. Курсор хранит клиент, хаб — только `read_at`.
- Владелец (62.1): `inbox_all(since, limit)` — сообщения к клиенту всех клиентов с `seq > since`
  в паре с заданием (`Job`); `max_seq()` — наибольший `seq` ящика. `read_at` не трогают, `ack` и
  `purge` остаются за клиентами Claude. Читает бот по proto, см. [hub.md](hub.md).
- Исполнитель: `poll(since, job_id | executor)` — сообщения к исполнителю; выданное помечается
  прочитанным, но повторный `poll` с прежним `since` вернёт его снова.
- `purge(retention_days)`: удаляются сообщения с `read_at` старше срока
  (`mailbox_retention_days`); непрочитанные не удаляются никогда. Заодно удаляются каталоги логов
  `<data_dir>/logs/<id>/` заданий `test`, завершённых раньше срока. Вызов по расписанию — см. [api.md](api.md) («Очистка ящика»).

### Застой (Этап 62.6, 2026-11-02)
`flag_stalled(threshold_s, executor_alive, now)` проверяет задания `agent` в `running`
(`waiting_owner` и `queued` не считаются). «Последняя весть» = максимум из `started`, `created`
любого сообщения задания в обе стороны, кроме самого `stalled` (`step_done`, `question`, `answer`,
`resume`), и времени последнего изменения прогресса. Прогресс: исполнитель в heartbeat передаёт
`progress = {job_id: число завершённых ответов модели}` (`JobSpend.replies`, хранится в состоянии
исполнителя), хаб (`note_progress`, таблица `job_progress`) запоминает время, когда счётчик изменился.
Так `stalled` значит «нет ни одного завершённого шага N минут», а не «модель не вызвала
`step_done`». Старый исполнитель без поля `progress` работает как раньше (только сообщения).
Сам heartbeat не весть: он говорит, что исполнитель жив, а не что задание движется. Порог по
умолчанию 45 мин, больше `MODEL_TIMEOUT_S` (30 мин) на один ответ модели. При тишине не меньше
порога в ящик клиента кладётся `stalled` с
`payload.silent_min` и `payload.executor_alive` (свежий heartbeat исполнителя; `false` — исполнитель
пропал). Один раз на застой: новый `stalled` возможен, только если после прошлого пришла весть.
Время берётся из `now` (в тестах подменяется). Периодический вызов — `app.stall_loop`.

### Задания test (Этап 61.1, 2026-11-01)
Прогон тестов клиента на исполнителе, модель не нужна (`model`, `task.md`, `base_branch` пустые).
Параметры лежат в колонке `jobs.params` (JSON, поле `Job.params`): `ref` (ветка или sha, проверяется
`git rev-parse --verify <ref>^{commit}` при постановке), `cmd` (пусто: исполнитель берёт команду из
`.devlab.toml` проекта), `args` (список строк). Ставит `HubQueue.submit_test`; `ref` не начинается
с `-` и без пробелов.

- **Приоритет и слот.** `claim` ставит `test` перед любым `agent`, приоритет агентов не учитывается.
  Исполнитель передаёт `kinds`, которые готов взять сейчас: отдельный слот под тесты спрашивает
  `kinds=["test"]`, слот агента `kinds=["agent"]` с моделями. `kinds` по умолчанию `["agent"]`.
- **Переходы** те же (`protocol.TRANSITIONS`): queued → running (`claim`) → done / failed; `stop` из
  queued и running; `resume` возвращает завершённое в queued (повторный прогон). `question` у
  `test` недопустим, `test_done` у `agent` недопустим.
- **`test_done`** (`protocol.MSG_TEST_DONE`, от исполнителя, running → done). Прогон дошёл до конца,
  красные тесты это тоже `done`, вердикт в `payload.exit_code`. Сбой самого исполнителя (нет
  образа, не скачался ref) — `failed` с `payload.text`. Payload: `exit_code` (int), `duration_s`,
  `summary` {`passed`, `failed`, `errors`, `skipped`}, `failures` (список объектов, например
  `{name, excerpt}`), `tail` (хвост вывода). Сводка живёт только в сообщении ящика, колонки нет:
  `HubQueue.last_message(job_id, kind)` отдаёт её MCP.
- **Ожидание.** `HubQueue.wait_terminal(job_id, timeout)` ждёт `asyncio.Event` задания, который
  будит `post_from_executor` и `post_from_client` после коммита; БД не опрашивается. По сроку
  возвращает текущее задание.
- **Логи.** Полный лог (`output.log`) и junit-xml (`junit.xml`) лежат в `<data_dir>/logs/<id>/`
  (`devlab/hub/joblogs.py`), не в БД. Запись идёт во временный `.part` и подменяется целиком;
  повторная загрузка заменяет файл.

