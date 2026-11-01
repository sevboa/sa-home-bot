# Исполнитель `devlab`

Этап 61.2a (2026-11-01): ядро исполнителя для заданий `test`. Этап 61.2b (2026-11-01): контейнерный
`PodmanTestRunner`. Служба `devlab` (`protocol.EXECUTOR_SERVICE`, runner
`sa_home_bot.devlab.executor.app:run_executor`), назначается на mycraft. Агентский слот — `AgentWorker` (60.5.4, `agent.md`); с `TestWorker` их роднит `WorkerLoops`.

## Устройство
- `executor/app.py` — `run_executor`: конфиг `[devlab]`, `ExecutorHub`, `TestWorker` и `AgentWorker` рядом
  (каждый в своей задаче со своими poll и claim), один общий heartbeat (`beat_forever`), останов по сигналу.
  `build_runner` возвращает `PodmanTestRunner`: чужой код исполняется только в контейнере.
- `executor/worker.py` — `TestWorker`: три фоновых цикла (heartbeat, poll, claim) и обработка задания.
- `executor/hub.py` — `ExecutorHub`: `claim`, `post`, `poll`, `upload_log` поверх `devlab/hubhttp.HubHttp`
  (bearer, базовый URL, heartbeat; тот же базовый класс у `models/hubclient.HubClient` шлюза).
- `executor/mirror.py` — `ProjectMirror`: bare-кэш проекта и работа с git хаба.
- `executor/spec.py` — разбор `.devlab.toml`; `executor/report.py` — сводка из junit и текста pytest.
- `executor/runner.py` — интерфейс `TestRunner` (`run(RunRequest) -> RunResult`, `cancel(job_id)`).
  В тестах — `FakeTestRunner` из `tests/unit/support/fakes.py`.
- `executor/wrapper.py` — общее для контейнерных исполнителей (60.5.3): `WrapperClient` (вызов обёртки через
  `sudo -n -u devlab`, `checked`, `exec` с запасом `EXEC_GRACE_S`, `create`, `remove_container`, `cleanup`,
  отправка дерева `push_tree`), `pack_directory` (с фильтром путей), `extract_files`, `job_name`, `RunnerError`.
- `executor/image.py` — образ по рецепту: `ImageRecipe`, `containerfile`, `image_tag`, `ensure_image` (кэш по
  хэшу, сборка с сетью). Один код для `PodmanTestRunner` и `PodmanSandbox`.
- `executor/podman.py` — `PodmanTestRunner`: контейнерный запуск через обёртку 60.1 (см. ниже).
- `executor/agent_sandbox.py` — `PodmanSandbox`, контейнер задачи агента (`agent.md`).

## Цикл
1. heartbeat каждые `heartbeat_interval_s`: `{busy, jobs}`; `busy` истинно, пока есть текущее задание
   любого работника (слоты и общий heartbeat — ниже).
2. claim с `kinds=["test"]` (слот test; agent не берём), `models=[]`; пусто: пауза `poll_interval_s`.
3. Задание, по порядку:
   1. `git fetch` всех веток хаба в bare-зеркало `<work_dir>/mirrors/<проект>.git` (`+refs/heads/*:refs/hub/*`,
      `--prune`); `ref` — ветка или sha, резолвится в коммит;
   2. `.devlab.toml` читается из этого коммита (`git show`); нет файла — значения по умолчанию;
   3. `git archive` коммита в `<work_dir>/jobs/<id>`;
   4. команда: `cmd` задания, иначе `[test] cmd`; аргументы: `args` задания, иначе `[test] args`
      (задание заменяет, не дополняет); если среди первых трёх слов есть `pytest`, добавляется
      `--junitxml=.devlab-junit.xml`; запуск через `TestRunner` с таймаутом `[test] timeout_s`;
   5. итоги: junit-xml (если есть и в нём есть `testcase`), иначе последняя строка pytest
      (`N passed, M failed, ...`) и строки `FAILED`/`ERROR` короткой сводки; до 50 упавших, выдержка
      до 500 символов; хвост вывода 4 КиБ; таймаут — `exit_code` 124 и метка в начале хвоста;
   6. `PUT /api/log` для `output.log` и `junit.xml`; затем `post test_done`;
   7. ветка `test/*` на хабе удаляется (`git push :refs/heads/<ref>`), только если `ref` начинается с
      `test/`; ветки `base/`, `agent/` и sha не трогаются;
   8. каталог задания удаляется.
