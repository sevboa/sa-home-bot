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
- «Графиковые» метрики `domain/host.GAUGES` (без порогов, алертов не бывает): `cpu_busy_pct` (всё, кроме
  idle и iowait), `mem_used_pct`, `mem_available_bytes`, `load1/load5/load15`, `uptime_s`, `procs`, а
  также с экземплярами: `disk_pct{mount}` (реальные ФС ext*/xfs/btrfs/... на `/` и `/mnt/*`) и
  `net_rx_bps{iface}` / `net_tx_bps{iface}` (байт/с за окно замера; физические интерфейсы со
  `/sys/class/net/<if>/device` и `tailscale0`, без lo/veth/vprobe/docker/br-). Все метрики — gauge, rate в
  Grafana считать не нужно. Экземпляр кодируется в `component_id` (`host:disk_pct:/mnt/data`), ключи
  существующих метрик не менялись; метка (`mount`/`iface`) добавляется при экспорте.
- `sa_temperature_celsius{kind,component}`.
Метки `node` экспортёр не ставит: её добавляет scrape-конфиг хаба (`node="<id ноды>"`), иначе она
столкнулась бы с целевой меткой (`exported_node`).

**Где снимаются host-метрики**: на VPS (`sensors.host.enabled`, по умолчанию `kind = "vps"`) — с алертами
по порогам, как раньше. На любой другой ноде при `[metrics] export = true` монитор снимает те же метрики,
но только для экспорта: порогов нет, алерты не рассылаются, в карточку статуса бота они не попадают
(`host_health` пуст). Температуры VPS нет (термозон нет).

## Grafana не докачивает плагины
Решение владельца 2026-11-23: в `grafana.ini` `[plugins] preinstall_disabled = true`,
`preinstall_auto_update = false`, `[analytics] check_for_plugin_updates = false` — иначе Grafana 12 сама
ставит и обновляет облачные источники (cloudwatch, stackdriver и т. п.). Применяется `nodectl fix` на хабе
(`grafana-config`), нужен перезапуск grafana-server (фикс-ап делает сам). Уже скачанные плагины в
`/var/lib/grafana/plugins` остаются на диске.

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
`sa-home-nodes`; переменная `node` — multi, All). Сводная таблица по нодам (up, CPU %, RAM занято %, диск макс %, steal %, аптайм,
макс. температура) и графики: CPU %, steal, iowait, load/ядро и load average, RAM занято и доступно, swap,
диски по точкам монтирования, сеть rx/tx по интерфейсам, аптайм, число процессов, PSI, conntrack, ошибки
NIC, OOM и стойла, температуры. Матрица VPN-проверок добавится новой записью в `CATALOG`. Дашборды лежат в
коде (генерируются), папки Grafana = подкаталоги провайдера (`foldersFromFilesStructure`).

## Данные
`data_dir` по умолчанию `/mnt/data/metrics` — Hitachi (надёжный диск), не eMMC и не Seagate-расходник.
Хранение `retention = "1y"`.

## Первый вход
`http://<tailscale-IP alfred>:3000` (порт `grafana_port`); логин `admin` / `admin`, Grafana сразу
попросит сменить пароль. Источник данных и дашборд «Ноды» уже заведены.
