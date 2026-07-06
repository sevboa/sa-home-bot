# sa-home-bot

> **Версия:** 0.3.0 · **Статус:** ядро готово · Python 3.11+ · 70 тестов, ruff чисто

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

С этапа 13 приложение — **две службы** (см. [`PROTOCOL.md`](./PROTOCOL.md)):

- **monitor** — датчики, пороги, планировщик, своя БД (`[monitor] db_path`);
  наружу — протокол v0 через unix-сокет (`[monitor] socket`);
- **bot** — Telegram-фронтенд: подписки, авторизация, рендер; подключается к
  монитору, получает события и данные `/status` по протоколу.

```bash
sa-home-bot --service monitor --config ./config.toml   # сначала монитор
sa-home-bot --config ./config.toml                     # затем бот (--service bot)
```

Порядок не строгий: бот без монитора живёт и переподключается каждые 5 с, а на
запросы отвечает «служба мониторинга недоступна». Алерты при этом не теряются —
монитор держит их pending и досылает при подключении бота.

Останов — `Ctrl+C` (SIGINT) или SIGTERM: службы штатно гасятся, бот дошлёт
прощание и поставит флаг чистого завершения.

## Команды

Универсальные (везде, без проверок): `/help`, `/ping`, `/whoami`.
Управляющие (нужно право в `allowed_commands` подписного чата): `/status`,
`/stats`, `/scan_now`, `/wake`.

### /wake — Wake-on-LAN

Будит машину в локальной сети (например, домашний ПК) magic packet'ом на её MAC.
Настройка — секция `[wake]` в config.toml (`mac`, опционально `ip` для
ping-проверки «проснулась ли»). На целевой Windows-машине должны быть включены:
WoL в BIOS/UEFI, «Wake on Magic Packet» в свойствах сетевого адаптера, и
выключен «быстрый запуск» (Fast Startup). Надёжно работает только по
Ethernet-кабелю.

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
├── cli.py                 # точка входа: --service bot|monitor
├── app.py                 # сборка бота (фронтенд)
├── monitor/               # служба monitor: сборка, MonitorService, proto-диспетчер
├── proto/                 # протокол v0: сообщения, сервер, клиент (PROTOCOL.md)
├── config.py / runtime.py # настройки (TOML+env), uptime
├── domain/                # чистое ядро: модели, политика порогов, reconciliation, рендер
├── sensors/               # адаптеры CPU (psutil) и дисков (smartctl)
├── db/                    # aiosqlite (WAL), миграции, Store (голый SQL)
├── subscriptions/         # модель подписки + SubscriptionBook
├── worker/ + jobs/        # DedupQueue, JobWorker, SensorScanJob, housekeeping
├── scheduler/             # APScheduler (cron → очередь)
├── bot/                   # aiogram: команды, notifier, middleware, handlers,
│                          # monitor_link/monitor_events (клиент монитора)
└── utils/                 # logging, lifespan (LIFO-shutdown + сигналы)
tests/unit/                # 200 тестов: домен, store, scan-job, proto, monitor, smoke app
```

## Разработка

```bash
pytest            # все тесты (без сети, датчиков и Telegram — всё мокается)
ruff check .      # линтер
```

## Дорожная карта

Этап 2 (см. [`ARCHITECTURE.md`](./ARCHITECTURE.md) §10):

- ✅ адаптивный baseline (`BaselinePolicy`) и таблица `readings` — включается
  `mode = "baseline"` в `[sensors.cpu]` / `[sensors.disks]`. По умолчанию `fixed`.
  Порог = `min(warn_c, mean + k_sigma·max(std, min_std))` по последним
  `baseline_window` показаниям; пока истории мало (`< baseline_min_samples`) —
  фиксированный `warn_c`. Baseline только повышает чувствительность, `warn_c`
  остаётся верхней страховкой.
- ⏳ мьюты («я в курсе, не отвлекайте») по компоненту на время;
- ⏳ проверка календарей (авто и по запросу) как новый job + тип события;
- ⏳ опрос удалённых хостов как датчиков.

> **Ограничение baseline:** при длительном перегреве скользящее окно постепенно
> «привыкает» к высокой температуре (порог ползёт вверх). Онсет ловится надёжно;
> для очень долгих аномалий страховкой служит фиксированный `warn_c`/`crit_c`.

## Запуск под systemd (опционально)

Готовые шаблоны **пользовательских** юнитов — в [`deploy/`](./deploy):
`sa-home-monitor.service` (монитор) и `sa-home-bot.service` (бот). Бот
заказывает запуск монитора через `Wants=`/`After=`, но переживает его
отсутствие. SMART под непривилегированным пользователем — через sudo-обёртку
`~/.local/bin/smartctl` (см. «Права на SMART» выше).

```bash
cp deploy/sa-home-monitor.service deploy/sa-home-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sa-home-monitor sa-home-bot
journalctl --user -u sa-home-monitor -f   # логи монитора
journalctl --user -u sa-home-bot -f       # логи бота
```

Чтобы user-службы стартовали с загрузкой машины без входа в сессию:
`sudo loginctl enable-linger <user>`.
