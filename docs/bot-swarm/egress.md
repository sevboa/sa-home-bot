# Этап 52: маршрут до Telegram Bot API

Режим `proxy_mode="auto"`: маршрут - строка `"direct"` либо URL прокси `socks5://host:port`.

- Здоровье и пинг прокси берутся из замеров `vpn_check` (действие `telegram_egress` службы vpn) - собственных проб прокси нет (решение этапа). Здоровье direct - успех обычных запросов бота (`TroubleDetector` как request-middleware); пока сидим на прокси, раз в тик (`TICK_S`, в такт vpn_check) один прямой `getMe` через отдельную короткоживущую сессию.
- Выбор (`choose_route`) - чистая функция без I/O. Возврат на direct - после `DIRECT_RETURN_STREAK` успешных проверок подряд. Смена прокси на прокси - только при выигрыше больше `SWITCH_GAIN_FACTOR` раз и `SWITCH_GAIN_MS` мс. Бан прокси, на котором детектор поймал ошибки - `BAN_S`.
- Детектор: триггер - `TROUBLE_CONSECUTIVE` подряд сетевых ошибок либо нет успеха `TROUBLE_NO_SUCCESS_S` при неудачных попытках. Таймаут `getUpdates` тоже сбой: при блокировке DROP'ом (как в KZ) ошибки приходят именно таймаутами, штатный long-poll таймаута не даёт. Флаг ожидания восстановления переживает `reset()`, иначе `on_recovered` (флаш outbox) терялся бы при смене маршрута.
- `apply_route` для direct повторяет `__init__` aiogram (setter `proxy` не умеет вернуть direct), использует приватные поля `AiohttpSession`.
- Стартовый порядок проб (`startup_routes`): последний рабочий маршрут, `proxy` из конфига, direct, `extra_proxies`. Рой на старте не опрошен, поэтому `extra_proxies` стоит держать с SOCKS обеих VPS.
- Оповещения админов о переключении (`SwitchNotices`) не чаще `NOTICE_MIN_INTERVAL_S`; подавленные сворачиваются в строку следующего. `OutboxFlusher` флашит outbox раз в минуту и сразу по восстановлению связи.
- Состояние (`data/egress.json`) пишется во временный `*.tmp` и переименовывается, права обычные (0644, не mkstemp): `utils.files.atomic_write` не подходит - сменил бы права на 0600.
- Включено на alfred в `instances/telegram-bot.alfred.toml`, не в общем конфиге.