4. Любое исключение исполнителя (git, `.devlab.toml`, runner, хаб) — `post failed` с текстом
   (ограничен 1000 символов, токен вырезается); цикл продолжается, ветка `test/*` всё равно
   удаляется.
5. poll: сообщение `stop` по текущему заданию вызывает `runner.cancel(job_id)`; по возвращении `run`
   исполнитель шлёт `stopped` (хаб уже перевёл задание в `stopped`), `test_done` не шлётся.
6. Состояние `<work_dir>/state.json`: `cursor` (poll переживает рестарт, прочитанное не разбирается
   повторно) и `current` (задание в работе). На старте непустой `current` — исполнитель умер на
   прогоне: задание закрывается `failed` «исполнитель перезапущен во время прогона».

## Слоты и heartbeat (60.5.7)
- Слот test постоянный: `TestWorker` берёт только `kind=test` и не ждёт агента. Слот agent один
  (`AgentWorker` держит одну задачу и свои `parked`); параллельных агентов нет: состояние, outbox и
  каталог задач `AgentWorker` рассчитаны на одно активное задание. Второй агентский слот потребует
  отдельных экземпляров работника со своим состоянием и общей очередью claim — не сделано.
- Heartbeat один на службу (хаб хранит запись на имя токена, два heartbeat перезаписывали бы друг
  друга): `busy` истинно, если занят любой работник, `jobs` — объединение их текущих заданий
  (`WorkerLoops.jobs`, `heartbeat_body`). Работники запускаются с `heartbeat=False`.
- Состояний два, и они не сливаются: `state.json` (курсор poll и `current` test) и
  `agent-state.json` (курсор, задание, parked, расход агента), плюс `agent-outbox.json`. Курсоры
  независимы: каждый работник читает ящик исполнителя сам и разбирает только свои сообщения.
- Каталог моделей и клиент шлюза (`ModelCatalog`, `GatewayModels`) создаются в `run_executor`,
  токен у них тот же, что у хаба. Лимиты агента из `[devlab] agent_max_*`; модели в настройках нет.
- Выкат: [deploy.md](deploy.md).

## Контейнерный запуск (61.2b)
`PodmanTestRunner.run` вызывает обёртку `sudo -n -u <devlab.user> /usr/local/libexec/sa-home-devlab …`
(`utils/process.run`, тесты подменяют `spawn`). Имя задания в обёртке — id задания в нижнем регистре
(`[a-z0-9-]`, иначе `RunnerError`). Шаги:
1. `sync-in <id>`: каталог задания пакуется в tar (в памяти) и уходит в stdin; обёртка кладёт его в
   `~devlab/work/<id>`.
2. Образ: `image` из `.devlab.toml` (пусто — `[devlab] test_image`) + `setup`/`setup_files`/`env`.
   Тег `devlab/t<24 hex sha256>`; хэш берётся от версии формата, текста Containerfile и sha256 каждого
   `setup_files`. `images` показывает, что образ уже есть: тогда сборка пропускается, иначе
   `build <id> <тег>` (Containerfile со stdin, контекст — копия задания, СЕТЬ ПРИ СБОРКЕ ЕСТЬ, таймаут
   `build_timeout_s`). Containerfile: `FROM`, `WORKDIR /setup`, `ENV`, `COPY` файлов в `/setup/`, по
   `RUN ["sh","-c",<команда>]` на строку `setup` (JSON-кавычки, команда не может выйти за свою
   инструкцию), `WORKDIR /work`. Имя образа и ключи `env` проверяются, `setup_files` — только
   относительные пути внутри кода задания. Ошибка сборки — `RunnerError` с хвостом вывода, задание
   закрывается `failed`.
3. `rm` (на случай хвоста прошлого запуска) и `create <id> <тег> <cpus> <память> <cpuset>`: без сети,
   `--cpus`, `--memory`, `--cpuset-cpus` из `[devlab]` (`test_cpus`, `test_memory`, `test_cpuset`).
