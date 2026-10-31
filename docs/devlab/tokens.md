# Токены и роли (60.3.1, 2026-10-31)

- `devlab/hub/store.py` — SQLite `<data_dir>/hub.sqlite`, таблица `tokens`
  (name, role, hash, created, revoked).
- `devlab/hub/service.py` — действия ноды `issue_token name= role=` и `revoke_token name=`
  (`nodectl call devlab_hub issue_token name=... role=...`).
  Они объявлены только в `capabilities`, не в `actions`: `actions` бот рисует кнопками панели роя,
  а токен не должен выпускаться и показываться в Telegram (решение владельца).
- `devlab/hub/http.py` — middleware проверяет `Authorization: Bearer`; нет токена, плохой или
  отозванный — 401. Маршрут `/health` помечен `@public` и открыт.

## Решения
- Токен `dlh_` + `secrets.token_urlsafe(32)`, в БД только sha256. Открытый вид возвращается один раз
  в ответе `issue_token` и нигде не хранится и не логируется.
- Имя токена уникально. Повторный выпуск под активным именем отклоняется, под отозванным
  заменяет строку.
- Ограничение по ролям: декоратор `@roles(...)` на обработчике. Без него маршрут доступен любой
  роли с действующим токеном; чужая роль получает 403. `/mcp`, `/git/`, `/api/` повесят свои роли так же.
