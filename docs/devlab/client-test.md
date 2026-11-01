# Клиент тестов: снимок и скил `devlab-test` (61.3, 2026-11-01)

Прогон тестов своей рабочей копии на mycraft с любого компьютера с нодой роя.

## `nodectl devlab snapshot`
Код: `devlab/snapshot.py`, подкоманда рядом с `connect`. Флаги: `--project`, `--client`.

1. Корень репозитория по `git rev-parse --show-toplevel` (из любого подкаталога). Нет `HEAD`:
   ошибка «в репозитории нет коммитов».
2. Временный коммит: каталог `tempfile`, `GIT_INDEX_FILE` в нём, `read-tree HEAD`, `add -A`,
   `write-tree`, `commit-tree -p HEAD` (автор `devlab <devlab@localhost>`). Ветки, настоящий индекс,
   файлы и stash не меняются; в коммит попадают staged, unstaged и неотслеживаемые файлы, игнорируемые
   (`.gitignore`) нет. Объекты остаются в базе репозитория недостижимыми (уберёт `git gc`).
3. Push: `git push --no-verify <hub>/git/<проект>.git <sha>:refs/heads/test/<клиент>/<n>`, `<n>` =
   `<unix-время>-<7 символов sha>`. URL хаба как у `connect` (`http://<[devlab_hub].hub_node>:<port>`),
   токен из `~/.config/sa-home-bot/devlab-token`. Токен передаётся только окружением
   `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=http.extraHeader`, `GIT_CONFIG_VALUE_0` (в argv его нет);
   в тексте ошибок git он заменяется на `***`.
4. stdout: одна строка, ветка (`test/alfred/1762000000-1a2b3c4`), её передают в `devlab_test ref=`;
   пояснения и ошибки идут в stderr, код выхода 1 при ошибке.

### Решения
- Имя клиента = имя токена = имя ноды (`[node].id`, иначе hostname, как у хаба). Не совпало — хук
  отклонит push (`test/<чужое>/*` закрыт); тогда `--client <имя ноды в рое>`.
- Имя проекта: `--project`, иначе `project = "…"` в корне `.devlab.toml`, иначе имя каталога
  репозитория. В worktree имя каталога случайное, поэтому в sa-home-bot задано `project = "sa-home-bot"`
  (поле `project` читает `executor/spec.py`, исполнитель его игнорирует).
- Первый push тянет всю историю, дальше только новое.

## Скил `devlab-test`
Один источник: `src/sa_home_bot/devlab/skills/devlab-test/SKILL.md` (включён в пакет через
`package-data`). Копии:
- `.claude/skills/devlab-test` в репозитории — симлинк на каталог источника (не копия). На Windows
  без `core.symlinks` симлинк станет текстовым файлом, там работает копия из пункта ниже;
- `nodectl devlab connect` ставит или обновляет `~/.claude/skills/devlab-test/SKILL.md` из пакета
  (`install_skill`: `installed` / `updated` / `current`, правка пользователя перезаписывается).
  Так скил попадает на любой компьютер, где выполнен `connect`, без правок пользовательских
  настроек Claude Code.

## Предел ожидания `devlab_test wait` (сверено с документацией Claude Code, 2026-11-01)
- Для HTTP-серверов MCP действует idle-тайм-аут: вызов без ответа и без progress-уведомлений 5 минут
  обрывается (`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`, мс, `0` отключает). Хаб progress не шлёт.
- Общий предел вызова: `MCP_TOOL_TIMEOUT` или `timeout` сервера (мс); по умолчанию около 28 часов, но
  `timeout` сервера ещё и жёсткий предел, progress его не продлевает.
- Вызов, идущий дольше 2 минут, в основной сессии уходит в фоновую задачу
  (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`); лимиты остаются.
- Вывод: рекомендуемый `wait` — не больше 240 секунд (ниже 5 минут с запасом). Дальше опрос
  `devlab_status id=<id>` или `devlab_inbox` (сообщение `test_done`). Потолок хаба 1800 с остаётся для
  клиентов с поднятым idle-тайм-аутом. `connect` печатает эту подсказку, настройки пользователя не
  меняет.

## Адрес хаба на ноде хаба
`connect.hub_url`: если `hub_node` совпадает с id этой ноды (`node.id` или hostname), берётся адрес
`tailscale ip -4`, иначе имя `hub_node`. Причина (2026-11-01): на alfred имя `alfred` резолвится в
`127.0.1.1`, а хаб слушает только tailscale-адрес. Нет tailscale: остаётся имя. Тест
`test_hub_url_on_the_hub_node_uses_the_tailscale_address`.

## Тесты
`tests/unit/test_devlab_snapshot.py`: снимок не меняет HEAD, ветку, индекс, файлы, stash;
коммит содержит staged/unstaged/новые файлы без игнорируемых; репозиторий без коммитов; имя
проекта; push в настоящий bare-репозиторий через `build_hub_app`; токен не в argv и не в ошибках;
чужое пространство `test/*`; установка скила и совпадение репозиторного симлинка с пакетным
источником.

Не проверено: живой прогон (см. Этап 61.3 в `IMPLEMENTATION_PLAN.md`).
