# Метрики и графики нод (Grafana + VictoriaMetrics)

Решение владельца 2026-11-23: процессор по режимам, память, swap, диск, сеть, load и температуры
нод смотреть на графиках. Матрица VPN-проверок и аналог суточного графика — дашборд «VPN» (раздел ниже).

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
NIC, OOM и стойла, температуры. Дашборд «VPN» — вторая запись `CATALOG` (`vpn.json`). Дашборды лежат в
коде (генерируются), папки Grafana = подкаталоги провайдера (`foldersFromFilesStructure`).

## Данные
`data_dir` по умолчанию `/mnt/data/metrics` — Hitachi (надёжный диск), не eMMC и не Seagate-расходник.
Хранение `retention = "1y"`.

## Первый вход
`http://<tailscale-IP alfred>:3000` (порт `grafana_port`); логин `admin` / `admin`, Grafana сразу
попросит сменить пароль. Источник данных и дашборд «Ноды» уже заведены.

## VPN: матрица проверок (решение владельца 2026-11-23)

Только для владельца, через Tailscale. Данные те же, что кладёт в БД служба `vpn` (`vpn_check_states`,
`vpn_link_states`), ничего нового не снимается и расписание проверок не меняется (раунд 15–20 минут, AWG раз в 1–3 ч).

### Экспорт
Каждый сервер со службой `vpn` при `[metrics] export = true` отдаёт `GET /metrics` на tailscale-адресе и порту
`[metrics] vpn_port` (9101). Общий код (рендер строк, `MetricsEndpoint` с повтором привязки раз в 15 с) лежит в
`metrics/exposition.py`, им же пользуется монитор. Рендер: `metrics/vpn.py`, выборка из БД —
`ChecksMixin.render_metrics`. Сервер знает результаты проверок к себе от всех наблюдателей.

Свежесть: строки фильтруются той же `_fresh_clause`, что и остальная логика (`stale_after_s`: AWG —
`awg_check_max_s * 3`, остальное — `(check_interval_s + check_jitter_s) * 3`). Устаревшие ряды не отдаются,
поэтому в Grafana на их месте провал в данных, а не ложный красный. Наблюдатели из `ignored_observers` не отдаются.

| метрика | значение |
|---|---|
| `sa_vpn_check_ok{observer,server,transport,scope,target}` | 1 — последняя проверка цели прошла, 0 — нет |
| `sa_vpn_check_latency_ms{...}` | задержка последней проверки, мс (если есть) |
| `sa_vpn_check_ok_ratio{observer,server,transport,scope}` | доля успешных целей: 1 — работает, 0 — не работает, между — «частично» |
| `sa_vpn_link_level{observer,server,transport,scope}` | устойчивый уровень связки: 2 — работает, 1 — частично, 0 — не работает |

Метки: `observer` — имя наблюдателя; `server` — имя VPN-сервера; `transport` — `reality`, `reality_alt`,
`reality_xhttp`, `awg`, `awg_alt`; `target` — адрес цели; `scope` — `russia` для наблюдателей из
`[vpn] russia_observers` (на проде osip и vanka), иначе `global`. Отдельной константы для «Из России» нет:
охват задаёт конфиг службы, тот же, что у суточного отчёта и алертов. Метку `node` ставит scrape (= сервер).

### Scrape
Отдельный job `sa-home-vpn` на `vpn_port` (`metrics/scrape.py::ScrapeJob`, `vpn_scrape_targets`). Список серверов
берётся из `data/vpn_known_servers.json` (его ведёт бот, `vpn_nodes.known_server_nodes`) в пересечении с
`[swarm].nodes`; если файла нет или он пуст, опрашиваются все пиры роя, лишние остаются `up = 0`. Файл читается при
`nodectl fix` на хабе, новый VPN-сервер подхватывается следующим фиксом.

### Дашборд «VPN» (`metrics/vpn_dashboard.py`, uid `sa-home-vpn`)
- Матрица (status history): ряды «наблюдатель → сервер · способ», значение `ok_ratio`: зелёный 1, красный 0,
  жёлтый «частично».
- Группа «Из России»: та же матрица, `scope="russia"`.
- Аналог суточного графика: доступность по способам и наблюдателям и по серверам (`avg_over_time` на 15 минут),
  задержка, уровень связки.
- Таблица «Сейчас не работает»: цели с `ok = 0`.
- Переменные `observer`, `server`, `transport` — multi, по умолчанию All; период по умолчанию 24 часа.

### Кнопка в боте
Экран «🛰 Проверка сети» (только админ) получает URL-кнопку «📊 Grafana» на `<grafana_public_url>/d/sa-home-vpn`, если
`[metrics] grafana_public_url` не пуст.

### Что включить
- На каждом VPN-сервере: `[metrics] export = true` и перезапуск службы `vpn` (порт `vpn_port`, по умолчанию 9101).
- На alfred (хаб): `[metrics] grafana_public_url = "https://alfred.tail61d88b.ts.net"`, затем `nodectl fix`
  (перепишет scrape и добавит `vpn.json`); публикацию Grafana по этому адресу (Tailscale Serve) настраивает владелец.
