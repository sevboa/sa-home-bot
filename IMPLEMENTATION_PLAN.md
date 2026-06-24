# План реализации sa-home-bot

Документ — пошаговый план. Архитектура и контракты — в
[`ARCHITECTURE.md`](./ARCHITECTURE.md), права — в
[`AUTHORIZATION.md`](./AUTHORIZATION.md).

**Принцип этапа:** этап завершён, когда написан код **и** unit-тесты, `ruff`
чист, приложение запускается без ошибок (даже если функциональность ещё
неполная). Тесты — без сети, без реального Telegram, без реальных датчиков
(всё мокается).

**Принцип переноса:** архитектуру и паттерны берём из `my-tm-tm-bot`, но пишем
заново под новый домен — новые имена модулей/классов/типов. Не копировать
построчно.

---

## Этап 0. Скелет проекта

- `pyproject.toml` (PEP 621), entry point `sa-home-bot = "sa_home_bot.cli:main"`,
  зависимости: `aiogram`, `apscheduler`, `aiosqlite`, `pydantic-settings`,
  `psutil`; dev: `pytest`, `pytest-asyncio`, `ruff`.
- Дерево каталогов из `ARCHITECTURE.md` §5 с пустыми `__init__.py`.
- `config.py` — все pydantic-модели Settings (см. §7 архитектуры), `config.example.toml`.
- `cli.py` — argparse, `--config`, `--check-config` (загрузить и напечатать конфиг).
- `utils/logging.py` — plain/json.
- **Готово:** `pip install -e ".[dev]"`, `sa-home-bot --help`,
  `sa-home-bot --config ./config.toml --check-config` работают.

## Этап 1. БД и миграции

- `db/connection.py` — `Database` (aiosqlite, `PRAGMA journal_mode=WAL`,
  `foreign_keys=ON`).
- `db/migrations.py` — идемпотентное применение `schema.sql`.
- `db/schema.sql` — таблицы (см. §«Схема БД» ниже): `job_runs`, `app_state`,
  `health_states`, `health_notifications`.
- `db/store.py` — `Store` со скелетом и методами под smoke-тест.
- **Тест:** открыть БД во временной директории, мигрировать, закрыть.

## Этап 2. Датчики (адаптер источника)

- `sensors/cpu.py` — чтение температур CPU через `psutil.sensors_temperatures()`,
  fallback на `sensors -j`. Возвращает `list[SensorReading]`.
- `sensors/disks.py` — чтение температур дисков через `smartctl -j -A`
  (подпроцесс), парсинг JSON. Автоопределение устройств или список из конфига.
- `sensors/source.py` — `SensorSource.read_cpu()/read_disks()`, всё через
  `run_in_executor`.
- **Тест:** мок `psutil` и фейковый вывод `smartctl` (фикстуры JSON) → проверка
  парсинга в `SensorReading`. Реальное железо в тестах не трогаем.

## Этап 3. Доменная логика (чистая, ядро)

- `domain/models.py` — `SensorReading`, `HealthState`, `Transition`, `Event`,
  `HealthDiff`.
- `domain/policy.py` — `ThresholdPolicy` (Protocol) + `FixedThresholdPolicy`
  (warn-порог + гистерезис).
- `domain/health.py` — `compute_health_diff(current, known)` → started / cleared
  / unchanged. Логика гистерезиса (N подряд срезов) — здесь.
- `domain/render.py` — тексты «🔥 перегрев …» и «✅ остыл …» (HTML), без БД/aiogram.
- **Тесты (полное покрытие):** новый компонент перегрелся; остыл; дребезг у
  порога не вызывает событий (гистерезис); идемпотентность повторного diff;
  рендер.

## Этап 4. Подписки

- `subscriptions/models.py` — `Subscription` (frozen, **без `quiet_hours`**),
  `accepts_event`, `allows_command`, `with_broken`.
- `subscriptions/book.py` — `SubscriptionBook.from_config`, `for_chat`,
  `validate_on_startup` (через `bot.get_chat`, пометка broken).
- **Тесты:** подписка ловит свой event_type; `"*"` ловит всё; права команд.

## Этап 5. Queue + Worker + Job-контракт

- `worker/queue.py` — `DedupQueue` (asyncio.Queue + set ключей; ключ
  освобождается на `get()`).
- `jobs/base.py` — `SensorJob` Protocol, `JobContext` (store, sensors, notifier,
  subscriptions, config), `JobResult` (метрики).
- `worker/worker.py` — `JobWorker`: get → run → запись `job_runs` → task_done;
  падение job'а не валит worker; stop-sentinel.
- **Тесты:** два одинаковых job'а → один; разные → оба; корректный shutdown.

## Этап 6. Главный job — SensorScanJob (с отправкой в лог-заглушку)

- `jobs/scan.py`: снять срез (`SensorSource`) → `FixedThresholdPolicy` →
  `HealthState` → читать известные состояния из БД → `compute_health_diff` →
  в одной транзакции записать новые состояния/переходы → собрать pending
  уведомления (по `notified_*_at IS NULL`) → разослать подписчикам → пометить
  notified только после успеха. «Остыл» — reply на «перегрев» (сохранённый
  `message_id`).
