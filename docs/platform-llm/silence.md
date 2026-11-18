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
