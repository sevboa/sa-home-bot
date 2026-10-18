# ServiceLink и ретрай Telegram

## ServiceLink (`bot/service_link.py`)

Одно подключение на службу (monitor, node, ...). Обрыв не валит бота: фоновая задача переподключается с паузой `RECONNECT_DELAY_S`, запросы в этот момент получают `ServiceUnavailableError`. Pending-алерты монитор досылает сам (у него `handled=False`, пока не было клиента). После каждого подключения кэшируется `describe` - из него строятся кнопки действий, даже когда служба недоступна. `on_connected`-хук не должен рвать соединение, его ошибки логируются.

`ServiceTimeoutError` - подкласс `ServiceUnavailableError`: соединение есть, ответа не дождались. Отдельно его различает тот, кому важно, что повтор не поможет (`llm_chat._call_chat_with_retry`). `describe(dst)` - «в моменте», без кэша; `actions()` - живой describe своей службы, при недоступности кэш или пусто.

## telegram_retry

Живой инцидент 2026-08-12: после ребута alfred DNS для api.telegram.org уже резолвился (это единственное, что проверяет `ExecStartPre`), но HTTP ещё таймаутил, и незащищённый `bot.get_me()` убил процесс. Цикл polling aiogram сам ретраит - защита нужна только разовым вызовам ДО старта поллинга (`get_me`, `get_chat` в `validate_on_startup`).

`call_with_network_retry` - ручной цикл (без tenacity: слабая машина) по образцу `llm/ollama.py::_post_with_retry`. Ретраятся только `TelegramNetworkError` (aiogram заворачивает туда и timeout, и aiohttp.ClientError) и `TelegramRetryAfter`; остальное пробрасывается сразу. Бюджет по умолчанию 3 x 10 с + 2 x 5 с = 40 с держится под `SUPERIOR_READY_TIMEOUT_S=45` из `node/lease.py`, чтобы ретрай не столкнулся с решением аренды забрать слот. После исчерпания процесс падает намеренно: длинные простои закрывает restart-loop супервизора. Таймаут одной попытки - `request_timeout=` у вызова (`REQUEST_TIMEOUT_S`), `bot.session.timeout` не трогается.

## link_watch

`LinkWatchMiddleware` - request-middleware: при восстановлении после офлайна дольше порога зовёт `on_reconnect(downtime)`.
