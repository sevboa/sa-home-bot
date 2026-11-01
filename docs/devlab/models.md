# Шлюз моделей `devlab_models`

Этап 60, задача 60.4.1. Служба на ноде с моделями (по экземпляру на ноду, не singleton):
OpenAI-совместимый HTTP-сервер на адресе tailscale0, который проксирует запросы исполнителя к
провайдеру, соблюдает его лимиты, считает токены и $ и раз в несколько секунд сообщает хабу
каталог и остатки лимитов. Код: `devlab/models/`. Вид `llamacpp` (локальный llama.cpp, 60.4.2) — раздел «Локальные модели».

## Устройство
- `gateway.py` — HTTP: `GET /health` (публичный), `GET /v1/models`, `POST /v1/chat/completions`
  (имена путей и заголовка `X-Devlab-Job` — `devlab/protocol.py`). `stream: true` передаётся
  потоком: чанки провайдера пишутся клиенту по мере прихода, ответ целиком не буферизуется.
- `backends.py` — вид модели = отдельный бэкенд с общим интерфейсом `ModelBackend`
  (`chat(payload)` отдаёт ответ провайдера). Реестр `BACKENDS` по `kind`; `llamacpp` — наследник
  `HttpBackend` (общий HTTP-прокси с `openai`). `openai` — `aiohttp`-сессия, `Authorization:
  Bearer <api_key>`, `model` в теле подменяется на `model` из конфига, `proxy` опционален.
- `limits.py` — `RateWindow`: скользящее окно `rpm`.
- `ledger.py` — SQLite `ledger.sqlite` в `data_dir`.
- `hubclient.py` — клиент хаба: heartbeat и introspect.
- `app.py` — `run_models`: сервер, цикл heartbeat. Адрес: `bind_host` либо `tailscale ip -4`
  (`devlab.hub.app.tailscale_address`, та же функция, что у хаба).

## Конфиг
Поля — `docs/platform/config-reference.md` (`DevlabModelsConfig`). Секреты (`hub_token`,
`api_key`) лежат только в instance-конфиге ноды. Таблицы `[devlab_models.<имя>]` разбираются
валидатором `DevlabModelsConfig`: любой ключ-таблица, не совпавший с полем, становится моделью.
Опечатки в полях модели ловит `unknown_config_keys`.

Пример бесплатного OpenRouter-подобного провайдера:

```toml
[devlab_models]
hub_url = "http://alfred.tail61d88b.ts.net:8720"
hub_token = "dlh_..."

[devlab_models.openrouter-free]
kind = "openai"
base_url = "https://openrouter.ai/api/v1"
model = "meta-llama/llama-3.3-70b-instruct:free"
api_key = "sk-or-..."
free = true
rpm = 20
rpd = 200
context = 65536
remote = true
```

`report_cost = true` (только OpenRouter): шлюз добавляет к запросу `usage: {include: true}`, провайдер
присылает фактическую стоимость в `usage.cost`, она идёт в учёт вместо цены из конфига. Строгие API
неизвестное поле могут отвергнуть, поэтому по умолчанию `false`. Для локальной `llamacpp` ключ `--jinja`
кладётся в `extra_args`. Адрес в heartbeat — `http://<bind_host или tailscale0>:<port>`
(`models/app.py`); на jeeves и wooster шлюз слушает tailscale-адрес.

Токен шлюза выпускается на alfred: `nodectl call devlab_hub issue_token name=<имя> role=gateway`.

## Доступ и introspect
Хаб хранит токены только хэшами, поэтому шлюз не может проверить токен сам. Он спрашивает хаб:
`POST /api/introspect` (роль `gateway`) с телом `{"token": "<предъявленный токен>", "address": "<request.remote клиента>"}`, хаб отвечает
`{"name", "role"}` или 404 (неизвестный, отозванный, адрес не из нод токена или не executor: хаб подтверждает только токены исполнителя, чтобы шлюз не стал оракулом для клиентских токенов). Пускается только роль `executor`: нет
токена или хаб его не знает — 401, роль другая — 403, хаб недоступен — 503 (не кэшируется).
Ответ, включая отказ, кэшируется на `INTROSPECT_TTL_S` (120 с) по паре (хэш токена, адрес): отозванный токен
перестаёт работать на шлюзе не позже чем через это время.

## Лимиты
- `rpm`: минутное окно исчерпано, запрос ждёт освобождения слота (не ошибка), очередь по порядку.
- `rpd`: сутки считаются по UTC (как у большинства провайдеров), счётчик берётся из учёта и
  переживает перезапуск. Исчерпан — 429 с текстом «суточный лимит модели … исчерпан», провайдер
  не вызывается. Запрос засчитывается при допуске, включая те, что провайдер отклонил.
