# sa-home-bot

> **Версия:** 0.2.0 · **Статус:** ядро готово · Python 3.11+ · 59 тестов, ruff чисто

Личный Telegram-бот-сторож домашней машины: следит за температурой CPU и дисков,
шлёт предупреждение при перегреве и сообщение о возврате к норме, сообщает о
собственном запуске/остановке и о восстановлении связи после сбоя.

Архитектура и решения — в [`ARCHITECTURE.md`](./ARCHITECTURE.md), модель прав — в
[`AUTHORIZATION.md`](./AUTHORIZATION.md), пошаговый план — в
[`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md).

## Возможности (MVP)

- Снятие срезов температуры по cron (по умолчанию раз в минуту).
- Reconciliation: уведомление — функция от **перехода** состояния, а не от
  мгновенного значения. Жёсткий рестарт безопасен.
- Анти-дребезг (гистерезис): перегрев/возврат фиксируются только при удержании
  значения N подряд срезов.
- Идемпотентные уведомления: падение между записью в БД и отправкой не теряет и
  не дублирует сообщения.
- Подписочная доставка и chat-level авторизация команд (только из конфига).
- Системные события: старт (после штатного завершения / после сбоя), graceful
  shutdown, восстановление связи с Telegram.

## Требования

- Python 3.11+
- Для температуры CPU: `psutil` (ставится автоматически); опционально
  `lm-sensors` (бинарь `sensors`) как fallback.
- Для температуры дисков: `smartmontools` (бинарь `smartctl`). Чтение SMART
  обычно требует прав root — см. [«Права на SMART»](#права-на-smart-диски).

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"      # dev — для тестов и линтера; без них: pip install -e .
```

## Конфигурация

Скопируйте пример и отредактируйте:

```bash
cp config.example.toml config.toml
```

- Получите токен у [@BotFather](https://t.me/BotFather) → `[telegram] token`.
- Узнайте свой `chat_id`: запустите бота, напишите ему `/whoami`.
- Заполните `[[subscriptions]]`: `chat_id`, какие события получать
  (`event_types`), какие управляющие команды разрешены (`allowed_commands`).

Любое значение можно переопределить переменной окружения с префиксом `SENTINEL__`
и разделителем `__`, например `SENTINEL__TELEGRAM__TOKEN=123:abc`. Подписки —
только в TOML.

Проверка конфига без запуска:

```bash
sa-home-bot --config ./config.toml --check-config
```

## Запуск

```bash
sa-home-bot --config ./config.toml
```

Останов — `Ctrl+C` (SIGINT) или SIGTERM: бот штатно гасится, дошлёт прощание и
поставит флаг чистого завершения.

## Команды

Универсальные (везде, без проверок): `/help`, `/ping`, `/whoami`.
Управляющие (нужно право в `allowed_commands` подписного чата): `/status`,
`/stats`, `/scan_now`.

## Права на SMART (диски)

Чтение SMART требует root. Под пользовательской службой (`User=`, не root) есть
два пути:

1. **sudo-обёртка** (рекомендуется, не отходя от user-службы). Создайте
   `~/.local/bin/smartctl`:

   ```bash
   #!/bin/sh
   exec sudo /usr/sbin/smartctl "$@"
   ```

   ```bash
   chmod +x ~/.local/bin/smartctl
   ```

   и беспарольный sudo в `/etc/sudoers.d/10-diag`:

   ```
   <user> ALL=(root) NOPASSWD: /usr/sbin/smartctl
   ```

   Адаптер ищет бинарь через `shutil.which`, поэтому обёртка из `~/.local/bin`
   (если каталог в `PATH` службы) подхватывается автоматически.

2. **Системная служба с `User=root`** — проще, но отход от непривилегированного
   запуска.

USB-мосты требуют типа адаптера (`-d`): он берётся автоматически из
`smartctl --scan`, либо задаётся вручную в `[sensors.disks] devices` как
`"/dev/sda:sntjmicron"`. Устройства без SMART (eMMC `/dev/mmcblk*` и т.п.)
пропускаются молча.

Проверить вручную, что мост отдаёт температуру:

```bash
smartctl -d sntjmicron -j -A /dev/sda   # temperature.current должно быть не null
```

## Структура проекта

```
src/sa_home_bot/
├── cli.py / app.py        # точка входа и сборка жизненного цикла
├── config.py / runtime.py # настройки (TOML+env), uptime
├── domain/                # чистое ядро: модели, политика порогов, reconciliation, рендер
├── sensors/               # адаптеры CPU (psutil) и дисков (smartctl)
├── db/                    # aiosqlite (WAL), миграции, Store (голый SQL)
├── subscriptions/         # модель подписки + SubscriptionBook
├── worker/ + jobs/        # DedupQueue, JobWorker, SensorScanJob, housekeeping
├── scheduler/             # APScheduler (cron → очередь)
├── bot/                   # aiogram: команды, notifier, middleware, handlers, lifecycle
└── utils/                 # logging, lifespan (LIFO-shutdown + сигналы)
tests/unit/                # 53 теста: домен, store, scan-job, авторизация, smoke app
```

## Разработка

```bash
pytest            # все тесты (без сети, датчиков и Telegram — всё мокается)
ruff check .      # линтер
```

## Дорожная карта

Вынесено на этап 2 (см. [`ARCHITECTURE.md`](./ARCHITECTURE.md) §10):

- адаптивный baseline (`BaselinePolicy`) и таблица `readings` вместо фиксированных порогов;
- мьюты («я в курсе, не отвлекайте») по компоненту на время;
- проверка календарей (авто и по запросу) как новый job + тип события;
- опрос удалённых хостов как датчиков.

## Запуск под systemd (опционально)

`/etc/systemd/system/sa-home-bot.service`:

```ini
[Unit]
Description=sa-home-bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# smartctl обычно требует root; иначе укажите непривилегированного пользователя
# и настройте доступ к устройствам (cap_sys_rawio / sudoers).
User=root
WorkingDirectory=/opt/sa-home-bot
ExecStart=/opt/sa-home-bot/.venv/bin/sa-home-bot --config /opt/sa-home-bot/config.toml
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sa-home-bot
journalctl -u sa-home-bot -f
```
