# Хаб мастерской `devlab_hub`

Этап 60 (мастерская), задача 60.3: служба на alfred, одна на рой. Принимает задания от клиента
(MCP), отдаёт их исполнителю (mycraft), хранит git-репозитории проектов. Сквозной сценарий
проверяет `tests/unit/test_devlab_e2e.py` (сборка приложения — `app.build_hub_app`).

## Разделы
- [tokens.md](tokens.md) — токены, роли, привязка к нодам роя, `connect`, `issue_token` / `revoke_token`.
- [queue.md](queue.md) — задание, статусы и переходы, ящик сообщений, курсоры, очистка.
- [git.md](git.md) — bare-репозитории, `http-backend`, права веток, хук `pre-receive`.
- [api.md](api.md) — HTTP API исполнителя и шлюзов, каталог моделей, MCP клиента.
- [models.md](models.md) — шлюз моделей `devlab_models`: проксирование, лимиты, учёт, introspect.
- [wake.md](wake.md) — пробуждение ноды исполнителя.
- [deploy.md](deploy.md) — выкат шлюзов (jeeves, wooster) и исполнителя (mycraft), чек-лист владельца.
- [client-test.md](client-test.md) — клиент тестов: `nodectl devlab snapshot`, скил `devlab-test`, предел `wait` (61.3).
- [executor.md](executor.md) — исполнитель `devlab`: задания `test`, `.devlab.toml`, конфиг (61.2a).
- [agent.md](agent.md) — агент-разработчик (60.5): цикл модели с инструментами, песочница, окно, страж финала.

## Что есть
- `devlab/protocol.py` — имена действий, ролей (`client`, `executor`, `gateway`), статусов задания,
  видов сообщений ящика и HTTP-путей. Остальные задачи берут имена только отсюда.
- `ServiceSpec devlab_hub` (singleton, сокет `devlab_hub.socket`), конфиг `[devlab_hub]`
  (см. `docs/platform/config-reference.md`). Один HTTP-сервер: `/health`, `/git/`, `/api/`, `/mcp`.
- `devlab/hub/app.py` — `run_hub` (сокет ноды, HTTP, фоновые `purge_loop`, `wake_loop` и
  `stall_loop`: раз в `stall_check_s` ищет задания без вестей, порог `stalled_after_min`, см.
  [queue.md](queue.md) «Застой»),
  `build_hub_app` — приложение со всеми маршрутами (его же собирает сквозной тест).

## Действия владельца по proto (62.1, 2026-11-02)
Бот-владелец видит ящик всех клиентов и отвечает за клиента задания. Это действия ноды
`devlab_hub` (`protocol.ACTION_OWNER_*`), объявлены в `describe`, обрабатываются в
`HubService._dispatch`:
- `owner_inbox since limit`: `since < 0` — `{messages: [], cursor: max_seq}` (первый запуск бота,
  историю не шлём); иначе сообщения к клиенту всех клиентов после `since` (по умолчанию 100, потолок
  `OWNER_INBOX_MAX_LIMIT` = 200, больше обрезается) и `cursor`. В каждом сообщении вложенное `job`:
  все поля задания (`job_view` без `task`) плюс `title` (первая непустая строка task.md без `#`,
  до 120 символов). `read_at` не меняется.
- `owner_job id`: то же `job` (все поля без `task` + `title`) + `question` (текст последнего `question`, иначе пусто).
- `owner_post id kind text model`: `kind` из `CLIENT_MESSAGES`; `answer` требует непустой `text`;
  вызывает `post_from_client(job.client, …)` с `payload[via] = "telegram"`; ответ
  `{seq, status}`. Недопустимый переход и чужие виды — `ERR_BAD_REQUEST` с текстом `HubError`.
- Кто может вызвать. У `ProtoServer` нет проверки прав по действию: сокет хаба unix 0600 (доверен),
  TCP — только с общим `[swarm].token`. Права на `действие@devlab_hub` проверяет нода/бот по
  подписке (`Subscription.allows_action`), у владельца `*`. MCP-клиенты ходят по HTTP с токеном
  `dlh_` и видят только фиксированный список `mcp_tools`; HTTP-маршрутов в `HubService` нет, а
  инструментов `owner_*` нет (страж: `test_owner_actions_have_no_mcp_tool`). Новые MCP-инструменты
  с этими именами заводить нельзя.
- Курсор бота хранит сам бот; `ack` бот не зовёт.

## Решения
- Адрес: `bind_host` либо `tailscale ip -4`. Нет адреса: служба не стартует, наружу не слушаем.
- aiohttp отдельной строкой в `pyproject.toml` не заведён: приходит с aiogram, уже используется
  в `vpn/subweb.py`.

### Окна стенда (2026-11-18)
- `DevlabHubConfig.bench_windows` — список строк `"HH:MM-HH:MM"` по местному времени хаба. Окно
  может переходить через полночь (`"23:00-07:00"`): действует `moment >= a` или `moment < b`,
  внутри суток — `a <= moment < b` (конец окна не включён). Неверная строка падает `ValueError`
  при старте `HubQueue` (`in_windows` в конструкторе).
