# Исполнитель `devlab`

Этап 61.2a (2026-11-01): ядро исполнителя для заданий `test`, без контейнера. Служба `devlab`
(`protocol.EXECUTOR_SERVICE`, runner `sa_home_bot.devlab.executor.app:run_executor`), назначается
на mycraft. Контейнерный запуск команды — 61.2b; агентский слот (60.5) — позже.

## Устройство
- `executor/app.py` — `run_executor`: конфиг `[devlab]`, `ExecutorHub`, `TestWorker`, останов по сигналу.
  `build_runner` пока бросает `RuntimeError` (контейнерного `TestRunner` нет): службу нельзя запустить
  без реализации, чтобы чужой код не исполнялся на хосте без изоляции.
- `executor/worker.py` — `TestWorker`: три фоновых цикла (heartbeat, poll, claim) и обработка задания.
- `executor/hub.py` — `ExecutorHub`: `claim`, `post`, `poll`, `upload_log` поверх `devlab/hubhttp.HubHttp`
  (bearer, базовый URL, heartbeat; тот же базовый класс у `models/hubclient.HubClient` шлюза).
- `executor/mirror.py` — `ProjectMirror`: bare-кэш проекта и работа с git хаба.
- `executor/spec.py` — разбор `.devlab.toml`; `executor/report.py` — сводка из junit и текста pytest.
- `executor/runner.py` — интерфейс `TestRunner` (`run(RunRequest) -> RunResult`, `cancel(job_id)`).
  В тестах — `FakeTestRunner` из `tests/unit/support/fakes.py`.

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
| `image` | строка | образ контейнера — задел для 61.2b, сейчас не используется |
| `setup` | список строк | команды подготовки окружения — задел для 61.2b, сейчас не используются |

Другие разделы (`[agent]` и т. д.) исполнитель `test` игнорирует. В sa-home-bot файл лежит в корне:
`python -m pytest -q` (pytest-xdist в dev-зависимостях нет, `-n` не добавляется).

## Конфиг
Таблица `[devlab]`: `hub_url`, `hub_token`, `work_dir`, `poll_interval_s`, `heartbeat_interval_s`
(см. `docs/platform/config-reference.md`, `DevlabConfig`). Токен — только в instance-конфиге ноды.

## Тесты
`tests/unit/test_devlab_executor.py` (настоящий хаб `build_hub_app`, bare-репо, `FakeTestRunner`) и
`tests/unit/test_devlab_executor_report.py` (разбор итогов, `.devlab.toml`).
