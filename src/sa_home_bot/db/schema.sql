-- Схема БД sa-home-bot (MVP). Применяется идемпотентно.

-- История прогонов job'ов (для /stats).
CREATE TABLE IF NOT EXISTS job_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type     TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',  -- running/ok/error
    error        TEXT,
    metrics_json TEXT
);

-- Техническое KV (last_shutdown_clean и пр.).
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Текущее состояние здоровья компонента (одна строка на component_id).
-- Жизненный цикл: ok → alerting (started) → ok (cleared).
CREATE TABLE IF NOT EXISTS health_states (
    component_id          TEXT PRIMARY KEY,    -- "cpu:package" / "disk:/dev/sda"
    kind                  TEXT NOT NULL,       -- cpu / disk
    label                 TEXT NOT NULL,
    status                TEXT NOT NULL,       -- ok / alerting
    last_temperature_c    REAL,
    consecutive_count     INTEGER NOT NULL DEFAULT 0,  -- для гистерезиса
    alerting_since        TEXT,                -- когда перешёл в alerting; NULL если ok
    first_seen_at         TEXT NOT NULL,
    last_seen_at          TEXT NOT NULL,
    notified_alert_at     TEXT,                -- когда успешно разослали "перегрев"
    notified_cleared_at   TEXT                 -- когда успешно разослали "остыл"
);

-- История показаний датчиков (для BaselinePolicy, этап 2).
-- Пишется только когда хотя бы один вид датчиков в режиме mode="baseline".
CREATE TABLE IF NOT EXISTS readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id  TEXT NOT NULL,               -- "cpu:package" / "disk:/dev/sda"
    temperature_c REAL NOT NULL,
    taken_at      TEXT NOT NULL
);

-- Скользящее окно берётся по последним id в рамках component_id.
CREATE INDEX IF NOT EXISTS idx_readings_component ON readings(component_id, id);

-- Последний SMART-снимок диска (baseline для дельты деградации).
-- Одна строка на диск; обновляется нечастым SmartScanJob. attrs_json —
-- сырые raw-значения отслеживаемых атрибутов: {"5": 31, "197": 0, ...}.
CREATE TABLE IF NOT EXISTS smart_snapshots (
    component_id  TEXT PRIMARY KEY,    -- "disk:/dev/sda" (по realpath)
    label         TEXT NOT NULL,       -- модель диска
    health        TEXT,                -- ok / warning / failed / NULL
    attrs_json    TEXT NOT NULL,       -- {"<attr_id>": <raw>, ...}
    taken_at      TEXT NOT NULL
);

-- Отправленные сообщения: по одному на (component, chat, kind) —
-- нужно для reply "остыл" на исходный "перегрев".
CREATE TABLE IF NOT EXISTS health_notifications (
    component_id  TEXT NOT NULL,
    chat_id       INTEGER NOT NULL,
    kind          TEXT NOT NULL,               -- alert / cleared
    message_id    INTEGER,
    sent_at       TEXT NOT NULL,
    PRIMARY KEY (component_id, chat_id, kind),
    FOREIGN KEY (component_id) REFERENCES health_states(component_id) ON DELETE CASCADE
);