- `0` — без лимита.

## Учёт
`usage(day, model, job, requests, tokens_in, tokens_out, cost_usd)` — только агрегаты по
суткам, модели и задаче; тексты запросов не хранятся. Задача — заголовок `X-Devlab-Job`
(пусто, если не передан). Токены берутся из `usage` ответа провайдера; для stream — из последнего
события с `usage`. Для этого шлюз добавляет в запрос `stream_options.include_usage = true`, если
клиент не задал своё; если провайдер usage не отдал, пишется 0. Деньги: если провайдер прислал фактическую стоимость (`usage.cost`, у OpenRouter), берётся она, иначе
`(вход * price_in_per_m + выход * price_out_per_m) / 1e6`. В провайдера уходит запрос без полей
`models` и `route` (`FALLBACK_FIELDS`): иначе OpenRouter мог бы уйти на платные модели-запасные,
о чём ни шлюз, ни исполнитель не узнали бы заранее.

## Heartbeat
Раз в `HEARTBEAT_INTERVAL_S` (четверть `HEARTBEAT_FRESH_S` хаба) `POST /api/heartbeat`:
- `models`: на модель `name`, `kind`, `free`, `price_in`, `price_out`, `context`, `remote`;
  для `llamacpp` ещё `loaded` (поднята ли модель сейчас); `threads` клиенту не нужны и не отдаются;
- `limits`: `{<имя>: {"rpd_left": <остаток или null>}}`;
- `url`: адрес шлюза (`http://<bind_host или tailscale>:<port>`), по нему исполнитель ходит в шлюз; в клиентский `devlab_models` он не попадает.

Одну модель (один ключ провайдера) можно поднять на нескольких шлюзах (jeeves и wooster): каталог хаба покажет по записи на шлюз, исполнитель выбирает живой и переключается при отказе (`docs/devlab/agent.md`, «Шлюз»).

Ошибка сети пишется в журнал, цикл продолжается. Ключи провайдеров не попадают в heartbeat,
ответы, журнал и тексты ошибок (провайдер недоступен — 502 с фиксированным текстом); это проверяет
`tests/unit/test_devlab_models.py`.

## Локальные модели (llamacpp)
Этап 60.4.2. `LlamaCppBackend` (`backends.py`). GPU не используется: аргумент
`--n-gpu-layers 0` фиксирован, GPU днём принадлежит gemma. Ollama не участвует.
- Запуск по требованию: первый запрос поднимает `llama-server` как transient user-юнит
  `systemd-run --user --quiet --collect --unit=sa-home-devlab-model-<имя> --slice=sa-home-devlab.slice
  [taskset -c <cpus>] <llamacpp_bin> --model <gguf> --host 127.0.0.1
  --port <порт> --n-gpu-layers 0 [--ctx-size <context>] [--threads <threads>] <extra_args>`.
  Имена юнита и slice, вес — `devlab/protocol.py` (`MODEL_UNIT_PREFIX`, `DEVLAB_SLICE`,
  `DEVLAB_SLICE_CPU_WEIGHT`, `DEVLAB_SLICE_CPU_WEIGHT_NIGHT`). Вес и заморозка slice, дневные и
  ночные потоки — раздел «Соседство с ботом» ниже.
- Готовность: опрос `GET /health` раз в секунду, таймаут 900 с (`LLAMA_HEALTH_TIMEOUT_S`);
  перед каждым опросом проверяется `systemctl --user is-active`. Запуск идёт под lock, параллельные
  первые запросы ждут один процесс. Перед стартом юнит гасится (хвост прошлого запуска).
- Выгрузка: фоновая проверка раз в 15 с; без запросов дольше `idle_unload_s` юнит останавливается
  (`systemctl --user stop`). Запрос считается активным от приёма до конца ответа, поэтому
  stream не прерывается. При остановке службы модели гасятся.
- Падения: упавший юнит замечается при следующем запросе или проверке и поднимается заново.
  Три падения или неудачных старта за 10 минут — 503 с текстом о времени следующей попытки, без
  бесконечного перезапуска.

Пример для Flint (GLM/Colibri сюда не относится: отдельный бэкенд, не llama.cpp, будет после
замеров стенда):

```toml
[devlab_models]
llamacpp_bin = "/opt/llama.cpp/build/bin/llama-server"

[devlab_models.flint]
kind = "llamacpp"
gguf = "/models/sa-home/devlab/flint.gguf"
context = 32768
threads = 8
threads_night = 12
cpus = "0-5,12-17"
idle_unload_s = 600
```

