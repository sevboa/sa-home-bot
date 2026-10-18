# Доставка в Telegram

Модули: `bot/notifier.py`, `bot/outbox.py`, `bot/dispatch.py`, `bot/lifecycle.py`.

## Единый цикл отправки: `send_with_retry`

Все исходящие (`send_direct`, `edit_*`, `send_document`, `send_photo`, `send_photo_ex`, `send_voice`, rich-сообщения в `rich_stream.py`) идут через `send_with_retry` — `MAX_RETRIES = 3` попытки:

- `TelegramRetryAfter` (429): `sleep(retry_after + 1)` и повтор. Попытка при этом расходуется (цикл по `range`), хотя по смыслу лимит — не наш сбой.
- Транзиентный сбой (обрыв, таймаут, зависший SOCKS-прокси до Telegram, 5xx): пауза по `_TRANSIENT_BACKOFF_S = (0, 2, 5)` секунд и повтор. Цель — пережить моргание прокси, а не ждать минутами: пользователь смотрит на «печатает».
- Перманентный сбой (`is_permanent_send_error`: `TelegramBadRequest`, `TelegramForbiddenError`, `TelegramNotFound`): `None` сразу, повтор того же payload бессмыслен — вызывающий деградирует формат или сообщает об отказе.
- `outcome["transient"] = True` выставляется, если сдались из-за транзиентного сбоя: такое сообщение ставится в outbox.

Живые находки:
- 2026-08-29: сетевые сбои `aiohttp_socks.ProxyError` / `ProxyTimeoutError` — обычные `Exception`, не подклассы `TelegramAPIError`, и раньше улетали наверх. Всё, что не «перманентное», считается транзиентным.
- 2026-08-30: первый же не-429 сбой давал `return None` — на `rich_stream.finalize()` терялся готовый ответ модели. На путях «терять нельзя» лучше зря сделать пару попыток.

Рефакторинг (этап «bot-rest»): `send_document`, `send_photo`, `send_voice` раньше повторяли цикл вручную; теперь это тонкие обёртки над `send_with_retry`. Политика 429 / permanent / transient не менялась, закреплена тестами `test_media_send_*` в `tests/unit/test_notifier.py`.

## `send_direct`

- Чанкование по `MAX_LEN = 4096` (`chunk_text`, режет по последнему переводу строки).
- `reply_markup` вешается на ПЕРВЫЙ чанк: кнопка относится к сообщению целиком.
- `message_thread_id` — топик личного чата (Private Chat Topics). Без него проактивная отправка уезжает в общий топик (живой баг vpn 2026-08-03: конфиг/QR/APK терялись из вида).
- Этап 52: при транзиентном провале и наличии `Notifier(outbox=Store)` недоставленные чанки ставятся в постоянную очередь (`_enqueue_undelivered_chunks`). `outbox_kind` (`OUTBOX_KIND_*` из `db/store.py`) задаёт TTL, `dedup_key` — «свежее вытесняет старое»; у чанков одного сообщения ключ получает суффикс `#<индекс>`. Сбой самой очереди только в лог.
- `Notifier.bot` нужен местам, которым нужна другая механика Bot API поверх того же бота (`RichStreamSession`, `node_events::_handle_task_prewake`).

## Outbox (`bot/outbox.py`, этап 52)

`flush_outbox` досылает записи по `created_at`; один флаш одновременно. 429: ждём `retry_after+1` и повторяем ту же запись (до `_MAX_RETRY_AFTER = 3`, затем флаш откладывается); транзиентный: `attempts+1` и остановка флаша; перманентный: запись удаляется с warning; протухшее по TTL удаляется до отправки. Старше `LATE_MARK_AFTER` (60 с) — к тексту добавляется «⏳ с задержкой, ЧЧ:ММ».

## Индикатор «печатает» (`TypingIndicator`)

- Решение владельца 2026-08-11: индикатор горит ровно пока в чат льётся текст ответа, а не пока модель «думает» (там свой thinking-блок `RichStreamSession.push_status`).
- Этап 34.3 (2026-09-30): при стриме черновиком индикатор не включается вовсе — параллельный `send_chat_action` раз в 4 с добавлял клиенту обновлений, Android подвисал. Остался для rich-режима без черновика (`LlmConfig.rich_draft_streaming=False`) и фолбэков (`typing_action`).
- Telegram гасит typing примерно через 5 с, поэтому повтор каждые `TYPING_KEEPALIVE_INTERVAL_S = 4`.
- `TYPING_MAX_TICKS = 450` (450 × 4 с = 30 минут) — предохранитель на случай `start()` без `stop()`; тот же запас, что у keep-alive черновика.
- Первый `send_chat_action` вызывается синхронно в `start()`, не через `create_task`: если дальше нет реального I/O, фоновая задача могла не успеть выполниться до `stop()`.
- Сетевой сбой индикатора проглатывается (некритичный побочный эффект).

## Рассылка по подпискам

Одна функция `notifier.broadcast(subs, notifier, text, **send_kwargs) -> int` (число доставленных). Отбор «кому» задают вызывающие:

| Кто зовёт | Отбор |
|---|---|
| `notify_admins` | `WILDCARD in allowed_commands` |
| `lifecycle.broadcast_system` | `book.accepting(EVENT_SYSTEM)` |
| `lifecycle.broadcast_all` | все не-broken (события, обязанные дойти и до гостей, у которых `event_types` пуст) |
| `lifecycle.notify_tool_call` | не-broken и точное `alfred_tool_call` в `event_types` |
| `dispatch._dispatch_host`, `dispatch_smart` | `book.accepting(тип)`, без outbox |

`dispatch_alert` / `dispatch_clear` остаются отдельными: они пишут `record_notification` и (clear) отвечают reply-ем на сообщение алерта. У них `dedup_key = health:<component_id>` общий: если алерт ещё ждёт в outbox, «восстановлено» заменяет его.

Host/smart-события идут без outbox: недоставленное монитор повторит сам (`handled=False`). `handled` у алертов здоровья всегда `True`: иначе следующий тик зашлёт дубль живым подписчикам.
