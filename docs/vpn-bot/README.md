# Бот: экраны и уведомления VPN

Зона `bot/handlers/vpn*`, `bot/vpn_*.py`. Серверная часть VPN (служба `vpn`, `vpn_check`, протокол) описана отдельно.

- [callbacks-and-screens.md](callbacks-and-screens.md) - схема callback_data, экраны `vpn_card`, префиксы `~`, `f_`, `t_`, мастер, эксперт-режим
- [secrets-delivery.md](secrets-delivery.md) - как секрет (ключ, QR, файл) доходит до гостя, TTL, порядок способов
- [rights-and-admin.md](rights-and-admin.md) - права, тумблеры, экраны гостей и локаций, заявки владельцу
- [health-notifications.md](health-notifications.md) - индикаторы проверок, события службы, тексты уведомлений
- [decisions.md](decisions.md) - решения владельца по тексту и поведению, которые нельзя терять
