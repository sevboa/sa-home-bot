# Git через хаб (60.3.3)

Bare-репозитории лежат в `<data_dir>/git/<имя>.git`. Проект заводит `HubStore.add_project(имя)`
(MCP-обёртка `devlab_project_add` — 60.3.5): строка в `projects`, `git init --bare`, хук
`pre-receive`. Имя: `a-z`, `0-9`, `-`, `_`, до 64 символов, без точек и путей. Если репозиторий
создать не удалось, строка проекта откатывается.

### Адрес и настройка клиента
`http://alfred.tail61d88b.ts.net:8720/git/<имя>.git`. Токен лежит в локальном конфиге клиента:

    git remote add devlab http://alfred.tail61d88b.ts.net:8720/git/<имя>.git
    git config http.http://alfred.tail61d88b.ts.net:8720/.extraHeader "Authorization: Bearer <токен>"

### Как устроено
- `devlab/hub/git_http.py`: `/git/<имя>.git/...` запускает `git http-backend` как CGI
  (asyncio subprocess). Тело запроса и ответ идут потоком кусками по 64 КиБ, pack целиком в память
  не читается. Одновременно не больше 4 процессов (alfred слабый).
- Без действующего токена 401 (общий middleware), неизвестный проект или кривой путь 404.
  Fetch доступен любой роли, push ограничивает хук.
- В окружение бэкенда уходят `REMOTE_USER` = имя токена и `DEVLAB_ROLE` = роль
  (`protocol.GIT_ENV_USER`, `GIT_ENV_ROLE`). Из окружения хаба пробрасываются только
  `PATH`, `PYTHONPATH`, `LANG`, `LC_ALL`.
- `Content-Encoding: gzip`: aiohttp разжимает тело сам, поэтому `CONTENT_LENGTH` и
  `HTTP_CONTENT_ENCODING` бэкенду не передаются, он читает stdin до конца. Проверено тестом.
- В репозитории включены `http.receivepack` и разрешено удаление веток на уровне git
  (`receive.denyDeletes=false`): удаление регулирует хук.

### Права по ролям
| роль | ветки |
|---|---|
| `executor` | `wip/*` создать, обновить, удалить; `agent/*` создать, обновить |
| `client` | `base/*` создать, обновить, удалить; `agent/*` только удалить |
| `gateway`, неизвестная роль | git закрыт целиком: 403 ещё до `http-backend` (шлюзу код проектов не нужен), хук тоже отклоняет push |

`master`, `main`, теги, чужие префиксы и пустое имя после префикса (`wip/`) отклоняются
с сообщением `devlab: отклонено (<имя>): ...`. Если отклонена хоть одна ветка, не проходит весь push.

### Почему хук вызывает общую функцию
Хук `hooks/pre-receive` состоит из двух строк: `exec <python службы> -m sa_home_bot.devlab.hub.refpolicy`.
Правила живут только в `devlab/hub/refpolicy.py` (`check_ref`, таблица `POLICY`): тесты проверяют
те же правила, что исполняет хук, копий на shell нет. Путь интерпретатора подставляется при
создании репозитория (`sys.executable` службы); при переезде venv хуки переустанавливаются
через `repos.install_hook`.