- На этом этапе `Notifier` — заглушка (пишет в лог), реальный Telegram на этапе 8.
- **Интеграционный тест:** мок `SensorSource` с двумя последовательными срезами
  (норма → перегрев → норма), проверить корректные переходы и pending-записи в БД.

## Этап 7. Scheduler

- `scheduler/setup.py` — `build_scheduler` (`AsyncIOScheduler`),
  `register_jobs`: `SensorScanJob` по `scan_cron`, housekeeping по
  `housekeeping_cron`; все `coalesce=True`, `max_instances=1`. Cron-callback
  только кладёт job в `DedupQueue`.
- **Готово:** сверху вниз в лог: cron → queue → worker → notifier-stub.

## Этап 8. Telegram-бот (реальная отправка) + системные события

- `bot/commands.py` — реестр: универсальные (`help`, `ping`, `whoami`),
  управляющие (`status`, `stats`, `scan_now`).
- `bot/notifier.py` — реальная отправка через aiogram (ретраи 429, чанкование,
  reply-fallback).
- `bot/lifecycle.py` (аналог `system_events.py`) — тексты и рассылка событий
  жизненного цикла: старт после clean/crash, graceful shutdown. Новые
  формулировки.
- `bot/link_watch.py` (аналог `connection_watch.py`) — watchdog связи с Telegram
  как `BaseRequestMiddleware`; при восстановлении после долгого дисконнекта —
  broadcast `system`-события.
- `bot/setup.py` — сборка Bot/Dispatcher, цепочка middleware,
  `set_bot_commands` per-chat по правам.
- `bot/handlers/basic.py` — `/start`, `/help` (контекстная по правам чата),
  `/ping`, `/whoami`.
- `subscriptions/book.validate_on_startup` — реальный `get_chat`.
- **Тесты:** рендер/рассылка системных сообщений (мок Notifier);
  `format_duration`; broadcast только живым подписчикам на `system`.

## Этап 9. Команды + авторизация

- `bot/middlewares.py` — `ChatContext`, DI-middleware, `AuthorizationMiddleware`
  (универсальные — без проверок; управляющие — право в `allowed_commands`
  не-broken подписки).
- `bot/handlers/status.py` — `/status`: текущие состояния компонентов и последние
  переходы из БД (scope чата).
- `bot/handlers/stats.py` — `/stats`: сводка прогонов из `job_runs`.
- `bot/handlers/control.py` — `/scan_now`: ставит `SensorScanJob` в очередь.
- **Тесты:** авторизация (универсальные везде; управляющая без права —
  отказ; broken-чат — отказ).

## Этап 10. Сборка жизненного цикла (app.py) и shutdown

- `app.py` — полная сборка по §8 архитектуры; `utils/lifespan.py` (LIFO-стек
  shutdown + сигналы SIGINT/SIGTERM); `runtime.py` (started_at/uptime).
- Флаг `last_shutdown_clean` в `app_state`: приветствие clean vs crash.
- Прощание при graceful shutdown; флаг чистого завершения в финалайзере.
- **Тест:** smoke-запуск с мок-датчиками и фейковым токеном (через
  `--check-config` и/или подмену Bot) — приложение поднимается и гасится чисто.

## Этап 11. Полировка MVP

- `README.md` — установка/запуск, пример конфига, опциональный systemd unit.
- Сценарии падений: убить процесс во время отправки → после рестарта нет дублей
  и нет молчания (тест на идемпотентность через флаги `notified_*_at`).
- Проверка на реальной машине: подобрать пороги, убедиться, что перегрев и
  «остыл» приходят, ручной стоп/старт дают системные сообщения.

---

## Схема БД (MVP)

```sql
-- история прогонов job'ов (для /stats)
CREATE TABLE IF NOT EXISTS job_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type     TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',  -- running/ok/error
    error        TEXT,
    metrics_json TEXT
);

-- техническое KV (last_shutdown_clean и пр.)
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- текущее состояние здоровья компонента (одна строка на component_id)
-- жизненный цикл: ok → alerting (started) → ok (cleared).
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

-- отправленные сообщения: по одному на (component, chat, kind) —
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
```

**Этап 2 добавит** таблицу `readings` (история показаний для `BaselinePolicy`) и
таблицу `mutes` — без изменения существующих.

---

## Проверка end-to-end (после MVP)

1. **Конфиг:** `sa-home-bot --config ./config.toml --check-config` — печатает
   разобранный конфиг без ошибок.
2. **Unit + lint:** `pytest` (всё зелёное), `ruff check .` (чисто).
3. **Сухой прогон с мок-датчиками:** прогнать `SensorScanJob` на фикстурах
   норма→перегрев→норма; убедиться, что в БД появились переходы и pending, а в
   логи/чат ушли «перегрев» и «остыл».
4. **Системные события:** запустить бота, отправить `/ping` и `/status`; затем
   `Ctrl+C` — должно прийти «ухожу в офлайн»; снова запустить — «снова с вами».
5. **Реальное железо:** запустить на домашней машине, нагрузить CPU
   (`stress-ng`/`yes`), убедиться, что приходит алерт и затем «остыл»; проверить
   `smartctl` доступен и температуры дисков читаются.
6. **Идемпотентность:** во время отправки убить процесс (`kill -9`), перезапустить
   — повторных дублей «перегрев» нет, недосланное досылается.
```
