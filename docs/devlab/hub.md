# Хаб мастерской `devlab_hub`

Этап 60.3.1 (2026-10-31): каркас службы и токены. Очередь, ящик, git, API исполнителя и MCP
добавляются в 60.3.2–60.3.5 на этом же каркасе.

## Что есть
- `devlab/protocol.py` — имена действий, ролей (`client`, `executor`, `gateway`), статусов задания,
  видов сообщений ящика и HTTP-путей. Остальные задачи берут имена только отсюда.
- `ServiceSpec devlab_hub` (singleton, сокет `devlab_hub.socket`), конфиг `[devlab_hub]`
  (см. `docs/platform/config-reference.md`).
- `devlab/hub/http.py` — aiohttp-приложение. Middleware проверяет `Authorization: Bearer`;
  нет токена, плохой или отозванный — 401. Маршрут `/health` помечен `@public` и открыт.
- `devlab/hub/store.py` — SQLite `<data_dir>/hub.sqlite`, таблица `tokens`
  (name, role, hash, created, revoked).
- `devlab/hub/service.py` — действия ноды `issue_token name= role=` и `revoke_token name=`
  (`nodectl call devlab_hub issue_token name=... role=...`).

## Решения
- Токен `dlh_` + `secrets.token_urlsafe(32)`, в БД только sha256. Открытый вид возвращается один раз
  в ответе `issue_token` и нигде не хранится и не логируется.
- Имя токена уникально. Повторный выпуск под активным именем отклоняется, под отозванным
  заменяет строку.
- Ограничение по ролям: декоратор `@roles(...)` на обработчике. Без него маршрут доступен любой
  роли с действующим токеном; чужая роль получает 403. `/mcp`, `/git/`, `/api/` повесят свои роли так же.
- Адрес: `bind_host` либо `tailscale ip -4`. Нет адреса: служба не стартует, наружу не слушаем.
- aiohttp отдельной строкой в `pyproject.toml` не заведён: приходит с aiogram, уже используется
  в `vpn/subweb.py`.
