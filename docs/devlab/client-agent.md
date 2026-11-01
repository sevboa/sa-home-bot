# Клиент агента: скил `devlab` и подключение (60.7, 2026-11-01)

Скил `devlab` ведёт Claude по циклу «задача агенту на mycraft, ящик, приёмка». Рядом работает скил
`devlab-test` ([client-test.md](client-test.md)). Шаблоны `.devlab.toml` для других проектов:
[devlab-toml.md](devlab-toml.md).

## Скил
Один источник: `src/sa_home_bot/devlab/skills/devlab/SKILL.md` (в пакете через `package-data`,
глоб `devlab/skills/*/SKILL.md`). Копии:
- `.claude/skills/devlab` в sa-home-bot — симлинк на каталог источника;
- `nodectl devlab connect` ставит оба скила (`devlab-test`, `devlab`) в `~/.claude/skills`
  (`installed` / `updated` / `current`);
- для проекта на компьютере без ноды роя: скопировать `SKILL.md` вручную в
  `<проект>/.claude/skills/devlab/SKILL.md` или `~/.claude/skills/devlab/SKILL.md`.

### Сценарий скила
1. task.md по шаблону `devlab://task-template`: цель, контекст, файлы, шаги, критерии приёмки,
   тесты, ограничения (с напоминанием правил проекта: агент слабее Claude и оставляет комментарии),
   бюджет. Задачи режутся мелко и механично.
2. Модель из `devlab_models`, по умолчанию бесплатная; платная только с согласия владельца и
   потолком в долларах.
3. Показ задачи и модели владельцу, ожидание подтверждения.
4. `nodectl devlab base <имя>` (push `base/<имя>`), затем `devlab_submit`.
5. При каждом возвращении к работе: `devlab_inbox` и `devlab_ack`, ответы через `devlab_answer`,
   `devlab_resume` (замечания, другая модель), `devlab_stop`, `devlab_release`. Ожидание вне MCP:
   `nodectl devlab wait <id>` фоновым вызовом Bash (останавливается и на `waiting_owner`), затем
   `devlab_status` Последняя строка stdout `wait` — JSON `{"id","status","result"?}`. (2026-11-01: `sleep 60` в Bash блокируется, долгий MCP-вызов обрывается).
6. Приёмка: `nodectl devlab fetch <id>`, ревью диффа по правилам проекта,
   `devlab_test ref=agent/<id> wait=0` (новое test-задание, свой `job.id`) + `nodectl devlab wait <job.id>` + `devlab_status id=<job.id>`, перенос в `dev/<id>` в отдельном worktree (коммиты поимённо), отчёт,
   `nodectl devlab drop agent/<id> base/<имя>`.
7. Слияний в основную ветку и выката без явной команды владельца нет.

## Команды `nodectl devlab` (код `devlab/snapshot.py`, `branch_command`)
Токен берётся из `~/.config/sa-home-bot/devlab-token` и передаётся git только окружением
`GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0=http.extraHeader`, `GIT_CONFIG_VALUE_0` (в argv его нет, в
тексте ошибок он заменяется на `***`). Проект: `--project`, иначе `project` из `.devlab.toml`, иначе
имя каталога. Результат в stdout, пояснения и ошибки в stderr, код 1 при ошибке.

| команда | что делает |
|---|---|
| `base <имя> [--ref R]` | пушит коммит `R` (по умолчанию `HEAD`) в `base/<имя>` с `--force`; печатает ветку. Имя: a-z, 0-9, `.`, `-`, `_` |
| `fetch <id>` | забирает `agent/<id>` в локальный `refs/devlab/agent/<id>`, печатает ref; рабочую копию и ветки не трогает |
| `drop <ветка>...` | удаляет на хабе `agent/*` и `base/*` (других имён команда не принимает) |
| `snapshot` | см. [client-test.md](client-test.md) |

Права ролей: [git.md](git.md) (клиенту разрешены `base/*`, удаление `agent/*`, `test/<свой>/*`).

## Подключение вручную (если `connect` не подходит)
`connect` делает всё ниже сам: просит у хаба токен роли client по `POST /connect` с
`[swarm].token`, пишет его в файл (права 600), ставит скилы и, если `claude` есть в PATH, выполняет
`claude mcp add`. Без `claude` печатает готовую команду.

1. Токен: `nodectl devlab connect` либо у владельца хаба `nodectl call devlab_hub issue_token
   name=<имя ноды> role=client` (имя токена обязано совпадать с именем ноды роя; запросы принимаются
   только с IP этой ноды). Файл `~/.config/sa-home-bot/devlab-token`, права 600.
2. Git (только если хочется работать без `nodectl devlab`):
   ```
   git remote add devlab http://alfred.tail61d88b.ts.net:8720/git/<проект>.git
   git config http.http://alfred.tail61d88b.ts.net:8720/.extraHeader "Authorization: Bearer $(cat ~/.config/sa-home-bot/devlab-token)"
   ```
   Ручные команды с именем `alfred.tail61d88b.ts.net` оставлены как есть. `connect` пишет адрес из
   `connect.hub_url` (2026-11-01): на ноде хаба (alfred) это tailscale-IP, на остальных нодах имя
   `hub_node`.
   Так токен попадает в локальный `.git/config` проекта: не коммитить его и не делиться репозиторием.
   Команды `nodectl devlab` этого не требуют.
3. MCP:
   ```
   claude mcp add --scope user --transport http devlab http://alfred.tail61d88b.ts.net:8720/mcp \
     --header "Authorization: Bearer $(cat ~/.config/sa-home-bot/devlab-token)"
   ```
   Порт 8720 по умолчанию, реальный — `[devlab_hub] port`, хост — `hub_node`.
4. Проект на хабе: `devlab_project_add name=<проект>` и `.devlab.toml` в корне
   репозитория (шаблоны в [devlab-toml.md](devlab-toml.md)).

## Тесты
`tests/unit/test_devlab_client_kit.py`: `connect` ставит оба скила; `base`, `fetch`, `drop` против
настоящего хаба (ветка, токен не в argv и не в ошибках, чужие имена отклонены); шаблоны
`.devlab.toml` разбираются `parse_spec`; `SKILL.md` называет только инструменты из `TOOLS` хаба;
симлинки репозитория совпадают с пакетными источниками.

Хаб на alfred и исполнитель на mycraft работают вживую с 2026-11-01 (v0.154.x). Не проверено: полный
цикл агента владельцем с реальной моделью, установка на ноутбук и Windows-ПК.