## Соседство с ботом на CPU (60.6.2, в проде v0.154.x, вживую не проверено)
Решение владельца 2026-10-31: на mycraft распознавание речи, озвучка и картинки бота идут на CPU,
их ждёт человек, поэтому llama.cpp им уступает. Цифры — `/mnt/data/claude/dev/stage60-stand/REPORT.md`,
«Влияние на бота»: без ограничений озвучка замедляется вдвое (21–31 с вместо 12–21), `nice` не
помогает (30–42 с), `CPUWeight=10` с `taskset` на часть ядер возвращает 17–25 с при 5,2–5,5 ток/с модели.

Вес. Вес стоит на самом slice: `systemctl --user set-property --runtime sa-home-devlab.slice
CPUWeight=<вес>` (`models/slice.py::SliceControl`). Вес на юните внутри slice с одним ребёнком ничего
не решает, он делит CPU только с соседями по slice, а не с ботом (служба бота живёт в `app.slice`
того же user-менеджера, вес 100). Поэтому `--property=CPUWeight` у `systemd-run` убран, а вес
slice ставится сразу после успешного `systemd-run` (пустой slice systemd выгружает, свойство
пропало бы) и при каждой смене режима. Днём 10, ночью 100.

Потоки и ядра. Берутся из `[devlab_models.<имя>]`: `threads`, `threads_night`, `cpus`, `cpus_night`.
У `kind = "llamacpp"` умолчания из стенда: `threads = 8`, `threads_night = 12`, `cpus = "0-5,12-17"`,
`cpus_night = ""` (без `taskset`). Умолчания рассчитаны на mycraft (24 логических ядра), на другой
машине их надо переопределить (`cpus = ""` убирает `taskset`). Ночью `threads_night = 0` оставляет
дневные потоки. Потоки и ядра читаются при подъёме `llama-server`: уже запущенный процесс на лету не
перезапускается, новые значения подхватятся при следующей загрузке (после `idle_unload_s`). Вес
slice меняется на лету.

Пауза на живую задачу бота.
- Служба `llm` на время `transcribe_voice`, `synthesize_speech`, `generate_image` и `item_portrait`
  шлёт локальные события `llm_cpu_task_started` и `llm_cpu_task_finished` (`task_id`, `kind`:
  `stt`/`tts`/`image`); пока команда идёт, `get_state` отдаёт её id в `cpu_tasks`.
- Шлюз (только если есть хоть одна `llamacpp`-модель) держит соединение с `[llm].socket` (или
  `[devlab_models].neighbor_socket`) и подписан на события (`devlab/neighbor.py::follow_llm`). По
  первому `started` — `systemctl --user freeze sa-home-devlab.slice`, по `finished` последней
  задачи — `systemctl --user thaw`. Замораживается только slice моделей: служба шлюза, сам HTTP и
  службы бота не затрагиваются.
- Вложенные задачи: счёт по `task_id`, разморозка только когда множество пусто.
- Страховки: `freeze_max_s` (300 с) размораживает, если `finished` не пришёл (после этого поздний
  `finished` безвреден, а новый `started` замораживает заново); обрыв соединения с `llm` тоже
  размораживает, потому что события могли потеряться; при подключении состояние берётся из
  `get_state`, а не из потока событий.
- Заморозка не обрывает запрос клиента: ответ просто задерживается. Чтение апстрима ждёт данные до
  300 с (`UPSTREAM_TIMEOUT.sock_read`), столько же по умолчанию `freeze_max_s`, поэтому очень
  долгая заморозка посреди стрима может закончиться таймаутом чтения. Если на проде такое
  случится, уменьшить `freeze_max_s`.

Ночной режим. Пока Альфред в отлучке (`away`), у бота нет живых CPU-задач.
- Состояние `away` живёт в БД бота (alfred), на mycraft его не видно. `AwayRunner` (раз в минуту)
  шлёт службе `llm` команду `set_night_mode {night}` (адрес `LLM_ADDRESS` через ноду), `night`
  истинно только в фазе `away` (не в `returning`); повтор раз в 300 с, пока ночь, и после каждого
  сбоя доставки, чтобы перезапуск `llm` не терял режим.
- `llm` хранит флаг, отдаёт его в `get_state` и по смене шлёт `llm_night_mode`. Шлюз по событию
  меняет вес slice на 100 и переключает `threads_night`/`cpus_night` для следующего подъёма; по
  `away back` возвращает вес 10 и дневные потоки.
- Если mycraft выключен или `llm` недоступна, команда не доходит, режим остаётся прежним (дневным).

## Не сделано
Выкат (назначение службы на jeeves/wooster, токены, instance-конфиги) — за владельцем, чек-лист в
[deploy.md](deploy.md).
