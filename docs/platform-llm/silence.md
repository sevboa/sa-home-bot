# Тишина видеокарты (2026-11-18)

Владелец командой `/silent` запрещает работу видеокарты (жужжит дроссель). Первая задача —
состояние тишины в `app_state` БД бота на alfred и запрет будить ноды, пока тишина действует.
Команды бота и остальное — следующими задачами.

## Хранение (`src/sa_home_bot/silence.py`)

- Ключ в `app_state`: `gpu_silence` (`GPU_SILENCE_KEY`).
- `SilenceState`: `until` (`None` — бессрочно), `started_at`, `set_by`. Битая запись читается
  как `None` с `log.warning` («считаю, что тишина снята»).
- Активность: `active(now)` — `until is None or now < until`. Истёкшая запись не считается
  действующей: `SilenceService.load()` отдаёт `None`, но запись остаётся в БД (история).

## Право и команда `/silent` (2026-11-18)

- Команда `silent` (`right="silent"`, не universal, не в меню) — в `bot/commands.py`.
- Право `GuestRight("silent", "🔇 Тишина видеокарты")` добавлено в `GUEST_RIGHTS` (`bot/guest_rights.py`):
  владелец может выдать его гостю в `/guests`.
- Обработчик `bot/handlers/gpu_silence.py` (`cmd_silent`), роутер подключён в `bot/setup.py` сразу
  после `devlab.router`: `test_router_included_before_ai` требует, чтобы `devlab` шёл сразу после `away`.
  Право проверяется в обработчике: гость без права получает `DENIED_TEXT`, владелец проходит по `is_owner`.
- Аргументы: пусто — тишина бессрочно (`minutes=None`); целое > 0 — минуты; `off` — снять;
  `status` — состояние; остальное — подсказка про `/silent 30`, `/silent off`, `/silent status`.
- Связь с «Альфред в городе»: при включении/продлении тишины Альфред уезжает до `state.until`
  (или до потолка 24 ч, `MAX_AWAY` из `bot/away`) с причиной `SILENCE_AWAY_REASON = "тишина
  видеокарты"` (`AwayService.start`/`extend`, `AwayError` от потолка глотается). `off` снимает
  тишину и вызывает `do_back` (`bot/handlers/away.py`) — но Альфред возвращается только если
  уехал из-за тишины (`reason == SILENCE_AWAY_REASON`).
- `set_by` — `chat_id` того, кто включил.
- Открыто: бессрочная тишина держит Альфреда в городе только 24 ч (потолок `MAX_AWAY`), продления
  отъезда по таймеру пока нет — повторный `/silent` продлевает.

## CLI `sa-home-bot silent` (2026-11-18)

- Команды: `silent status`, `silent set [минуты]` (`minutes` — `nargs="?", type=int`, без числа —
  бессрочно), `silent off`; `--by claude|admin` у `set` и `off` (`claude` по умолчанию).
- Разбор БД и печать — как в `away_cli.py`: БД не найдена → код 2; печать через `html.unescape`;
  `set` с `minutes < 1` → `ValueError` из `SilenceService.start` в stderr, код 1.
- Логика «тишина + Альфред в городе» вынесена в общий модуль `bot/gpu_silence.py`
  (`SILENCE_AWAY_REASON`, `until_text`, `start_silence`, `away_for_silence`) — его зовут и
  обработчик `bot/handlers/gpu_silence.py`, и `silence_cli.py`.
- `off` снимает тишину и, если Альфред в городе с `SILENCE_AWAY_REASON`, вызывает `away.back()`
  (без `AwayRunner`, поэтому kick-обратного прохода нет).
- При `--by claude` после `set`/`off` — заметка владельцу через `queue_notice`
  («Claude включил тишину видеокарты. …» / «Claude снял тишину видеокарты. …»), как в `away_cli.py`.

## Правило продления (`SilenceService.start`)

- `minutes is None` → бессрочная (`until=None`); `minutes < 1` → `ValueError`.
- Если тишина уже действует:
  - бессрочная остаётся бессрочной (даже при `minutes`);
  - с числом `until = max(текущий until, конец)` — продление только на выступающую часть,
    `started_at` прежний.
- Если не действует (истёкшая или её нет) — новая запись с `started_at = now`.

## Запрет будить

- `wake_swarm_node_core` в `src/sa_home_bot/wake_core.py` принял параметр `force: bool = False`.
  Пока тишина действует (и `force` не передан), WoL не отправляется: нода получает
  `WakeOutcome(False, "🔇 Тишина видеокарты: «<node_id>» не будим.")`. `ensure_service_ready`
  возвращает `unreachable`, `send_wol` не проходит в `link.commands`.
- `force=True` шлёт WoL и в тишине (для ручного обхода владельцем).

## Хаб devlab в тишине (2026-11-18)

Хаб devlab живёт в отдельном процессе со своей БД (`wake.sqlite`), поэтому тишину он читает из БД
бота (`settings.database.path`). При старте `run_hub` открывает БД бота, создаёт `SilenceService`
и передаёт его в `build_hub_app` и `ExecutorWaker`.

Пока тишина действует:
- `ExecutorWaker.step()` не будит исполнителя: после проверки `pending_work` и `executor_alive`
  смотрит `await silence.load()`; если состояние не `None` — возвращает `None` без вызова
  `wake_core.ensure_service_ready`.
- `claim` (HTTP API исполнителя) не выдаёт задания на локальных моделях. Каталог моделей
  (`store.models()`) фильтруется: модели с `remote=false` исключаются из списка `models`
  исполнителя и из `live_models`. Задания на локальных моделях с `fallback` на удалённую
  уходят на удалённую модель (если она есть в каталоге и исполнитель её запросил).
- `heartbeat` исполнителя возвращает поле `silence`: `null` без тишины, иначе
  `{"until": "2026-11-18T12:30:00+00:00"}` (ISO-строка) или `{"until": null}` для бессрочной.
  Исполнитель передаст это `strata-switch` следующей задачей; хаб не решает, как поступить.
