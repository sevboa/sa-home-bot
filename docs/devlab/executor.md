# Исполнитель `devlab`

Этап 61.2a (2026-11-01): ядро исполнителя для заданий `test`. Этап 61.2b (2026-11-01): контейнерный
`PodmanTestRunner`. Служба `devlab` (`protocol.EXECUTOR_SERVICE`, runner
`sa_home_bot.devlab.executor.app:run_executor`), назначается на mycraft. Агентский слот (60.5) — позже.

## Устройство
- `executor/app.py` — `run_executor`: конфиг `[devlab]`, `ExecutorHub`, `TestWorker`, останов по сигналу.
  `build_runner` возвращает `PodmanTestRunner`: чужой код исполняется только в контейнере.
- `executor/worker.py` — `TestWorker`: три фоновых цикла (heartbeat, poll, claim) и обработка задания.
- `executor/hub.py` — `ExecutorHub`: `claim`, `post`, `poll`, `upload_log` поверх `devlab/hubhttp.HubHttp`
  (bearer, базовый URL, heartbeat; тот же базовый класс у `models/hubclient.HubClient` шлюза).
- `executor/mirror.py` — `ProjectMirror`: bare-кэш проекта и работа с git хаба.
- `executor/spec.py` — разбор `.devlab.toml`; `executor/report.py` — сводка из junit и текста pytest.
- `executor/runner.py` — интерфейс `TestRunner` (`run(RunRequest) -> RunResult`, `cancel(job_id)`).
  В тестах — `FakeTestRunner` из `tests/unit/support/fakes.py`.
- `executor/podman.py` — `PodmanTestRunner`: контейнерный запуск через обёртку 60.1 (см. ниже).

## Цикл
1. heartbeat каждые `heartbeat_interval_s`: `{busy, jobs}`; `busy` истинно, пока есть текущее задание.
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

Другие разделы (`[agent]` и т. д.) исполнитель `test` игнорирует. В sa-home-bot файл лежит в корне:
`python -m pytest -q` (pytest-xdist в dev-зависимостях нет, `-n` не добавляется).

## Конфиг
Таблица `[devlab]`: `hub_url`, `hub_token`, `work_dir`, `poll_interval_s`, `heartbeat_interval_s`,
`test_image`, `test_cpuset`, `test_cpus`, `test_memory`, `build_timeout_s`
(см. `docs/platform/config-reference.md`, `DevlabConfig`). Токен — только в instance-конфиге ноды.

## Тесты
`tests/unit/test_devlab_executor.py` (настоящий хаб `build_hub_app`, bare-репо, `FakeTestRunner`) и
`tests/unit/test_devlab_executor_report.py` (разбор итогов, `.devlab.toml`),
`tests/unit/test_devlab_podman_runner.py` (раннер с фейковым запуском обёртки: argv, кэш образа,
таймаут, cancel, очистка) и `tests/unit/test_devlab_wrapper_script.py` (настоящий текст обёртки в `sh`
с фейковым `podman`: валидация `cpuset` и имени файла `sync-out`).
