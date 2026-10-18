# Зона vpn-services

Служба `vpn` (выдача и учёт доступа), служба `vpn_check` (пробы доступности), `reality/` (клиентские конфиги и xray), доменные ядра гистерезиса. Знание вынесено из комментариев и докстрингов кода.

| Файл | О чём |
|---|---|
| [architecture.md](architecture.md) | Транспорты, реконсайлер, хранение пиров, сентинель chat_id=0, приватность |
| [protocol-actions.md](protocol-actions.md) | Действия и события протокола `vpn`, прокси Telegram, этап 52 |
| [quota-and-access.md](quota-and-access.md) | Квоты, допуск, самообслуживание, миграции данных, сверка после восстановления |
| [subscription-hiddify.md](subscription-hiddify.md) | Подписка Hiddify, токен, веб-страница, TLS, AmneziaVPN-ключ `vpn://` |
| [client-configs.md](client-configs.md) | sing-box, vless://, xray-конфиг пробника, маршрутизация, AWG-конфиг, имена файлов |
| [health-checks.md](health-checks.md) | `vpn_check`: туннели, гейт, HEAD/GET, гистерезис, устаревание, фанаут |
| [operations.md](operations.md) | sudo-узкие места, xray api, APK-кэш, прокси mtg, `nodectl fix` |
