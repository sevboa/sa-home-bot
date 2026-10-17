# Зона bot-swarm: рой, нода и маршрут до Telegram в боте

Оглавление знания, вынесенного из комментариев и докстрингов модулей `bot/`.

- [node-events.md](node-events.md) - приём событий роя и служб (`bot/node_events.py`): кто кому адресован, живые находки.
- [swarm-ui.md](swarm-ui.md) - панель `/swarm`, карточки нод и служб, ссылки-команды `/node_*` и `/svc_*`, `/status`, кэш WoL-реквизитов.
- [egress.md](egress.md) - Этап 52: автовыбор маршрута до Bot API, детектор, антиспам оповещений, outbox.
- [service-link-and-retry.md](service-link-and-retry.md) - `ServiceLink` и ограниченный ретрай Telegram на старте.