4. `exec <id> <ceil(timeout_s)> <shlex.join(argv)>`, вывод stdout+stderr вместе. `timeout` внутри
   контейнера даёт код 124 (это `timed_out`); если обёртка не вернулась за `timeout_s + 30 с`, процесс
   убивается, контейнер удаляется, `timed_out=True`.
5. `sync-out <id> .devlab-junit.xml`: обёртка отдаёт tar одного файла, исполнитель извлекает только
   регулярный файл с этим именем (до 32 МиБ) в каталог задания. Нет файла — не ошибка.
6. Всегда (`finally`, под `asyncio.shield`): `rm` контейнера и `clean` рабочей копии у devlab.

`cancel(job_id)`: помечает прогон отменённым, `rm` контейнера, отменяет текущий шаг и ждёт его конца.
`run` возвращает `exit_code=130`; `sync-out` в этом случае не делается. Отмена самой корутины `run`
(останов службы) пробрасывается, но контейнер всё равно удаляется.

Тег образа в кэше живёт, пока его не снимет `prune`/ручная чистка: автоматической уборки старых
образов нет (открыто). Для sa-home-bot `setup` ставит git и зависимости из `pyproject.toml`
(`setup_files = ["pyproject.toml"]`), `env.PYTHONPATH=/work/src` вместо установки самого пакета, поэтому
правка кода не пересобирает образ.

## git и токен
Адрес — `<hub_url>/git/<проект>.git`, токен роли `executor`. Заголовок передаётся не аргументами, а
окружением процесса: `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=http.extraHeader`,
`GIT_CONFIG_VALUE_0=Authorization: Bearer <токен>`; в argv токена нет. Окружение процесса видно только
владельцу и root. Ошибки git и хаба перед записью в журнал и в `failed` очищаются от токена. Имя
проекта проверяется (`[A-Za-z0-9][A-Za-z0-9._-]*`), чтобы оно не выходило за каталог зеркал.

## `.devlab.toml`
Файл в корне проекта, читается из коммита задания. Раздел `[test]` (неизвестные ключи — ошибка):

| поле | тип | значение |
|---|---|---|
| `cmd` | строка | команда по умолчанию (разбор `shlex`), например `python -m pytest -q` |
| `args` | список строк | аргументы по умолчанию, если у задания `args` пусты |
| `timeout_s` | число > 0 | таймаут прогона, по умолчанию 1800 |
| `image` | строка | базовый образ; пусто — `[devlab] test_image` |
| `setup` | список строк | команды подготовки окружения (RUN при сборке, сеть есть) |
| `setup_files` | список строк | файлы кода, доступные `setup` в `/setup/` и входящие в ключ кэша образа |
| `env` | таблица | переменные окружения образа (`[test.env]`) |

Верхнеуровневое `project = "<имя>"` — имя проекта на хабе для `nodectl devlab snapshot`
([client-test.md](client-test.md)), исполнитель его не использует.

Раздел `[agent]` исполнитель `test` не читает (разбирается, но не используется; [agent.md](agent.md)), прочие неизвестные разделы игнорируются. В sa-home-bot файл лежит в корне:
`python -m pytest -q` (pytest-xdist в dev-зависимостях нет, `-n` не добавляется).

## Признак «занят» и сон mycraft (60.6.1, 2026-11-01)
Пока исполнитель занят, нода не гасит машину по простою (`maybe_auto_poweroff_idle`) и не шлёт
`idle_power_blocked`: владельцу не пишут о том, чего он не просил.

Схема:
1. Исполнитель поднимает локальный ProtoServer (`[devlab].socket`, по умолчанию `./data/devlab.sock`,
   `endpoint_attr="devlab.socket"` в реестре служб) и в `hello`/`describe` отдаёт `ServiceInfo.busy`.
   Поле в payload появляется только при `true`; у старых служб его нет, это «не занят».
2. `PeerLink` ноды к локальной службе читает `busy` из `hello` при подключении и в каждом heartbeat
   (раз в 5 с); при обрыве связи сбрасывает в `false` (мёртвая служба машину не держит).