- `queue.claim` выдает задания `params.purpose = "bench"` только внутри окна; вне окна в выборку
  добавляется условие `purpose != bench`, задания `dev` выдаются как раньше.
- `queue.pending_work` вне окна тоже не считает `bench`: будильник не поднимает mycraft ради
  задания, которое всё равно не выдадут.
- Пустой `bench_windows` — ограничений нет (как сейчас).

## Провал зависимости (2026-11-18)
Когда задание переходит в `failed` или `stopped`, хаб кладёт в ящик клиента сообщение
`dependency_failed` на каждое ждущее (`queued`) задание того же клиента, у которого это задание
есть в `depends_on`. Полезная нагрузка: `dependency` — id провалившегося задания,
`dependency_status` — его финальный статус (`failed` или `stopped`). Статус зависимых заданий не
меняется: они остаются в `queued`. Если владелец перезапустит провалившееся задание (`resume`) и
оно станет `done`, зависимые пойдут сами; не нужны — владелец их остановит.

## База от зависимости (2026-11-18)
При `submit` с одной зависимостью и без явного `base_branch` база становится `agent/<id>` этой
зависимости. Ветку `agent/<id>` зависимости нельзя удалять (`nodectl devlab drop`), пока зависимое
задание не взято исполнителем. При нескольких зависимостях без `base_branch` — ошибка
«несколько зависимостей: укажи base_branch». Явный `base_branch` (как `base/...`, так и
`agent/...`) всегда приоритетен.

## Зависимости тест-заданий (2026-11-18)
`submit_test` и MCP-инструмент `devlab_test` принимают `depends_on` — список id заданий, которые
должны завершиться со статусом `done` до того, как тест-задание станет доступным для `claim`.
Пока зависимости не выполнены, тест-задание остаётся в очереди и не выдаётся исполнителю.
Если `ref` пустой и передана ровно одна зависимость, `ref` автоматически становится
`agent/<id>` этой зависимости (аналогично `submit`). Пустой `ref` без зависимостей — ошибка.
Проверка существования `ref` в репозитории пропускается, если есть `depends_on` (ветка зависимости
появится позже). Несуществующая зависимость — ошибка при отправке.

## Выкат хаба на alfred (чек-лист владельца, выполнен 2026-11-01, v0.154.x)
Живой выкат v0.154.x (2026-11-01): хаб на alfred, исполнитель `devlab` на mycraft. Без службы
исполнителя heartbeat не приходит: пока в очереди есть задание, будильник раз в 10 минут будит
mycraft и пишет в журнал хаба `Будим исполнителя «mycraft»: unreachable`.

1. Назначить службу alfred: `"devlab_hub"` в `[node].assignments` конфига alfred либо
   `nodectl assign devlab_hub` (рантайм-назначение в `state_path`).
2. Добавить в конфиг alfred секцию `[devlab_hub]`. Поля и значения по умолчанию — в
   `docs/platform/config-reference.md` (`DevlabHubConfig`): `socket`, `port` (8720), `data_dir`
   (`/mnt/data/devlab`), `bind_host` (пусто = `tailscale ip -4`), `mailbox_retention_days` (30),
   `executor_node` (`mycraft`). При значениях по умолчанию секция может быть пустой.
3. Убедиться, что на alfred есть `git` с `git http-backend` (`git http-backend` лежит в
   `/usr/lib/git-core/`; пакет `git` уже стоит). Хук `pre-receive` вызывает python службы.
4. Экстра `mcp` хабу не нужна: JSON-RPC реализован самим хабом, библиотека `mcp` нужна только тестам.
5. Каталог `/mnt/data/devlab` на Hitachi (`/mnt/data`, метка `DATA`): создать и отдать пользователю
   службы (`sevboa`). Не на Seagate `/mnt/scratch`: в хабе токены и единственные копии веток.
6. Перезапустить ноду alfred (`nodectl restart_node`; без `--force` при занятой мастерской
   рестарт ждёт освобождения до 30 мин), проверить `nodectl status` и
   `curl http://<адрес tailscale0 alfred>:8720/health` (ответ `{"ok": true, ...}`).
7. Хаб принимает только ноды роя (60.3.8, [tokens.md](tokens.md)): токен привязан к IP ноды с тем же
   именем. Токены executor и gateway выпускаются на alfred в терминале (не в Telegram, токен
   показывается один раз), имя = нода: `nodectl call devlab_hub issue_token name=<нода> role=<роль>`.
8. На каждой ноде-клиенте (в рое должна стоять нода и совпадать `[swarm].token`):
   `nodectl devlab connect`. Команда сама получает токен client у хаба, пишет его в
   `~/.config/sa-home-bot/devlab-token` и подключает MCP через `claude mcp add` (команда в
   [api.md](api.md), «Подключение в Claude Code»). Выданный раньше токен `alfred` (client)
   `connect` перевыпустит.
9. Проверить цикл: `devlab_project_add`, `git push` базы в `base/<имя>`, `devlab_submit`; до
   готовности 60.5 задание останется в `queued`.
