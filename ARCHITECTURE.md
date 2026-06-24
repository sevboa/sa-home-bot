# Архитектура домашнего бота-сторожа (sa-home-bot)

## 0. Контекст и происхождение

Этот проект — **личный** Telegram-бот для мониторинга домашней машины
(сервера/ПК). Он создаётся на основе архитектуры, которую автор спроектировал
ранее для рабочего бота мониторинга Camunda Operate (`my-tm-tm-bot`).

**Что переиспользуется:** архитектурная модель и подходы — reconciliation как
ядро логики, один процесс / один event loop / один worker тяжёлых задач,
очередь с дедупликацией, подписочная модель доставки, авторизация по `chat_id`,
системные события жизненного цикла, watchdog связи с Telegram, идемпотентные
уведомления через БД.

**Что НЕ переиспользуется:**
- Любой коммерческий код, принадлежащий компании, и библиотеки, написанные под
  рабочие нужды (`my-operate-connector-module`, `my-operate-reports` и т.п.).
  Домен Camunda Operate целиком заменяется на домен «здоровье домашней машины».
- Реализация **не копируется построчно**. Архитектура и паттерны переносятся, но
  имена классов, переменных, модулей и доменных типов — **новые**, под новый
  домен. Это самостоятельный проект, а не форк.

**Назначение бота:**
- следить за температурой CPU и дисков локальной машины;
- слать предупреждение при перегреве и сообщение о возврате к норме;
- сообщать о собственном ручном отключении и о восстановлении после сбоя/потери
  связи;
- в перспективе — выполнять задачи: проверка календарей автоматически или по
  запросу.

**Чего в боте нет (сознательно):**
- тихих часов (quiet hours) — алерты о перегреве и потере связи нужны сразу, в
  любое время суток;
- любой завязки на сервисы и код компании;
- динамического управления подписками из чата (только конфиг + рестарт).

## 1. Доменная модель: «сторож» вместо «инцидентов Operate»

Ключевой перенос: в рабочем боте сущность — **инцидент Camunda**; снимался срез
активных инцидентов, сравнивался с БД, разница превращалась в уведомления.

Здесь сущность — **показание датчика** (`SensorReading`) и производное от него
**состояние здоровья компонента** (`HealthState`). Аналогия один-в-один:

| Рабочий бот (Operate)          | Домашний бот (sentinel)                       |
|--------------------------------|-----------------------------------------------|
| активный инцидент              | компонент в состоянии `alerting` (перегрет)   |
| инцидент исчез из среза        | компонент вернулся в `ok` (остыл)             |
| `incident_opened`              | `overheat_started`                            |
| `incident_resolved`           | `overheat_cleared`                            |
| reconciliation Operate↔БД      | reconciliation «срез датчиков»↔БД              |

Снимается срез показаний → каждое сравнивается с порогом/baseline →
вычисляется состояние компонента → diff с последним известным состоянием в БД →
переходы (`OK→ALERT`, `ALERT→OK`) → события → рассылка подписчикам.

Это сохраняет главное свойство модели: **уведомление — функция от перехода
состояния, а не от мгновенного значения.** Жёсткий рестарт безопасен: следующий
тик заново снимет срез и догонит состояние из БД.

## 2. Технологический стек

Тот же, что в оригинале (выбор подтверждён):

- **Python 3.11+** (asyncio, typing, tomllib).
- **aiogram 3.x** — Telegram-бот (polling).
- **APScheduler 3.x** (`AsyncIOScheduler`) — планировщик тиков сбора.
- **aiosqlite** + WAL — локальная БД, без ORM, голый SQL.
- **pydantic-settings** — конфиг из TOML + env (префикс `SENTINEL__`).
- Стандартный `logging` (plain/json).

**Сбор показаний — локальный, без внешних сервисов (выбор подтверждён):**

- **CPU-температура** — через `psutil.sensors_temperatures()` (а где его нет —
  fallback на парсинг `sensors -j` из `lm-sensors`). Адаптер изолирует источник.
- **Температура дисков** — через `smartctl` (пакет `smartmontools`), вызов как
  подпроцесс с разбором JSON (`smartctl -j -A /dev/sdX`).
- **Потеря/восстановление связи** — состояние Telegram-сессии бота
  (watchdog-middleware), как в оригинале; опционально позже — ping внешнего
  хоста как отдельный датчик.

Все блокирующие вызовы (`psutil`, `smartctl`-подпроцесс) идут через
`run_in_executor`, чтобы не блокировать event loop — инвариант сохраняется.

## 3. Распространение и запуск

Как в оригинале:

- обычный Python-пакет с `pyproject.toml` (PEP 621);
- установка `pip install -e .`, entry point `sa-home-bot` в `[project.scripts]`;
- запуск `sa-home-bot --config ./config.toml`, либо переменные окружения;
- graceful shutdown по SIGINT/SIGTERM;
- никаких ОС-сервисов внутри приложения; снаружи можно завернуть в systemd
  (пример unit-файла — в README как опциональный материал). Так как бот «крутится
  дома», ориентир — Linux с systemd, но код кроссплатформенный.

## 4. Архитектурная модель

Один процесс, один event loop, **ровно один worker** тяжёлых задач. Та же схема,
что в оригинале:

```
┌──────────────────────────────────────────────────────────────────┐
│                  Главный процесс (asyncio loop)                    │
│                                                                    │
│  ┌──────────────────┐   handlers:                                  │
│  │ Telegram bot     │   • read-only (/status, /ping) → читают БД   │
│  │ (aiogram)        │   • /scan_now → ставит job в очередь          │
│  │                  │   middleware: авторизация по chat_id + DI    │
│  └──────────────────┘                                              │
│                                                                    │
│  ┌──────────────────┐   cron-триггеры (coalesce, max_instances=1): │
│  │ Scheduler        │   • каждые N сек/мин — SensorScanJob          │
│  │ (APScheduler)    │   • housekeeping (ночью)                      │
│  └──────────────────┘   В job'е планировщика — только put в очередь │
│             │                                                      │
│             ▼                                                      │
│  ┌────────────────────┐    ┌──────────────────────────────────┐   │
│  │  DedupQueue         │ ─► │ Worker (ровно один)              │   │
│  │  (sensor-bound jobs)│    │  • снять срез датчиков           │   │
│  └────────────────────┘    │  • вычислить HealthState          │   │
│                             │  • diff с БД → transitions       │   │
│                             │  • классификация → events         │   │
│                             │  • матчинг event → subscriptions  │   │
│                             │  • idempotent запись + рассылка   │   │
│                             └──────────────────────────────────┘   │
│                                                                    │
│  Shared singletons:                                                │
│    • SensorSource (адаптер локальных датчиков)                     │
│    • Database (aiosqlite, WAL)                                     │
│    • Notifier (обёртка над bot.send_message)                       │
│    • SubscriptionBook (статика из конфига)                         │
└──────────────────────────────────────────────────────────────────┘
```

### 4.1 Разделение задач по классам

**Class A — Sensor-bound (через очередь, строго последовательно):**
- снятие среза датчиков + reconciliation + запись в БД + формирование
  уведомлений;
- ручной форс-скан по команде `/scan_now`.

