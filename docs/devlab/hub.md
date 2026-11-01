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
- `devlab/hub/app.py` — `run_hub` (сокет ноды, HTTP, фоновые `purge_loop` и `wake_loop`),
  `build_hub_app` — приложение со всеми маршрутами (его же собирает сквозной тест).

## Решения
- Адрес: `bind_host` либо `tailscale ip -4`. Нет адреса: служба не стартует, наружу не слушаем.
- aiohttp отдельной строкой в `pyproject.toml` не заведён: приходит с aiogram, уже используется
  в `vpn/subweb.py`.

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
6. Перезапустить ноду alfred (`nodectl restart_node`), проверить `nodectl status` и
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
