# Остальной слой Telegram-бота (bot-rest)

Знание, вынесенное из комментариев и докстрингов `bot/*.py` и `bot/handlers/*.py`, которые не принадлежат другим зонам.

- [telegram-delivery.md](telegram-delivery.md) — notifier, ретраи, outbox, индикатор «печатает», рассылка по подпискам.
- [authorization-ui.md](authorization-ui.md) — два барьера (SilenceGate / Authorization), реестр команд и callback-кодов, права гостей, инвайты, меню.
- [handlers-and-routing.md](handlers-and-routing.md) — порядок роутеров в `setup.py` и почему он критичен.
- [away-mode.md](away-mode.md) — режим «Альфред в городе» (этап 51).
- [people-and-recipients.md](people-and-recipients.md) — подбор получателя для тула `tell`.
- [torrents-ui.md](torrents-ui.md) — панель `/torrents`, приём `.torrent` и magnet.
- [monitor-events-and-lifecycle.md](monitor-events-and-lifecycle.md) — события монитора, системные события, дебаг-канал тулов.
