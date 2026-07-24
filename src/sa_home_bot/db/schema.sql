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
-- bot/ai_flow.py::_build_context_note). На уже существующих БД эти колонки
-- добавляются миграцией ALTER TABLE (db/migrations.py) — CREATE TABLE IF NOT
-- EXISTS их бы не подхватил.
CREATE TABLE IF NOT EXISTS ai_turns (
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,   -- id именно этого сообщения (юзера или бота)
    dialogue_id   INTEGER NOT NULL,   -- message_id команды /ai, начавшей тред
    role          TEXT NOT NULL,      -- user / assistant
    content       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    user_id       INTEGER,            -- telegram user id отправителя (role='user')
    user_name     TEXT,               -- отображаемое имя отправителя (role='user')
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_turns_dialogue ON ai_turns(chat_id, dialogue_id, message_id);

-- Напоминания тула remind (/ai, LLM_INTEGRATION_PLAN.md §8.5) — плоская
-- очередь "когда напомнить в каком чате", опрашивается фоновым циклом
-- bot/reminders.py. due_at/created_at/fired_at — UTC ISO (as everywhere
-- else: сравнение строк работает только при едином часовом поясе).
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    due_at      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    fired_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(due_at) WHERE fired_at IS NULL;
