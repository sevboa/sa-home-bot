# Документация sa-home-bot

В коде нет комментариев и докстрингов. Код объясняет себя структурой и именами, а «почему так»,
живые находки, решения владельца и протокольные договорённости собраны здесь, по темам.
Корневые документы (`README.md`, `ARCHITECTURE.md`, `PROTOCOL.md`, `AUTHORIZATION.md`,
`IMPLEMENTATION_PLAN.md`, `LLM_INTEGRATION_PLAN.md`) остаются на своих местах.

## Общие примитивы
- [foundation/](foundation/README.md) — `utils/*`, `proto/{errors,chunks,emitter,addresses}`,
  `bot/ui/*`, `tests/unit/support/*`: где что лежит и какие дубли туда сведены.

## Telegram-бот
- [ai-flow/](ai-flow/README.md) — ход диалога /ai, контекст, окно истории, стрим черновиков, голос,
  формы подтверждения, память и графовая память.
- [ai-tools/](ai-tools/README.md) — пакет `bot/tools/`: каталог тулов, права, каркас, MCP-агент.
- [interactives/](interactives/README.md) — сценки с Ведущим: кабинет, подвал, катакомбы, предметы,
  снимки, хронология находок.
- [vpn-bot/](vpn-bot/README.md) — экраны /vpn, callback_data, доставка секретов, права и админка.
- [bot-swarm/](bot-swarm/README.md) — события роя, экраны /swarm и /status, маршрут до Telegram.
- [bot-rest/](bot-rest/README.md) — доставка сообщений, авторизация, роутинг хендлеров, режим
  «Альфред в городе», торренты, события монитора.

## Службы и нода
- [node-core/](node-core/README.md) — линки роя, аренды и репликация, жизненный цикл, пробуждение,
  сенсоры, реестр служб, инциденты.
- [node-fixups/](node-fixups/README.md) — `nodectl fix`, `nodectl`, `sa-home-bot init`, пробники VPN,
  сеть и прокси.
- [devlab/](devlab/hub.md) — мастерская роя: хаб `devlab_hub`, токены, роли.
- [vpn-services/](vpn-services/README.md) — служба `vpn`, `vpn_check`, `reality/`, подписка Hiddify.
- [platform-llm/](platform-llm/README.md) — служба `llm`: Ollama, STT/TTS, генерация картинок.
- [platform/](platform/README.md) — конфиг, база данных, здоровье и пороги, бэкапы, торренты, задачи.
- [deploy/extras.md](deploy/extras.md) — extras в `pyproject.toml` и причины пинов зависимостей.