3. `NodeRouter.busy_services()` перечисляет живые занятые локальные службы; `maybe_auto_poweroff_idle`
   при непустом списке пишет строку в журнал и выходит. Механизм общий: любая служба ноды может
   отдать `busy`, devlab лишь первая. Условие простоя (`llm_went_idle`) не повторяется само: если
   исполнитель освободился позже, машина погаснет при следующем простое llm.

Исполнитель занят (`devlab/executor/busy.py::BusyTracker`), если верно хоть одно:
- есть задача `running` (test или agent): `started` при взятии, `finished` при `test_done`/`done`/
  `failed`/`stopped`. Трекер один на оба воркера и общий heartbeat: `send_heartbeat` кладёт `queued`
  из ответа хаба в тот же `BusyTracker`;
- хаб сообщает непустую очередь: ответ на heartbeat исполнителя содержит `queued` (число заданий в
  `queued`; расширение 60.6.1, старый хаб без поля даёт 0);
- есть задача в `waiting_owner` и с момента `question` не прошло `[devlab].owner_wait_s` (3600 с);
  `question` ставит только агент (`test` не спрашивает), время вопроса лежит в `agent-state.json`
  (`parked[<id>].asked_at`) и перезапуск службы его не обнуляет; `answer` переводит задачу в
  «выполняется» (в том числе после рестарта), `resume`/`stop`/закрытие парковки снимают удержание;
- `outbox_size()` больше нуля: пока сообщения агента из `agent-outbox.json` не дошли до хаба, машину
  не усыпить (`AgentWorker` присваивает `BusyTracker.outbox_size` при создании; у `test` outbox нет,
  его сообщения уходят сразу или логируются).

`devlab_release` (MCP хаба, сообщение `release` в `poll`) снимает удержание по задаче: исполнитель
вызывает `released` (агент помечает запись `parked` как `released`, чтобы рестарт не вернул удержание). Хаб принимает `release` только для `waiting_owner`, это сообщение не будит mycraft
(`wake.md`). Перед отпусканием клиент проверяет, что всё уже в хабе: push `wip`, сообщения. `answer`
переводит задачу в удержание «выполняется», `resume` и `stop` тоже снимают ожидание. Освободившийся по
`owner_wait_s` исполнитель задачу не теряет: хаб разбудит mycraft ответом (`wake.md`).

## Соседство с ботом на CPU (60.6.2, код без выката)
Контейнеры тестов и агента уступают живым CPU-задачам бота (распознавание, озвучка, картинки).
Шлюз моделей делает то же для llama.cpp, подробности и цифры стенда в [models.md](models.md).

Вес. Контейнеры запускает пользователь `devlab` через sudo-обёртку и rootless podman, они не попадают
в user-slice пользователя ноды. Под cgroup v2 `--cpu-shares`/`--cpu-weight` у `podman create`
делят CPU только между контейнерами одного `devlab` (соседи по `user@<uid>.service`), с ботом они не
соревнуются: сравниваются вески `user-<uid_devlab>.slice` и `user-<uid_ноды>.slice` (оба под
`user.slice`, по умолчанию 100). Поэтому вес поставлен на слайс пользователя: фикс `devlab-cpu-weight`
кладёт `/etc/systemd/system/user-<uid>.slice.d/50-sa-home-devlab.conf` (`[Slice] CPUWeight=10`) и
делает `daemon-reload`. Флаги `--cpu-shares` у `create` не добавлены: они были бы пустым действием.
Это решение выведено из семантики cgroup v2, на живой машине не проверено (см. «Проверить вживую» в
[deploy.md](deploy.md)). Ночью вес контейнеров не повышается: тесты ночью работают на своих ядрах `6-11,18-23`, а
вес сказывается только при нехватке CPU.

Ядра. Контейнеры тестов `test_cpuset = "6-11,18-23"`, агента `agent_cpuset = "6-11"` (как и раньше);
дневные ядра шлюза `0-5,12-17` с ними не пересекаются.

