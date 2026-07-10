# Протокол v0 — общение служб ноды

Локальный протокол, по которому службы ноды (monitor, apps, telegram-bot и
сам сервис ноды) общаются между собой. Код — в `src/sa_home_bot/proto/`.
Это контракт ядра роя (ARCHITECTURE §12): модуль на другом языке/ОС обязан
реализовать только его.

## Транспорт и кадрирование

- Endpoint задаётся строкой (`proto/endpoints.py`): путь
  (`./data/node.sock`, допустим префикс `unix:`) — unix-сокет (Linux,
  права `0600`); `tcp://host:port` — TCP (Windows и межнодовый канал).
  Формат сообщений одинаковый.
- **TCP требует аутентификацию** общим токеном роя (`[swarm].token`):
  первое сообщение соединения — запрос `auth` с payload `{"token": "…"}`,
  ответ `{"authenticated": true}`. Любой другой запрос до auth и неверный
  токен → ошибка `unauthorized`, сервер закрывает соединение. События
  рассылаются только аутентифицированным. Сервер на TCP без настроенного
  токена не стартует. Unix-сокет защищён правами файла — auth не нужен
  (запрос `auth` на нём отвечает ok без проверки).
- Кадрирование — **NDJSON**: одно сообщение = одна строка UTF-8 JSON,
  завершённая `\n`. Лимит на сообщение — 1 MiB.

## Конверт

```json
{
  "v": 0,
  "id": "f3a9…",
  "type": "get_state",
  "src": {"node": null, "service": "telegram-bot"},
  "dst": {"node": null, "service": "monitor"},
  "payload": {}
}
```

- `v` — версия протокола. Приёмная сторона отвергает чужую версию
  (`unsupported_proto`).
- `id` — уникальный id сообщения; ответ несёт `id` исходного запроса.
- `src` / `dst` — адреса `{node, service}`; `node: null` — локальная нода.
  Адресат в конверте с самого начала: маршрутизация к удалённым нодам ляжет
  в этот же формат, фронтенд всегда говорит только со своей нодой.
- `ok` / `error` — только в ответах (см. ниже).

## Типы сообщений

### Запросы (клиент → сервер)

| Тип | payload запроса | payload ответа |
|---|---|---|
| `auth` | `{token}` (только TCP, первым) | `{authenticated: true}` |
| `hello` | — | `{node, service, version, proto}` |
| `describe` | — | hello + `capabilities: [str]` + `actions: […]` |
| `get_state` | — | произвольное состояние службы (dict) |
| `command` | `{action, args: {…}}` | результат действия (dict) |

Действие в `describe.actions`:

```json
{"id": "scan_now", "title": "Запустить скан", "params": [
  {"name": "force", "type": "bool", "required": false, "title": "Форсировать"}
]}
```

Фронтенды строят UI и проверяют права (`действие@нода`) по этому списку —
ничего не хардкодят. Сервер валидирует `command` по своему же `describe`:
неизвестное действие → `unknown_action`, нет обязательного параметра →
`bad_request`.

### Ответ (сервер → клиент)

```json
{"v": 0, "id": "<id запроса>", "type": "response", "ok": true, "payload": {…}}
{"v": 0, "id": "<id запроса>", "type": "response", "ok": false,
 "error": {"code": "unknown_action", "message": "нет такого действия: x"}}
```

Коды ошибок: `bad_request`, `unsupported_proto`, `unknown_type`,
`unknown_action`, `unauthorized` (TCP: нет/до auth или неверный токен;
после ответа соединение закрывается), `internal`.

### Событие (сервер → все подключённые, без запроса)

```json
{"v": 0, "id": "…", "type": "event",
 "src": {"node": "alfred", "service": "monitor"},
 "payload": {"event": "overheat_started", "data": {…}}}
```

Доставка — только текущим подключённым (at-most-once). Гарантированная
доставка алертов остаётся на слое выше: monitor хранит pending-флаги в своей
БД, бот после реконнекта добирает состояние через `get_state`.

## Обвязка

- `proto.server.ProtoServer(endpoint, handler, token=…)` — сервер одной
  службы. `handler` реализует `ServiceHandler`: `describe()`, `get_state()`,
  `run_command(action, args)`. События — `broadcast_event(type, data)`.
  Падение обработчика запроса → ответ `internal`, сервер живёт дальше.
- `proto.client.ProtoClient(endpoint, token=…, on_event=…)` — клиент:
  `hello()`, `describe()`, `get_state()`, `command()`. На TCP `connect()`
  сам проходит auth (неверный токен → `ProtoError`). Ответы матчатся по
  `id`, события уходят в callback. Обрыв соединения роняет ожидающие
  запросы `ConnectionError`; переподключение — забота вызывающего.
