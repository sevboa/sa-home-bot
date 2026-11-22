# Метрики и графики нод (Grafana + VictoriaMetrics)

Решение владельца 2026-11-23: процессор по режимам, память, swap, диск, сеть, load и температуры
нод смотреть на графиках. Матрица VPN-проверок будет следующей задачей, место под неё — каталог дашбордов.

## Стек
- **Источник**: служба `monitor` каждой ноды. Своего экспортёра (node_exporter) не ставим: монитор уже
  снимает host-метрики и температуры, по ним работают алерты с гистерезисом. Одна вещь — одно место:
  `GET /metrics` отдаёт ПОСЛЕДНИЕ значения из тех же таблиц (`host_metric_states`, `health_states`),
  скан не учащается (`monitor/exposition.py`).
- **Хранилище**: VictoriaMetrics single-node (Debian-пакет `victoria-metrics` 1.112). Почему не Elastic:
  Elasticsearch/Kibana на нашей памяти (alfred 3.7 GiB) не живут, VictoriaMetrics занимает десятки МБ и
  читает Prometheus-формат сама (`-promscrape.config`).
- **Графики**: Grafana из официального репозитория apt.grafana.com (ключ `/etc/apt/keyrings/grafana.asc`,
  `signed-by`), пакет `grafana`.
- Хаб — нода с `[metrics] hub = true` (alfred).

## Безопасность
Наружу ничего. `/metrics` слушает ТОЛЬКО tailscale-адрес ноды (`tailscale ip -4`), Grafana — только
tailscale-адрес хаба, VictoriaMetrics — `127.0.0.1`. Если tailscale-адреса ещё нет (монитор стартовал
раньше tailscaled), монитор повторяет привязку каждые 15 с, а юнит Grafana получает drop-in
`After=/Wants=tailscaled.service` + `Restart=on-failure`, `StartLimitIntervalSec=0`.
Анонимный доступ в Grafana выключен.

## Метрики (`metrics/protocol.py`)
- `sa_up 1`;
- `sa_host_<metric>` — имена из `domain/host.METRICS` (`steal_pct`, `iowait_pct`, `load_per_core`,
  `mem_available_pct`, `swap_used_pct`, `disk_used_pct`, `psi_*`, `oom_kills`, `conntrack_pct`,
  `net_err_rate`, `kernel_stall_events`);
- `sa_temperature_celsius{kind,component}`.
Метки `node` экспортёр не ставит: её добавляет scrape-конфиг хаба (`node="<id ноды>"`), иначе она
столкнулась бы с целевой меткой (`exported_node`).

**Ограничение**: host-метрики монитор снимает только там, где `sensors.host.enabled` (по умолчанию —
`kind = "vps"`). На alfred и mycraft без `[sensors.host] enabled = true` на графиках будут только
температуры (и `up`). Температуры VPS нет (термозон нет).

## Включение
На каждой ноде с службой `monitor`: `[metrics] export = true`, перезапуск ноды. Порт —
`exporter_port` (9100, общий для роя). Нода без `monitor` метрик не отдаёт.
На хабе: `[metrics] hub = true`, затем `nodectl fix` (спросит sudo на alfred, запускает владелец).

Поля `[metrics]`: см. [config-reference.md](config-reference.md#metricsconfig).

## Фикс-апы хаба (`node/fixups/metrics.py`, `needs.metrics_hub`)
| id | что делает |
|---|---|
| `install-victoria-metrics` | пакет `victoria-metrics` |
| `victoria-metrics-config` | каталог данных `data_dir` (владелец `_victoria-metrics`), `/etc/default/victoria-metrics` (`ARGS=...`: `-storageDataPath`, `-retentionPeriod`, `-httpListenAddr=127.0.0.1:<vm_port>`, `-promscrape.config`), `/etc/victoria-metrics/scrape.yml`, enable --now |
| `grafana-repo` | ключ и `/etc/apt/sources.list.d/grafana.list`, `apt-get update` |
| `install-grafana` | пакет `grafana` |
| `grafana-config` | правки `/etc/grafana/grafana.ini` (`http_addr` = tailscale-IP, `http_port`, анонимный вход выкл.), drop-in юнита, enable --now |
| `grafana-provisioning` | источник данных (uid `vm`), провайдер дашбордов, JSON дашбордов в `/etc/grafana/sa-home-dashboards/` |

Debian-пакет victoria-metrics берёт аргументы из `ARGS` в `/etc/default/victoria-metrics` (проверено по
файлам пакета 1.112.0+ds1-2), служба работает от `_victoria-metrics`.

## Scrape-конфиг
Чистая функция `metrics/scrape.py`: цели — все пиры из `[swarm].nodes` (предпочитается tailscale-адрес из
`endpoint/endpoints`, unix и loopback пропускаются) плюс сама нода (`tailscale ip -4`). Надёжно узнать,
у кого включён `export`, нельзя, поэтому недоступные цели просто `up = 0` — это нормально. Список
обновляется при каждом `nodectl fix`; VictoriaMetrics перечитывает файл раз в минуту, рестарт не нужен.
Новая нода в рое: добавить в `[swarm].nodes`, `nodectl fix` на хабе.

## Дашборды
Каталог `metrics/dashboard.py::CATALOG` (имя файла → построитель): сейчас `nodes.json` («Ноды», uid
`sa-home-nodes`; переменная `node` — multi, All). Сводная таблица по нодам (up, steal, iowait, RAM, диск,
load, макс. температура) и графики: steal, iowait, load/ядро, RAM, swap, диск, PSI, conntrack, ошибки NIC,
OOM и стойла, температуры. Матрица VPN-проверок добавится новой записью в `CATALOG`. Дашборды лежат в
коде (генерируются), папки Grafana = подкаталоги провайдера (`foldersFromFilesStructure`).

## Данные
`data_dir` по умолчанию `/mnt/data/metrics` — Hitachi (надёжный диск), не eMMC и не Seagate-расходник.
Хранение `retention = "1y"`.

## Первый вход
`http://<tailscale-IP alfred>:3000` (порт `grafana_port`); логин `admin` / `admin`, Grafana сразу
попросит сменить пароль. Источник данных и дашборд «Ноды» уже заведены.