**Class B — Local (inline в handler'е, без очереди):**
- чтение из БД (`/status`, `/stats`);
- разовые read-only запросы текущих значений (если нужно мгновенное «сколько
  сейчас градусов» — выполняется в executor, в БД не пишет).

Граница A/B — как в оригинале: через очередь идёт только то, что **пишет в
таблицу состояний** (reconciliation). Read-only «посмотреть сейчас» — inline.

### 4.2 Reconciliation как ядро

```
срез датчиков + БД → diff → list[Transition]
                            │
                            ▼  классификация
                       list[Event]   (overheat_started / overheat_cleared / …)
                            │
                            ▼  для каждого Event × Subscription:
                       event_type подходит? не замьючено?
                       ──► idempotent запись «уведомление» + отправка
```

Свойства (наследуются из оригинала):
- пропуск тиков безопасен — следующий тик догонит;
- наложение тиков исключено `max_instances=1`;
- множественные пропуски схлопываются `coalesce=True`;
- падение между записью в БД и отправкой не теряет и не дублирует уведомления
  (флаги `notified_*_at` + уникальный ключ);
- транзиентные всплески (возникли и пропали между тиками) не видны — это фича.

### 4.3 Анти-дребезг (важное отличие от Operate)

Температура шумит сильнее, чем инциденты Operate. Чтобы не слать «перегрелся /
остыл» каждые несколько секунд на краю порога, добавляется **гистерезис**:

- переход `OK → ALERT` фиксируется, только если значение выше `warn`-порога
  держится `N` подряд снятых срезов (`consecutive_to_alert`);
- обратный переход `ALERT → OK` — только если значение упало ниже
  `warn − hysteresis_delta` на `M` подряд срезов (`consecutive_to_clear`).

Это чистая логика в `domain/`, тестируется без БД и без датчиков. Параметры — в
конфиге.

### 4.4 Пороги: фиксированные сейчас, адаптивный baseline потом

(Решение подтверждено: «сначала порог, потом baseline».)

- **Этап MVP** — фиксированные `warn`/`crit` пороги (°C) из конфига на CPU и на
  каждый диск. Просто, предсказуемо, быстро запускается.
- **Этап 2** — адаптивный baseline: бот копит историю показаний в БД (таблица
  `readings`), считает скользящую статистику (среднее/перцентиль за окно) и
  алертит на аномальное отклонение. Fallback на фиксированный порог, пока
  истории мало.

Архитектура проектируется под оба сразу: решение «нормально / перегрев» вынесено
в чистую стратегию `ThresholdPolicy` (Protocol). MVP даёт `FixedThresholdPolicy`,
этап 2 — `BaselinePolicy`. Job и БД-схема не меняются при переключении — меняется
только политика и наличие таблицы `readings`.

### 4.5 Подписочная модель

Подписки — **в TOML, иммутабельны во время работы** (как в оригинале: нельзя из
чата подписать чужой чат, изменение = правка конфига + рестарт). Подписка
содержит:

- `name` — человекочитаемое имя для логов;
- `chat_id` — куда слать;
- `event_types` — список типов событий (`"*"` = все);
- `allowed_commands` — управляющие команды, разрешённые в этом чате.

**Тихих часов нет** — поле `quiet_hours` из модели подписки удалено целиком
(подтверждено). Соответственно нет и связанного `utils/quiet_hours.py`, и
проверок `_is_quiet` в job'е.

`SubscriptionBook` (аналог `SubscriptionRegistry`) загружает подписки из конфига
при старте, валидирует через `bot.get_chat`, помечает недоступные как `broken`.

### 4.6 Мьюты (опционально, этап 2)

Семантика «я в курсе, не отвлекайте»: на ограниченное время полностью отбрасывать
события по компоненту для конкретного чата. Применяется на матчинге, до записи.
В MVP можно не реализовывать (перегрев — редкое событие), но БД-схема и точка
расширения предусмотрены.

### 4.7 Авторизация команд

**Chat-level, не user-level** (как в оригинале):

- **Универсальные** (`/help`, `/ping`, `/whoami`) — работают везде без проверок,
  не указываются в `allowed_commands`.
- **Управляющие** (`/status`, `/stats`, `/scan_now`, позже `/mute`) — только в
  подписном, не-broken чате, если имя есть в `allowed_commands`. Проверяет
  `AuthorizationMiddleware` до handler'а.
- `set_my_commands` и `/help` — это UX (показывают доступное), НЕ security.
  Реальная защита — в middleware.

Единый источник правды по командам — `bot/commands.py` (имена + описания), как в
оригинале. Хаб-команды (`/reports`, `/find_processes`) из оригинала **не
переносятся** — в домашнем боте отчётов нет; добавятся, только если появятся
команды-витрины (например, для календарей).

### 4.8 Системные события жизненного цикла

Переносится из оригинала (с новыми текстами и именами):

- старт после чистого завершения / после краша / graceful shutdown;
- восстановление связи с Telegram после длительного дисконнекта (watchdog).

Это закрывает требование «оповещение о ручном отключении и сообщение о
восстановлении после сбоя». Тип события — `system`; получают подписки с `system`
или `*` в `event_types`.

### 4.9 Защита от наложений, graceful shutdown

Без изменений относительно оригинала: `max_instances=1`, `coalesce=True`,
`DedupQueue`; на SIGINT/SIGTERM — стоп scheduler → стоп polling → дослать
прощание → worker дорабатывает текущий job → закрыть БД → флаг чистого
завершения.

## 5. Структура проекта

```
sa-home-bot/
├── pyproject.toml
├── README.md
├── config.example.toml
├── ARCHITECTURE.md            # этот документ
├── AUTHORIZATION.md           # детали модели прав
├── IMPLEMENTATION_PLAN.md     # пошаговый план
├── src/
│   └── sa_home_bot/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py             # argparse, загрузка Settings, --check-config
│       ├── app.py             # сборка и жизненный цикл
│       ├── config.py          # pydantic-модели
│       ├── runtime.py         # started_at / uptime
│       │
│       ├── sensors/           # ← аналог camunda/, но локальные датчики
│       │   ├── __init__.py
│       │   ├── source.py      # SensorSource: read_cpu(), read_disks()
│       │   ├── cpu.py         # psutil / lm-sensors адаптер
│       │   └── disks.py       # smartctl адаптер
│       │
│       ├── db/
│       │   ├── connection.py  # Database (aiosqlite, WAL, FK)
│       │   ├── migrations.py
│       │   ├── schema.sql
│       │   └── store.py       # ← аналог repository.py (новое имя)
│       │
│       ├── domain/            # чистая логика, без БД/сети/aiogram
│       │   ├── models.py      # SensorReading, HealthState, Transition, Event
│       │   ├── policy.py      # ThresholdPolicy (Protocol), FixedThresholdPolicy
│       │   ├── health.py      # compute_health_diff(readings, db_states)
│       │   └── render.py      # тексты сообщений (overheat / cleared)
│       │
│       ├── subscriptions/
│       │   ├── models.py      # Subscription (без quiet_hours)
│       │   └── book.py        # SubscriptionBook (аналог registry)
│       │
│       ├── jobs/
│       │   ├── base.py        # SensorJob protocol, JobContext, JobResult
│       │   └── scan.py        # SensorScanJob (главный job)
│       │
│       ├── worker/
│       │   ├── queue.py       # DedupQueue
│       │   └── worker.py      # JobWorker
│       │
│       ├── scheduler/
│       │   └── setup.py       # build_scheduler, register_jobs
│       │
│       ├── bot/
│       │   ├── setup.py       # Bot/Dispatcher, set_bot_commands
│       │   ├── commands.py    # единый реестр команд
│       │   ├── middlewares.py # ChatContext, DI, AuthorizationMiddleware
│       │   ├── notifier.py    # Notifier (ретраи, чанкование)
│       │   ├── link_watch.py  # ← аналог connection_watch.py (новое имя)
│       │   ├── lifecycle.py   # ← аналог system_events.py (новое имя)
│       │   └── handlers/
│       │       ├── basic.py   # start/help/ping/whoami
│       │       ├── status.py
│       │       ├── stats.py
│       │       └── control.py # /scan_now
│       │
│       └── utils/
│           ├── lifespan.py    # стек shutdown-колбэков + сигналы
│           └── logging.py
└── tests/
    └── unit/
        ├── test_health_diff.py
        ├── test_policy.py
        ├── test_dedup_queue.py
        ├── test_render.py
        └── test_lifecycle.py
```

Принципы (наследуются):
- `domain/` и `subscriptions/` не зависят от инфраструктуры — чистые функции и
  dataclass'ы, тестируются без внешних систем;
- `db/`, `sensors/`, `bot/` — адаптеры; jobs зависят только от их интерфейсов;
- `app.py` — единственное место сборки.

## 6. Контракты ключевых компонентов

### 6.1 SensorSource (аналог CamundaClient adapter)

```python
class SensorSource:
    async def read_cpu(self) -> list[SensorReading]: ...   # по ядрам/пакетам
    async def read_disks(self) -> list[SensorReading]: ... # по устройствам
```

Внутри — `run_in_executor` над `psutil` / `smartctl`. Назначение: изоляция
доменного типа `SensorReading` от формата конкретного источника и возможность
мокать в тестах.

### 6.2 SensorReading / HealthState / Event (domain/models.py)

```python
@dataclass(frozen=True)
class SensorReading:
    component_id: str      # "cpu:package" / "disk:/dev/sda"
    kind: str              # "cpu" | "disk"
    label: str             # человекочитаемое имя
    temperature_c: float
    taken_at: datetime

@dataclass(frozen=True)
class HealthState:
    component_id: str
    status: str            # "ok" | "alerting"
    temperature_c: float
```

### 6.3 ThresholdPolicy (domain/policy.py)

```python
class ThresholdPolicy(Protocol):
    def evaluate(self, reading: SensorReading) -> str: ...  # "ok" | "alerting"
```

`FixedThresholdPolicy` — сравнение с `warn`-порогом из конфига (с гистерезисом).
`BaselinePolicy` (этап 2) — со скользящей статистикой.

### 6.4 compute_health_diff (domain/health.py)

Чистая функция, ядро reconciliation:

```python
def compute_health_diff(
    current: list[HealthState],
    known: dict[str, str],          # component_id -> last known status из БД
) -> HealthDiff:                    # started / cleared / unchanged
    ...
```

### 6.5 Store (аналог Repository)

Один класс, голый SQL, транзакции через `async with store.transaction()`.
Ключевой метод идемпотентности — пометка уведомления отправленным только после
успеха (`mark_overheat_notified`, `mark_cleared_notified`), как `notified_*_at` в
оригинале.

### 6.6 Notifier

```python
class Notifier:
    async def send_direct(self, chat_id: int, text: str) -> bool: ...
    async def send_direct_message(self, chat_id, text, reply_to_message_id=None): ...
```

Ретраи на 429, чанкование длинных сообщений, reply-fallback. «Закрытие»
(`overheat_cleared`) приходит reply'ем на исходное «перегрев», как в оригинале —
для этого `message_id` сохраняется в БД.

### 6.7 SubscriptionBook (аналог SubscriptionRegistry)

```python
class SubscriptionBook:
    def all(self) -> list[Subscription]: ...
    def for_chat(self, chat_id: int) -> Subscription | None: ...
    async def validate_on_startup(self, bot) -> list[ValidationIssue]: ...
```

## 7. Конфигурация (пример)

```toml
[telegram]
token = "..."

[database]
path = "./data/sentinel.sqlite"

[schedule]
scan_cron = "*/1 * * * *"        # снимать срез раз в минуту
housekeeping_cron = "0 3 * * *"

[sensors.cpu]
enabled = true
warn_c = 80.0
crit_c = 90.0
hysteresis_delta_c = 5.0          # обратно в ok при temp < warn - delta
consecutive_to_alert = 3
consecutive_to_clear = 3

[sensors.disks]
enabled = true
warn_c = 55.0
crit_c = 65.0
hysteresis_delta_c = 5.0
consecutive_to_alert = 2
consecutive_to_clear = 2
devices = ["/dev/sda", "/dev/nvme0"]   # пусто = автоопределение

[logging]
level = "INFO"
format = "plain"

# --- Подписки (тихих часов нет) ---

[[subscriptions]]
name = "me"
chat_id = 123456789
event_types = ["*"]               # перегрев + система
allowed_commands = ["status", "stats", "scan_now"]

[[subscriptions]]
name = "family_room"
chat_id = -1009999999
event_types = ["overheat_started", "overheat_cleared"]
allowed_commands = []
```

Все значения переопределяются env с префиксом `SENTINEL__` (разделитель `__`).
Подписки — только в TOML.

## 8. Жизненный цикл приложения

Как в оригинале (`app.run`):

1. логирование;
2. БД (open + миграции);
3. `SubscriptionBook` из конфига;
4. `SensorSource`;
5. `DedupQueue`;
6. aiogram Bot + Dispatcher + Notifier + middleware (DI + авторизация);
7. `validate_on_startup` подписок → пометка broken;
8. `set_bot_commands` по правам чатов;
9. системное приветствие (clean / crash — по флагу `last_shutdown_clean`);
10. `JobContext` → `JobWorker` фоновой задачей;
11. scheduler: регистрация `SensorScanJob` + housekeeping, старт;
12. polling фоновой задачей;
13. ждать shutdown → в обратном порядке остановить всё, дослать прощание,
    выставить флаг чистого завершения.

## 9. Инварианты, которые код обязан соблюдать

1. **Все обращения к датчикам — только из worker'а** (Class A). Из handler'ов —
   только read-only «посмотреть сейчас» через executor, без записи в БД.
2. **Уведомления идемпотентны:** пометка доставки в БД только после успешной
   отправки; при сбое следующий тик дошлёт; дубли исключены ключом.
3. **Каждый job коммитит «состояние + признак уведомления» одной транзакцией.**
   Отправка в Telegram — отдельный шаг, переживающий рестарт.
4. **`max_instances=1` и `coalesce=True` обязательны для всех cron-job'ов.**
5. **`domain/` и `subscriptions/` не импортируют `db/`, `sensors/`, `bot/`,
   `jobs/`.**
6. **Никаких блокирующих вызовов в loop'е** — `psutil`/`smartctl` через
   `run_in_executor`.
7. **Конфиг (включая подписки) читается один раз при старте, дальше иммутабелен.**
8. **Тихих часов нет.** Никакого quiet-hours-gate ни в подписке, ни глобально.
9. **Авторизация — chat-level.** Управляющие команды только при наличии имени в
   `allowed_commands` подписного не-broken чата. Универсальные — везде.
10. **Никакого кода и библиотек компании.** Только локальные датчики и публичные
    зависимости.
11. **Реализация — самостоятельная**, не построчная копия оригинала: новые имена
    модулей/классов/типов под новый домен.

## 10. Что вынесено за скобки (на потом)

- Адаптивный baseline (`BaselinePolicy`) и таблица `readings` — этап 2.
- Мьюты — этап 2.
- Проверка календарей (авто и по запросу) — отдельная фича после стабилизации
  ядра; ляжет как новый `Job` + новый тип события + (возможно) хаб-команда.
- Ночные сводки/дайджесты — не нужны (тихих часов нет, шлём сразу).
- Внешний healthcheck-endpoint, метрики Prometheus — при необходимости.
- Опрос удалённых хостов как датчиков — возможное расширение `sensors/`.
