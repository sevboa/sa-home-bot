# Приём событий роя (`bot/node_events.py`)

`build_node_event_handler` - callback для `ServiceLink(node).on_event`. Принимает его геттер `get_node_link`, а не сам линк: хендлер собирается раньше, чем линк существует (он сам передаётся линку в конструктор, см. `app.py`).

## Куда уходит каждое событие

| Событие | Адресат |
|---|---|
| `node_joined`, `node_down`, `node_up`, `node_leaving`, `node_returned`, `singleton_activated`, `singleton_yielded`, `update_finished`, `restart_applied` | чат `system` через `broadcast_system` (как старт/останов, `bot/lifecycle.py`) и запись в журнал `swarm_events` |
| `llm_idle_sleep`, `llm_service_restart` | адресно в `chat_ids` из данных события (служба llm знает точный список чатов тёплого окна), мимо подписок |
| `llm_speech_cured` | `broadcast_all` - всем подпискам, включая гостей без opt-in (разовая новость Логопеда) |
| `llm_cpu_task_started`, `llm_cpu_task_finished`, `llm_night_mode` | никому в боте (обработчик их игнорирует): служебные события 60.6.2 для шлюза моделей и исполнителя devlab на той же ноде (`docs/devlab/models.md`) |
| `idle_power_blocked` | только админам (`notify_admins`) с кнопкой «закрыть SSH-сессии и выключить» (действие `close_ssh_sessions` службы node) |
| `task_prewake`, `task_result`, `deliver_message` | службы tasks; адресно по `meta` |
| `tool_call` | подписки с `event_types=["alfred_tool_call"]` (`notify_tool_call`), адресата в событии нет |
| `vpn_quota_warning`, `vpn_quota_exceeded`, `vpn_access_restored`, `vpn_extra_resolved` | гостю |
| `vpn_extra_requested`, `vpn_node_quota_warning`, `vpn_peer_issued`, `vpn_server_restored` | админам (Этап 33, 57.7) |
| `vpn_check_failed`, `vpn_check_recovered` | админам; пробник (`chat_id=0`) гостям не шлётся |

Остальные события ноды (`service_started`, `service_failed` и т.п.) сюда не заведены.

## Направления объекта события

- `node_down/up/returned` объявляет сосед-объявитель (`node/watch.py`), объект - нода из payload (`node`), а не `src`.
- `update_finished` и `restart_applied` описывают сами себя: объект - `env.src.node` (работает и для пиров, ретрансляция в `node/app.py`).
- `update_finished` - файлы на диске обновлены, процесс не перезапущен (перезапуск делает человек через `restart_node`). `restart_applied` эмитится один раз на старте, когда версия сменилась с прошлого известного старта: единственный сигнал, что нода реально исполняет новую версию. Он и `update_finished` сверяются со службой tasks (`ACTION_MATCH_EVENT`, решение владельца 2026-08-05: ожидания хранит служба tasks, потому что `remind` из уже сработавшего хода не имеет доступа к БД бота).

## Строки событий - литералы

Имена событий в `bot/node_events.py` продублированы строками (не импорт из `node/*` и `llm/*`): модуль ноды тяжёлый, бот тянул бы его ради констант. Источник правды - `node/service.py`, `node/watch.py`, `node/lease.py`, `llm/service.py`.

## Задачи службы tasks

У службы tasks нет доступа к Telegram: доставка и запись в `ai_turns` делаются в боте по `meta`, которую служба не читает.

- `TaskRichSessions` - одна `RichStreamSession` на `task_id` (живая находка 2026-08-10, четвёртый заход): раньше «шаги»/«Агнольд» от tasks слали обычным `send_direct` в обход rich-механики, и при совпадении с живым /ai в чат шли два несовместимых потока. Теперь все состояния одной задачи (waking, ready/failed, итоговый ответ или «Альбегт») подменяют один черновик. `get()` возвращает None, если `response_mode != "rich"` или нет `task_id` - тогда старый plain-путь.
- Сессия на `ready` не закрывается: тот же черновик доезжает до итогового ответа.
- Этап 34.3 (2026-09-30): `_handle_task_result` закрывает сессию в `finally`; раньше падение до `pop` оставляло фоновую задачу освежать черновик до получасового потолка.
- Живой баг 2026-08-05: `meta.message_thread_id` обязателен для ответа в нужный топик личного чата.
- Живой сбой 2026-07-30: «Альбегт» был единственным следом провалившейся задачи - пользователю персонаж, админам причина.
- `deliver_message` (живая находка 2026-08-06): `tell`/`notify_guest` из chat_loop-задачи; получателя и текст готовит служба tasks, отправляет и пишет `ai_turn` бот. `require_acquaintance` (Этап 46) проверяется здесь - в tasks нет Store.
- Формы подтверждения (Этап 45): `task_result` с `meta.kind = pending_action_expire` - будильник экспирации (`PendingActions.expire` идемпотентен); речь Альфреда перед формой (`meta.pending_action_id/stage`) - порядок «сначала Альфред, потом форма»; при сбое LLM - заглушка, форма уходит всё равно; опоздавшую речь (форма уже в чате) не шлём.
- Проактивный агент (Этап 44.1/44.2): без `dialogue_id` тред рождается из `message_id` стартового сообщения Альфреда.

## Прочее

- Текст pip в `update_finished` бывает с `<` (numpy<2): неэкранированный он ронял всё сообщение в Telegram (2026-10-01), поэтому `html.escape`.
- Адрес в vpn_check-сообщении оборачивается в `<code>`, иначе Telegram делает ссылку и превью.
- Ограничение: события роя из старого формата vpn_check (один `target`) понимаются, ноды обновляются не одновременно.
- Вопрос «есть ли другие страны с квотой» при `vpn_quota_exceeded`: не удалось спросить - не обещаем (лучше недосказать).
