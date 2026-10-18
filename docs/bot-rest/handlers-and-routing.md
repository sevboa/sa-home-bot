# Порядок роутеров и хендлеры

Порядок `include_router` в `bot/setup.py::build_dispatcher` критичен:

1. `SilenceGate` на `dp.update` — самый первый, чужой чат не доходит ни до одного роутера. `ProfileMiddleware` — только для своих (после гейта).
2. `invites` рано: ловит `/invite`, `/guests` и единственное сообщение, которым чужой стал своим (`JustAdmitted`).
3. `basic`, `away` (служебные, работают и пока Альфред в городе; перехват ИИ-входов делается в хендлерах ai/draw/interactives через `away.intercept`), `tool_debug` (свой callback-префикс), `pending_actions` (`pa:`), `interactives` (`ia:`), `items` (`it:`).
4. `draw` — до `ai`, чтобы `/draw` ответом на картинку Альфреда не ушёл в диалог.
5. `ai` (команда `/ai` + узкий фильтр реплаев на свои диалоги, резолвится по `ai_turns`, не по дереву реплаев Telegram).
6. `wake`, `swarm_panel`, `torrents_panel` — ДО `status`: их callback'и (`st:wake`, `st:sw_*`, `st:t_*`) точечные, а `status.on_status_action` ловит весь префикс `st:`.
7. `status`, `control` (`/stats`, `/downtime`, `/scan_now`; бывшие модули stats/power/control слиты), `node`.
8. `node_links` до `apps`: фиксированные префиксы `/node_*` и `/svc_*`.
9. `torrents` (документы и magnet, с командами не пересекается), `vpn` (его кнопки идут по общей `act:`-схеме и разбираются в `node.on_dynamic_action`), `apps` — последним из «точных»: ловит динамические команды-скилы, остальное игнорирует. Приложения из `apps_view.HIDDEN_MENU_APP_IDS` (сейчас qbittorrent) голой командой не открываются — у них панель `/torrents`.
10. `ai.catchall_router` — самый широкий фильтр (любой текст в личке, @упоминание в группе), строго последним.

`setup.py` реэкспортирует `set_bot_commands` из `menu` ради `app.py` (чужая зона).

## Приём торрентов (`handlers/torrents.py`)

Файл/ссылка -> карточка «куда сохранить?» с кнопками директорий из describe службы torrents (`save_path`, choices) -> по нажатию команда `add`. Файл едет по протоколу как base64-строка (`ActionParam` — только string|int|float|bool); `.torrent` на порядки меньше `MAX_MESSAGE_BYTES` (1 МиБ). Права проверяются в хендлере (middleware про `.torrent`/magnet не знает). Выбор директории между сообщением и нажатием хранит `PendingTorrents` (в памяти, на `utils.pending.PendingStore`, 128 записей; рестарт бота его теряет — кнопка честно скажет).

## Воздействия

- `handlers/away.py`: только владелец; `/away <срок> [причина]`, `/away +1ч`, `/back`, `/away` — статус; кнопки `away:ext:<минуты>` и `away:back`. Пояс владельца для «до 23:00» — из карточки (этап 58), иначе пояс сервера.
- `handlers/apps.py`: имена команд не захардкожены, сверяются с актуальным describe.