Пауза. `run_executor` создаёт `devlab/neighbor.py::NeighborGate` и подписывается на события `llm`
(`[devlab].neighbor_socket`, по умолчанию `[llm].socket`; нет сокета, цикл молча повторяет раз в 5 с).
Первое `llm_cpu_task_started` вызывает `WrapperClient.pause()`: подкоманда обёртки `pause` без имени
ставит на паузу все запущенные контейнеры `devlab-*` (`podman pause`), последнее
`llm_cpu_task_finished` вызывает `unpause`. Вложенность, `freeze_max_s` (300 с, `[devlab]`) и
обрыв связи работают как у шлюза. Подробности и поведение при потере событий в models.md.

Тонкости паузы контейнеров:
- `podman exec` в приостановленный контейнер не работает, поэтому `exec`, `start`, `stop` и
  `interrupt` ждут разморозки (`NeighborGate.wait_thawed`), а контейнер, созданный в заморозку,
  сразу ставится на паузу.
- Таймаут команды считается без времени заморозки. Внутриконтейнерный `timeout` считает по стенным
  часам и счётчик паузы не знает, поэтому теперь он только страховка (`exec_backstop`: лимит ×2 + 30 с),
  а срок следит `WrapperClient.exec` на хосте: `потрачено = стена − (frozen_total сейчас − frozen_total на старте)`.
  Когда срок вышел, подкоманда `interrupt` шлёт `TERM`, через 2 с `KILL` всем процессам контейнера
  (кроме PID 1), `exec` завершается, а вывод сохраняется. Результат: `exit_code 124`, `timed_out`.
  Прежний путь «убили контейнер, вывода нет» остаётся для случая, когда `interrupt` не помог за
  `EXEC_GRACE_S`.
- Не вычитается: `agent_max_wall_s` (потолок всей задачи агента, 8 ч), `build_timeout_s` и
  `SYNC_TIMEOUT_S` (сборка и синхронизация не внутри контейнера, не замораживаются).

Подкоманды обёртки (`/usr/local/libexec/sa-home-devlab`, ставит `nodectl fix`):
- `pause [имя]` и `unpause [имя]`: имя проверяется как у остальных (`[a-z0-9-]`), без имени
  выбираются `podman ps --filter name=devlab- --filter status=running|paused`, пустой список не ошибка;
- `interrupt имя`: `podman exec devlab-<имя> sh -c 'kill -TERM -1; sleep 2; kill -KILL -1'`.

## Конфиг
Таблица `[devlab]`: `socket`, `hub_url`, `hub_token`, `work_dir`, `poll_interval_s`, `heartbeat_interval_s`,
`owner_wait_s`, `freeze_max_s`, `neighbor_socket`, `test_image`, `test_cpuset`, `test_cpus`, `test_memory`, `build_timeout_s`, `agent_cpuset`, `agent_cpus`,
`agent_memory`, `agent_max_steps`, `agent_max_wall_s`, `agent_max_red_runs`, `agent_max_cost_usd`,
`agent_max_tokens`, `agent_context_tokens`, `agent_reply_tokens` (см. `docs/platform/config-reference.md`, `DevlabConfig`). Токен — только в instance-конфиге ноды.

## Тесты
`tests/unit/test_devlab_executor.py` (настоящий хаб `build_hub_app`, bare-репо, `FakeTestRunner`) и
`tests/unit/test_devlab_executor_service.py` (сквозной: `run_executor` с настоящим хабом, двумя настоящими
шлюзами на фейковом апстриме, настоящим репозиторием; клиент через MCP до `agent/<id>` и ящика, `test` проходит,
пока агент занят, отказ одного шлюза посреди задачи) и `tests/unit/test_devlab_executor_report.py` (разбор итогов, `.devlab.toml`),
`tests/unit/test_devlab_neighbor.py` (заморозка по событиям, вложенность, `freeze_max_s`, вычет паузы),
`tests/unit/test_devlab_wrapper_pause.py` (таймаут без паузы, `interrupt`, ожидание разморозки),
`tests/unit/test_devlab_podman_runner.py` (раннер с фейковым запуском обёртки: argv, кэш образа,
таймаут, cancel, очистка), `tests/unit/test_devlab_agent_sandbox.py` (песочница агента) и `tests/unit/test_devlab_wrapper_script.py` (настоящий текст обёртки в `sh`
с фейковым `podman`: валидация `cpuset` и имени файла `sync-out`).
