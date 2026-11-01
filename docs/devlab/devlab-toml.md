# Шаблоны `.devlab.toml` для других проектов (60.7)

Файл лежит в корне репозитория, читается `devlab/executor/spec.py` (`parse_spec`). Лишние ключи в
`[test]` и `[agent]` — ошибка (`extra="forbid"`), на верхнем уровне лишние ключи игнорируются.
Тест `tests/unit/test_devlab_client_kit.py` достаёт блоки ниже и разбирает их настоящим парсером,
поэтому правь шаблоны только вместе с парсером.

## Поля
Верхний уровень: `project` (имя проекта на хабе; для `nodectl devlab snapshot`, исполнитель его
игнорирует).

`[test]`: `cmd` (команда по умолчанию для `devlab_test`), `args`, `timeout_s` (по умолчанию 1800),
`image` (образ контейнера), `setup` (команды установки зависимостей, выполняются один раз и
кэшируются), `setup_files` (файлы, от содержимого которых зависит кэш окружения), `[test.env]`.

`[agent]`:
- `rules`: файлы правил, которые агент читает первыми (`CLAUDE.md`, `AGENTS.md`);
- `checks`: команды стражей, зелёные перед завершением;
- `allowed_commands`: команды сверх рецепта, префиксы;
- `forbidden_paths`: пути, закрытые для записи (читать агент может: нужны образцы и тесты);
- `secret_paths`: пути, закрытые и для чтения, и для записи (glob или префикс-каталог с `/`);
- `diff_prefixes`: префиксы путей, где у результата должен быть непустой дифф (страж завершения; пусто: любой дифф);
- `allow_remote_models`, `command_timeout_s`, `output_chars`.

## Python
```toml
project = "my-python-app"

[test]
cmd = "python -m pytest -q"
timeout_s = 1800
image = "docker.io/library/python:3.13-slim"
setup_files = ["pyproject.toml"]
setup = ["pip install --no-cache-dir -e .[dev]"]

[agent]
rules = ["CLAUDE.md"]
checks = ["ruff check .", "python -m pytest -q"]
allowed_commands = ["ruff check", "ruff format", "python -m pytest", "git diff", "git status"]
forbidden_paths = [".devlab.toml", "CLAUDE.md", "pyproject.toml"]
secret_paths = [".env", "*.sqlite"]
diff_prefixes = ["src/", "tests/", "docs/"]
```

## Node
```toml
project = "my-node-app"

[test]
cmd = "npm test --silent"
timeout_s = 1800
image = "docker.io/library/node:22-slim"
setup_files = ["package.json", "package-lock.json"]
setup = ["npm ci"]

[agent]
rules = ["CLAUDE.md"]
checks = ["npm run lint", "npm test --silent"]
allowed_commands = ["npm run", "npm test", "git diff", "git status"]
forbidden_paths = [".devlab.toml", "CLAUDE.md", "package.json", "package-lock.json"]
secret_paths = [".env", ".env.*"]
diff_prefixes = ["src/", "test/", "docs/"]
```

## Без зависимостей
```toml
project = "my-scripts"

[test]
cmd = "sh ./run-tests.sh"
timeout_s = 600
image = "docker.io/library/debian:stable-slim"

[agent]
rules = ["CLAUDE.md"]
checks = ["sh ./run-tests.sh"]
allowed_commands = ["sh ./run-tests.sh", "git diff", "git status"]
forbidden_paths = [".devlab.toml", "CLAUDE.md"]
secret_paths = [".env"]
diff_prefixes = []
```

## Сверка с sa-home-bot
`.devlab.toml` sa-home-bot использует те же поля: `project`, `[test]` (`cmd`, `timeout_s`, `image`,
`setup_files`, `setup`, `[test.env]` с `PYTHONPATH`), `[agent]` (`rules = ["CLAUDE.md"]`, `checks` —
`ruff check src tests` и страж комментариев `test_no_comments_in_code.py`, `allowed_commands`,
`forbidden_paths`, `secret_paths`, `diff_prefixes = ["src/"]`). В шаблонах `forbidden` и `secret`
из задачи называются так, как в парсере: `forbidden_paths` и `secret_paths`.