-- /ai: плоская таблица «сообщение -> диалог» для резолва reply-цепочек.
-- dialogue_id — message_id самой команды /ai, начавшей тред (свой же способ
-- адресации уже монотонен и уникален в рамках чата, отдельный uuid не нужен).
-- PRIMARY KEY составной: telegram message_id уникален только в рамках чата.
-- user_id/user_name — отправитель хода (только role='user'; для 'assistant'
-- NULL, это сам Альфред). Нужны, чтобы промпт LLM знал, кто именно пишет,
-- кто начал тред и кто ещё обращался к Альфреду в этом чате (см.
-- bot/ai_flow.py::_build_context_note). photo_path — NULL для ходов без
-- фото; для фото — непрозрачный ключ файла на диске УЗЛА, где физически
-- крутится служба llm (сейчас mycraft), а не путь на этой машине (alfred)
-- — сам бот этот файл не читает и не пишет, только хранит ключ и
-- передаёт его обратно тулу look_at_photo. Сама картинка в content НЕ
-- попадает (там только текстовый плейсхолдер "[фото] ...") — иначе она
-- заново уезжала бы по сети роя в КАЖДОМ будущем ходе этого треда
-- (history пересобирается из ai_turns целиком на каждый запрос). На уже
-- существующих БД эти колонки добавляются миграцией ALTER TABLE
-- (db/migrations.py) — CREATE TABLE IF NOT EXISTS их бы не подхватил.
CREATE TABLE IF NOT EXISTS ai_turns (
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,   -- id именно этого сообщения (юзера или бота)
    dialogue_id   INTEGER NOT NULL,   -- message_id команды /ai, начавшей тред
    role          TEXT NOT NULL,      -- user / assistant
    content       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    user_id       INTEGER,            -- telegram user id отправителя (role='user')
    user_name     TEXT,               -- отображаемое имя отправителя (role='user')
    photo_path    TEXT,               -- ключ уменьшенного фото на диске службы llm
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_turns_dialogue ON ai_turns(chat_id, dialogue_id, message_id);

-- Трасса вызовов тулов /ai (get_time, get_weather, calc, convert_currency,
-- remind) — живая находка 2026-07-24: "шиза" с часовыми поясами (модель то
-- врёт про место, которого нет в таблице get_time, то вообще не зовёт тул)
-- была недиагностируема — ai_turns.content хранит только финальный текст
-- ответа, по нему нельзя понять, вызывался ли тул и что он вернул. Пишется
-- только из bot/ai_flow.py (там есть Store) через колбэк в llm_chat.
-- run_chat_loop — служба tasks (свои задачи/self-scheduling) БД бота не
-- видит вовсе (см. docstring ToolContext в bot/tools.py) и трассу не пишет.
CREATE TABLE IF NOT EXISTS ai_tool_calls (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id            INTEGER NOT NULL,
    dialogue_id        INTEGER NOT NULL,
    trigger_message_id INTEGER,
    tool_name          TEXT NOT NULL,
    args_json          TEXT NOT NULL,
    result             TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_tool_calls_dialogue ON ai_tool_calls(chat_id, dialogue_id);

-- Отложенные задачи роя — служба tasks (`sa-home-bot --service tasks`,
-- sa_home_bot/tasks/), собственная таблица этой службы (не бота — эта
-- схема общая для всех служб проекта, см. db/migrations.py). Замена
-- старого reminders (был здесь до 2026-07-24, писал прямо в БД бота и
-- доставлял константный текст) — задача теперь произвольная протокольная
-- команда (dst_node/dst_service/action/args_json), а результат/неудача
-- отдаётся событием (task_result) тому, кто её создал (meta_json —
-- непрозрачные данные заказчика, эта служба их не читает, только хранит и
-- возвращает целиком). due_at/created_at/fired_at — UTC ISO (сравнение
-- строк работает только при едином часовом поясе, как везде в проекте).
-- prewake_done — за PREWAKE_LEAD_S до due_at служба пробует разбудить dst
-- заранее (см. tasks/service.py) — 0/1, важен только факт "уже пробовали".
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dst_node     TEXT NOT NULL,
    dst_service  TEXT NOT NULL,
    action       TEXT NOT NULL,
    args_json    TEXT NOT NULL,
    timeout_s    REAL NOT NULL,
    meta_json    TEXT NOT NULL,
    due_at       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    fired_at     TEXT,
    prewake_done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_at) WHERE fired_at IS NULL;

-- Долгая память Альфреда о чате (служба memory, sa_home_bot/memory/).
-- Виртуальная таблица FTS5: сам текст индексируется, chat_id и created_at
-- лежат рядом как UNINDEXED (их не ищут, но отдают в ответе) — так вся
-- запись живёт в одной таблице, без пары «данные + индекс» и триггеров
-- синхронизации между ними. Токенайзер trigram, а не стандартный: он ищет
-- по подстроке, что для русского практичнее (см. memory/service.py).
-- ВНИМАНИЕ: память привязана к chat_id — общего хранилища на весь дом нет
-- сознательно, иначе сказанное в личке всплыло бы в общей группе.
-- scope: 'chat' — знает только этот разговор; 'common' — общее знание дома
-- (кто такой Альфред, как он выглядит, что умеет рой), видно во всех чатах.
-- sensitive: личное (адрес, здоровье, деньги, документы) — такое НИКОГДА не
-- бывает common и в контекст уходит с оговоркой «не пересказывай вслух».
CREATE VIRTUAL TABLE IF NOT EXISTS facts USING fts5(
    text,
    chat_id UNINDEXED,
    scope UNINDEXED,
    sensitive UNINDEXED,
    created_at UNINDEXED,
    tokenize='trigram'
);

-- Инвайт-коды приватного входа (бот, sa_home_bot/bot/invites.py). Эфемерная
-- часть механизма: сама выданная подписка живёт в гостевом ПАКЕТЕ и потому
-- переживает переезд бота на резервную ноду (subscriptions/guests.py), а
-- непогашенные коды здесь — нет, и это осознанно: при TTL в час проще
-- выпустить новый, чем реплицировать секрет с коротким сроком жизни.
-- code — уже нормализованный (верхний регистр, без разделителей).
-- issued_by_chat_id — чат админа, выпустившего код: ему уйдёт уведомление о
-- том, кто именно вошёл. redeemed_* заполняются при погашении, revoked_at —
-- при отзыве до погашения; и то, и другое делает код непригодным.
CREATE TABLE IF NOT EXISTS invites (
    code               TEXT PRIMARY KEY,
    issued_by_chat_id  INTEGER NOT NULL,
    issued_by_user_id  INTEGER,
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    redeemed_at        TEXT,
    redeemed_chat_id   INTEGER,
    redeemed_user_id   INTEGER,
    redeemed_user_name TEXT,
    revoked_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_invites_open ON invites(expires_at)
    WHERE redeemed_at IS NULL AND revoked_at IS NULL;

-- Служба vpn (AmneziaWG-доступ на jeeves, sa_home_bot/vpn/). Пир привязан к
-- человеку (chat_id), не к устройству — квота считается суммарно по всем
-- его пирам (device_label различает "телефон"/"ноутбук" и т.п.).
-- status: active — на интерфейсе (или должен быть — реконсайлер сверяет);
-- expired — снят перевыпуском (reissue), история расхода при этом
-- сохраняется за старым peer_id; revoked — отозван вручную.
-- Приватный ключ НИКОГДА не пишется в БД (только публичный) — секрет живёт
-- только в конфиге, который получает гость, см. vpn/service.py::_issue.
CREATE TABLE IF NOT EXISTS vpn_peers (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id            INTEGER NOT NULL,
    device_label       TEXT NOT NULL,
    public_key         TEXT NOT NULL UNIQUE,
    address            TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active',
    created_at         TEXT NOT NULL,
    revoked_at         TEXT,
    last_handshake_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_vpn_peers_chat ON vpn_peers(chat_id);

-- Состояние сэмплера трафика: последние снятые счётчики `awg show <iface>
-- transfer` (с момента поднятия интерфейса, не с начала месяца) — по ним
-- считается дельта на следующем тике. Отрицательная дельта = интерфейс
-- переподняли, дельта считается от нуля (см. vpn/service.py::sample_once).
CREATE TABLE IF NOT EXISTS vpn_counters (
    public_key  TEXT PRIMARY KEY,
    last_rx     INTEGER NOT NULL DEFAULT 0,
    last_tx     INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT
);

-- Накопленный расход по пиру за календарный месяц ("YYYY-MM") — история
-- по месяцам, ролловер получается бесплатно (новый месяц = новая строка).
CREATE TABLE IF NOT EXISTS vpn_peer_usage (
    peer_id     INTEGER NOT NULL,
    month       TEXT NOT NULL,
    used_bytes  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (peer_id, month)
);

-- Гистерезис уведомлений о лимите за месяц: warned_limit_bytes — при каком
-- лимите последний раз слали "осталось N ГБ" (сравнение с ТЕКУЩИМ лимитом
-- решает, слать ли повторно после гранта); blocked_at — когда лимит был
-- исчерпан (пиры сняты реконсайлером), NULL — доступ есть. chat_id=0 —
-- сентинель для учёта состояния канала ВСЕЙ ноды (vpn/service.py::
-- NODE_SENTINEL_CHAT_ID), реальный Telegram chat_id никогда не 0.
CREATE TABLE IF NOT EXISTS vpn_quota_state (
    chat_id             INTEGER NOT NULL,
    month               TEXT NOT NULL,
    warned_limit_bytes  INTEGER,
    blocked_at          TEXT,
    PRIMARY KEY (chat_id, month)
);

-- Добавки к базовой месячной квоте: source='self' — гость сам (+100 ГБ,
-- пока не упёрся в потолок самообслуживания), 'admin' — админ (прямая
-- установка через set_quota либо одобренная заявка, request_id тогда не
-- NULL). Лимит месяца = base_quota_gb (конфиг) + SUM(bytes) по месяцу.
-- Добавки не переносятся на следующий месяц (новая строка month = чистый
-- сброс на базу) — решение пользователя 2026-08-03.
CREATE TABLE IF NOT EXISTS vpn_quota_grants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    month       TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    source      TEXT NOT NULL,
    request_id  INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vpn_quota_grants_chat_month ON vpn_quota_grants(chat_id, month);

-- Заявки на трафик сверх потолка самообслуживания — гость просит, админ
-- решает кнопками approve/deny (vpn/service.py::_resolve_request).
CREATE TABLE IF NOT EXISTS vpn_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    bytes       INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);

-- Журнал системных/админских событий роя (node_down/up/leaving/returned/
-- joined, singleton, update_finished, admin-алерты VPN/idle_power_blocked —
-- bot/node_events.py) — тот же текст, что уходит в рассылку, только ещё и
-- на диск: до 2026-08-04 у Альфреда не было доступа к тому, что УЖЕ
-- произошло, только к текущему состоянию (/nodes, /status). Личные события
-- гостей (VPN-квоты конкретного чата, задачи) сюда намеренно не попадают —
-- решение пользователя 2026-08-04: это чужая приватная информация, не
-- «что случилось с роем».
CREATE TABLE IF NOT EXISTS swarm_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    node        TEXT,          -- кого касается; NULL — не про конкретную ноду
    text        TEXT NOT NULL, -- уже отрендеренный текст рассылки
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_swarm_events_created ON swarm_events(created_at);

-- Отложенный ход диалога (remind, bot/tools.py), ждущий не будильника
-- (tasks.due_at), а конкретного события роя — «нода X применила рестарт»/
-- «нода X закончила update» (решение пользователя 2026-08-04: Альфред не
-- должен держать открытый ответ и гадать через check_update, пока нода
-- перезапускается — вместо этого закрывает текущий ход и просыпается по
-- приходу события). task_id — задача той же самой заявки в СЛУЖБЕ tasks
-- (у той db/schema.sql общая, но файл БД отдельный от бота — здесь только
-- сторона бота, где эти события и приходят, см. bot/node_events.py).
-- due_at у задачи в tasks остаётся страховкой на случай, если событие не
-- придёт вовсе (нода не поднялась) — тогда задача сработает по таймауту;
-- строка чистится в любом случае при получении task_result (см.
-- bot/node_events.py::_handle_task_result — не только при попадании
-- события), поэтому отдельный housekeeping ей не нужен.
CREATE TABLE IF NOT EXISTS event_waiters (
    task_id     INTEGER PRIMARY KEY,
    node        TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_waiters_match ON event_waiters(node, event_type);

-- Кэш официального APK AmneziaWG на jeeves — ровно одна строка (id=1).
-- Свежесть по GitHub API проверяется на каждый запрос (vpn/apk.py), кэш —
-- ускоритель доставки, не источник правды о версии. telegram_file_id —
-- уже загруженный в Telegram файл (сбрасывается на NULL при обновлении
-- версии, см. vpn/service.py::_download_apk), чтобы не гонять байты через
-- рой повторно (bot/vpn_apk.py::deliver_apk).
CREATE TABLE IF NOT EXISTS vpn_apk (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    version            TEXT,
    file_name          TEXT,
    size               INTEGER,
    sha256             TEXT,
    path               TEXT,
    telegram_file_id   TEXT,
    checked_at         TEXT,
    updated_at         TEXT
);
