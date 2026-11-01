# Выкат исполнителя и шлюзов (чек-лист владельца, не выполнялся)

Состояние на 2026-11-01: код 60.5 готов и проверен сквозным тестом
(`tests/unit/test_devlab_executor_service.py`), живого выката нет. Порядок: хаб на alfred
([hub.md](hub.md)), затем шлюзы, затем исполнитель. Токены выпускаются на alfred в терминале
(показываются один раз), имя токена = имя ноды ([tokens.md](tokens.md)). Всё ниже выполняется только
по команде владельца.

## Шлюзы на jeeves и wooster (`devlab_models`)
1. Выкатить версию на ноду обычным путём (`sa-home-bot update` / деплой роя).
2. На alfred: `nodectl call devlab_hub issue_token name=jeeves role=gateway`, то же для `wooster`.
3. Ключ OpenRouter лежит в `~/.config/sa-home-bot/devlab-openrouter.key` (права 600, на обеих
   нодах и на alfred). В instance-конфиг шлюза положить таблицы:
   ```toml
   [devlab_models]
   hub_url = "http://alfred.tail61d88b.ts.net:8720"
   hub_token = "<токен gateway этой ноды>"

   [devlab_models.openrouter-free]
   kind = "openai"
   base_url = "https://openrouter.ai/api/v1"
   model = "<модель>:free"
   api_key = "<содержимое devlab-openrouter.key>"
   free = true
   report_cost = true
   rpm = 20
   rpd = 200
   context = 65536
   remote = true
   ```
   Имя модели одинаково на обеих нодах: исполнитель переключается между шлюзами без потери диалога.
   `bind_host` пуст: шлюз слушает адрес tailscale0 и сам присылает его в heartbeat (`url`). Ключ
   лежит только в instance-конфиге, в git и в этот документ не попадает.
4. Назначить службу: `nodectl assign devlab_models` на ноде (или `assignments` в конфиге).
5. Проверить: `curl http://<tailscale-адрес>:8721/health`, в `devlab_models` (MCP) модель видна с двумя
   шлюзами.

## Исполнитель на mycraft (`devlab`)
1. Выкатить версию на mycraft. Не забыть про `sa-home-llm` (`systemctl --user restart sa-home-llm`),
   если выкат трогал `llm/`.
2. `nodectl fix` на mycraft: фиксы `devlab` ставят обёртку `sa-home-devlab`, пользователя `devlab`,
   sudoers и каталоги (проверка `check()` покажет, что не так).
3. На alfred: `nodectl call devlab_hub issue_token name=mycraft role=executor`.
4. В instance-конфиг mycraft:
   ```toml
   [devlab]
   hub_url = "http://alfred.tail61d88b.ts.net:8720"
   hub_token = "<токен executor>"
   ```
   Остальное по умолчанию (`docs/platform/config-reference.md`, `DevlabConfig`): ядра и память
   агента `agent_*`, стоп-краны `agent_max_*`, потолок $ 2.
   Сон (60.6.1): `owner_wait_s` (3600) и `socket` (`./data/devlab.sock`) можно не задавать. Служба `devlab`
   должна быть назначена ноде, иначе нода не увидит `busy` и погасит машину в простой llm. Хаб отвечает
   на heartbeat исполнителя полем `queued` (число заданий в `queued`).
5. Назначить службу: `nodectl assign devlab` на mycraft.
6. Проверить: heartbeat executor виден хабу (`devlab_status`), `devlab_test` на тестовой ветке проходит,
   затем одно задание `agent` на модели шлюза.

## Ограничения первого выката
- Агентский слот один; задания `test` идут рядом в своём постоянном слоте.
- Соседство с ботом на CPU (вес slice, заморозка) — 60.6.2.
