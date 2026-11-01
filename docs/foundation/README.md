# Общие примитивы (foundation)

Каталог модулей, на которые зоны переключают свои дубли. Правило переключения везде одно:
поведение потребителя не меняется — различия вариантов выражены параметрами, а не
унификацией вывода. Тесты примитивов: `tests/unit/test_utils_*.py`, `test_proto_foundation.py`,
`test_bot_ui.py`, `test_support_fakes.py`. Знание из комментариев служебных модулей —
[shutdown-and-sessions.md](shutdown-and-sessions.md) и [requirements.md](requirements.md).

Что НЕ сделано сознательно (зоны сводят внутри себя): общий `utils/retry.py`, sync-обёртка
`run_sync` для subprocess, `utils/lazy.py`. Причины — в конце файла.

## utils/files.py

| Функция | Сигнатура |
|---|---|
| `atomic_write` | `(path, data: bytes \| str) -> None` — mkstemp в том же каталоге + `os.replace`, каталоги создаёт, временный файл удаляет при любой ошибке. Файл получается с правами mkstemp (0600). `bytes` пишется в `"wb"`, `str` — в `"w"` (кодировка по умолчанию, как у прежних копий) |
| `load_model` | `(ModelClass, path)` — нет файла → `ModelClass()`, иначе `model_validate_json(path.read_bytes())` |
| `save_model` | `(model, path)` — `atomic_write(path, model.model_dump_json(indent=2))` |
| `write_private` | `(path, data, *, exclusive=False, enforce_mode=False, make_parents=False, encoding=None)` — файл через `os.open(..., 0o600)`; `exclusive=True` → `O_EXCL` (не затирать существующий) и удаление файла при сбое записи; `enforce_mode=True` → дополнительный `os.chmod(0o600)` |

Переключить:
- `node/state.py::NodeState.load/save` и `llm/speech_therapy.py::SpeechTherapyState.load/save` → `load_model(cls, path)` / `save_model(self, path)` (тела совпадают побайтно; `load`/`save` остаются тонкими классметодом/методом либо вызывающие переходят на функции напрямую).
- `node/instances.py::atomic_write(path, data)` → `utils.files.atomic_write` (реэкспорт старого имени, если им пользуются вне зоны).
- `backup/apply.py` (блок `xray_path.tmp`: write_text → chmod 0600 → replace) → `atomic_write(xray_path, json.dumps(doc, indent=2))`: mkstemp уже даёт 0600, итоговое содержимое то же (кодировка по умолчанию; на Linux это UTF-8).
- `nodectl.py` (приём бандла, `O_TRUNC`, bytes) → `write_private(path, data)`, `make_parents=True` уже выполнено строкой выше — можно передать вместо `mkdir`.
- `backup/hold.py::allow_publish` → `write_private(path, text, make_parents=True, encoding="utf-8")`.
- `backup/restore.py::_write_private` → `write_private(path, data, enforce_mode=True)`.
- `backup/sealed.py::write_private_key` → `write_private(path, dump_key(key) + "\n", exclusive=True, make_parents=True, encoding="ascii")`.
- НЕ переключать `bot/egress.py::save_state`: пишет в `.tmp` с обычными правами (0644) без mkstemp и ловит `OSError` в `log.warning`; смена на `atomic_write` сменит права итогового файла на 0600. Решает зона bot-rest (если права не важны — `atomic_write(path, json.dumps(data, ensure_ascii=False).encode("utf-8"))` внутри прежнего `try/except OSError`).

## utils/http.py

- `get_json(url, timeout, headers=None) -> Any` — sync GET + `json.loads`; при `headers` строится `urllib.request.Request`.
- `get_json_async(url, timeout, headers=None)` — то же через `asyncio.to_thread`.
- `JSON_FETCH_ERRORS = (URLError, TimeoutError, OSError, ValueError)` — кортеж, который во всех потребителях стоит в `except` после такого запроса.
- `noqa: S310` живёт только здесь.

Переключить `_get_json_sync` в: `net/searxng.py`, `bot/tools.py`, `llm/ollama.py` (`get_json(url, timeout)`), `vpn/apk.py` (`get_json(url, timeout, _HEADERS)`). Вызовы `asyncio.to_thread(_get_json_sync, url, t)` → `await get_json_async(url, t)`. Тесты, подменявшие `urlopen`/`_get_json_sync`, подменяют `sa_home_bot.utils.http.get_json` (или `urllib.request.urlopen`).
POST не вынесен: единственный потребитель `llm/ollama.py::_post_json_sync` с обогащением `HTTPError` телом ответа (живая находка 2026-08-04) — оставить в зоне llm. `bot/interactives/transylvania.py::_fetch_code_sync` читает `["current"]` из JSON: `get_json(WEATHER_URL, WEATHER_TIMEOUT_S)["current"]`.

## utils/process.py

`async run(argv, *, timeout=None, input=None, stdout=PIPE, stderr=PIPE) -> ProcResult`;
`ProcResult(returncode, stdout: bytes, stderr: bytes)` + свойства `stdout_text` (`decode(errors="replace")`) и `stderr_text` (то же + `strip()`).
Исключения — как у asyncio: нет бинарника → `OSError`/`FileNotFoundError` из `run`; таймаут → `TimeoutError` (процесс при этом НЕ убивается, как и в прежнем `vpn_check._run`). `returncode` — `proc.returncode or 0`.

Переключить:
- `vpn/awg.py::_sudo_awg` — `run(["sudo", "-n", path, *args])`; `stdout.decode()` (строгий) → `result.stdout.decode()`, stderr → `result.stderr_text`.
- `vpn/proxy_backend.py::_run` → `run(argv)` (возвращает `(code, stdout_text, stderr_text)` через свойства).
- `apps/service.py::_run_systemctl` → `run(["sudo","-n","systemctl",action,unit], stdout=asyncio.subprocess.DEVNULL)`; `read_unit_status` → `run(["systemctl","is-active",unit], stderr=DEVNULL)` с `except OSError`.
- `reality/xray.py::_run` → `run([bin,"api",*args], input=stdin)` (stdin передаётся как `input`).
- `utils/ssh_sessions.py::_run` — уже переключён.
- `wol.py::ping_host` → `(await run(["ping","-c","1","-W","1",ip], stdout=DEVNULL, stderr=DEVNULL)).returncode == 0`.
- `vpn_check/service.py::_run(*cmd, timeout)` → `run(cmd, timeout=timeout)` внутри прежнего `except (OSError, TimeoutError)`; inline-вызов в `_curl_code` (create_subprocess_exec + wait_for) → `run(cmd, timeout=timeout_s + 3.0)`. Декодирование там `errors="replace"` без strip — использовать `stdout_text`, а stderr `result.stderr.decode(errors="replace")`.
- Синхронные `subprocess.run` в `sensors/*` и `node/fixups.py` НЕ сводятся: у каждого свои исключения, `check`, диагностика, и общая обёртка была бы тонкой копией `subprocess.run`. Внутри зон `sensors` и `node/fixups` — свести `_run`-подобные локально.

### Детекторы «sudo отказал» — `utils/requirements.py`
- `looks_like_sudo_denied(stderr)` = `looks_like_permission_error(stderr) or "a password is required" in stderr.lower()` — заменяет 5 копий условия: `vpn/awg.py`, `vpn/proxy_backend.py`, `apps/service.py`, `vpn_check/service.py` (`_curl_code`) и второй inline в `vpn_check`.
- `looks_like_sudo_message(stderr)` = `looks_like_permission_error(stderr) or stderr.strip().lower().startswith("sudo:")` — вариация `vpn_check/service.py::_looks_like_needs_password` (не зависит от локали). Это именно две разные функции: у второй нет проверки «a password is required» и есть префикс `sudo:`.
- Тексты ошибок для пользователя («по SSH выполните: nodectl fix») остаются у потребителей.

## utils/clock.py
`utc_now()` = `datetime.now(tz=UTC)`. Заменяет `_now()` в `runtime.py`, `vpn/service.py`, `sensors/source.py`, `jobs/smart.py`, `jobs/scan.py`, `worker/worker.py`, `bot/interactives/base.py`; и встречающиеся ~80 inline-вызовов `datetime.now(tz=UTC)` — по желанию зоны. Если тест монкейпатчит `module._now`, он должен подменять `module.utc_now` (или оставить локальную тонкую обёртку).

## utils/timefmt.py
- `ceil_div(a, b)` = `-(-a // b)` — `bot/away.py::format_remaining` (`-(-int(seconds) // 60)`), `bot/handlers/invites.py::_format_expiry` (3 места).
- `split_dhms(seconds) -> (d, h, m, s)` с `int(max(0, seconds))`.
- `format_dhms(seconds, *, days="д", hours="ч", minutes="м", secs="с")` — только ненулевые единицы, секунды показываются если ненулевые или если иных частей нет. `runtime.format_duration(seconds)` = `format_dhms(seconds)`; `domain/render._fmt_duration(td)` = `format_dhms(td.total_seconds(), days="d", hours="h", minutes="m", secs="s")`.
- `format_local(value: datetime | str, fmt, *, tolerant=False)` — `fromisoformat` (для str) → `astimezone()` → `strftime(fmt)`; `tolerant=True` возвращает исходную строку при `ValueError` (как `bot/guests_view.format_local_time`). Заменяет 11 цепочек `astimezone().strftime`: `nodectl.py:203`, `domain/render.py` (7 мест), `bot/pending_actions.py::_fmt_time` (ветки None/str остаются у вызывающего), `bot/outbox.py`, `bot/guests_view.py`, `bot/node_view.py::_fmt_since`.
- `zone_or_none(name) -> ZoneInfo | None` — `None` для пустого имени, неизвестного ключа и `ValueError`. Заменяет `bot/handlers/away.py::_owner_tz` (там ловился только `ZoneInfoNotFoundError`; расширение до `ValueError` — только для заведомо мусорных ключей), `bot/tools.py::_local_zone`, `bot/ai_flow.py` (строка с `datetime.now(ZoneInfo(tz))`).
- НЕ унифицированы по выводу: `_human_downtime` (`bot/node_events.py`), `_format_hours_diff` (`bot/tools.py`), `format_remaining` (`bot/away.py`) — три разные формы «ч/мин» с разными краевыми случаями (0 минут, 0 часов); `domain/render._fmt_uptime_short`; размеры (`_fmt_gb` и др.). Это персонажные/пользовательские тексты — зона оставляет их у себя, максимум берёт `ceil_div`/`split_dhms` внутри.

## utils/peer_path.py
`format_peer_path(peer)` - хвост строки пира (задержка, потери, ретранслятор) из записи `get_state()["peers"]`; пустая строка, если полей нет. Потребитель: `nodectl.py::_render_peer`.

## utils/plural.py
`plural_ru(count, one, few, many)`. Проверено табличной эквивалентностью с четырьмя прежними реализациями на n = −100..1000. Заменяет `bot/vpn_notify.py::_plural`, `bot/handlers/invites.py::_plural`, `bot/interactives/underground.py::_cells_ru` (`f"{n} {plural_ru(n, 'клетка', 'клетки', 'клеток')}"`).

## utils/text.py
- `clip(text, limit, *, mark="", collapse_spaces=False, at_word=False)` — текст до `limit`, если длиннее: обрезка, `rstrip()`, затем `mark`; `collapse_spaces` схлопывает пробелы до проверки длины; `at_word` режет по последнему пробелу, если он правее `limit // 2`.
  - `net/searxng.py::_trim` = `clip(text, SNIPPET_LIMIT, mark="…", collapse_spaces=True)`.
  - `bot/dialogue_context.py::_shorten` = `clip(text, limit, mark=SHORTENED_MARK, at_word=True)` (вызывается только при `len > limit`).
  - `bot/interactives/places.py::_clip` = `clip(str(value or ""), limit, collapse_spaces=True).rstrip(" .,;")` (точки/запятые срезаются и у коротких строк — поэтому снаружи).
  - НЕ переключать `bot/tool_debug.py::_clip`: маркер зависит от длины («обрезано, всего N символов»).
- `stem(word)` — триграммное «начало слова» (6+ букв: −2, 4–5: −1). Заменяет `memory/service.py::_stem` и `bot/dialogue_context.py::_stem` (объединить тесты на stem). Живая находка 2026-07-29 (озвучка/озвучке) описана в [shutdown-and-sessions.md](shutdown-and-sessions.md#stem).

## utils/ids.py
`short_token(length=8)` = `uuid4().hex[:length]`; `random_seed32()` = `int.from_bytes(uuid4().bytes[:4], "big")`.
Переключить: `bot/torrent_pending.py`, `bot/tool_debug.py`, `llm_chat.py:98` (`short_token()`), `bot/interactives/engine.py:2235` (`short_token(12)`), `bot/interactives/items.py:604` (`short_token(10)`), `items.py:608` и `llm/service.py:1481` (`random_seed32()`). Полный `uuid4().hex` как request/session id — это уже `proto.messages.new_id()`: `llm_chat.py:333`, `llm/service.py:1176`, `bot/voice_stt.py:203`, `bot/interactives/engine.py:2285,2583`.

## utils/pending.py
`PendingStore[T](maxsize, token_factory=short_token)`: `add(item) -> token` (вытесняет самый старый при переполнении), `get(token)`, `pop(token)`, `len()`.
`bot/torrent_pending.py::PendingTorrents` = `PendingStore[PendingTorrent](128)`; `bot/tool_debug.py::ToolCalls` = `PendingStore[ToolCall](256)` (методы `add/pop` и `add/get` совпадают по именам). `bot/vpn_secrets.py::PendingVpnSecrets` НЕ ложится (другая схема токена `token_urlsafe(6)`, TTL, нет лимита размера) — остаётся.

## utils/lifespan.py
`async cancel_task(task | None)` — cancel + `suppress(CancelledError)` + await. Точные копии `stop()`: `backup/snapshot.py`, `node/replication.py`, `node/lease.py`, `node/watch.py` — `await cancel_task(self._task); self._task = None`. Близкие: `backup/snapshot.py` (`_pending`), `bot/notifier.py`, `bot/service_link.py`, `bot/egress.py`, `bot/egress_app.py`, `node/peers.py`, `node/discovery.py`, `worker/queue.py`, `vpn/subweb.py`, `llm/ollama.py`, `llm_chat.py` (poll_task) — переключать только там, где нет дополнительной очистки между cancel и await.

## proto/
- `proto/errors.py`: `ServiceUnavailableError` (перенесён сюда из `bot/service_link.py`, оттуда реэкспортируется — импорт `from sa_home_bot.bot.service_link import ServiceUnavailableError` продолжает работать), `SERVICE_CALL_ERRORS = (ServiceUnavailableError, ProtoError)` — вместо 22 кортежей в `except (ServiceUnavailableError, ProtoError)`, `is_service_unavailable(exc)` — вместо `bot/ai_flow.py::_is_unavailable` и `tasks/service.py::_is_unavailable` (причина дубля в докстринге tasks — «не тянуть aiogram» — снята: `proto.errors` не зависит от aiogram).
- `proto/chunks.py`: `CHUNK_BYTES = 700 * 1024` (значение — часть протокола, не менять) и `async download_chunks(link, action, dst, *, base_args=None, expected_sha256=None, integrity_error=...)`. Переключить: `bot/voice_tts.py::_download_chunks` (`base_args={"session_id": session_id}`, `integrity_error="скачанное голосовое не прошло проверку целостности (sha256)"`), `bot/vpn_apk.py::_download_chunks` (`integrity_error="скачанный APK не прошёл проверку целостности (sha256)"`). Определения `_CHUNK_BYTES`: `bot/voice_tts.py`, `bot/voice_stt.py`, `bot/vpn_apk.py`, `llm/service.py::_TTS_CHUNK_BYTES`, `vpn/service.py::APK_CHUNK_BYTES` → `from sa_home_bot.proto.chunks import CHUNK_BYTES` (старые публичные имена `APK_CHUNK_BYTES` оставить как алиас, если их импортируют снаружи).
- `proto/emitter.py`: `DeferredEmitter` — замена четырёх `emit` в `*/app.py`. Использование: `emitter = DeferredEmitter()`; `service = X(..., emit=emitter)`; `server = ProtoServer(...)`; `emitter.bind(server)`. До `bind` события молча отбрасываются (как при `server is None`). Места: `vpn/app.py`, `tasks/app.py`, `node/app.py`, `llm/app.py`.
- `proto/addresses.py`: `NODE_SERVICE = "node"`, `MONITOR_SERVICE = "monitor"`, `LLM_NODE = "mycraft"`, `LLM_SERVICE = "llm"`, готовые `LLM_ADDRESS`, `MEMORY_ADDRESS`, `GRAPH_MEMORY_ADDRESS`, `NET_ADDRESS`, `TASKS_ADDRESS` (из констант `*/protocol.py` без их изменения), `node_address(node_id)` (`None` для пустого). Это отдельный модуль, а не правки `*/protocol.py`: те файлы намеренно без импортов внутри проекта (чтобы бот-тулы не тянули службы), а `Address` живёт в `proto.messages`. Заменяет `Address(node=memory_protocol.NODE_ID, service=memory_protocol.SERVICE_NAME)` и аналоги в `bot/ai_flow.py`, `bot/tools.py`, `bot/node_events.py`, `bot/pending_actions.py`, `people/seed.py`; шесть `LLM_NODE/LLM_SERVICE` в `bot/*`; шесть `NODE_SERVICE = "node"` в `node/lease.py`, `node/peers.py`, `node/replication.py`, `vpn/service.py`, `bot/node_view.py`, `wake_core.py`; `MONITOR_SERVICE` в `wake_core.py` и `bot/status_view.py`. Старые имена в модулях оставляют реэкспортом, если их читают снаружи (`node_view.NODE_SERVICE`, `eng.LLM_NODE`, `image_tools.LLM_NODE` и др. используются в других модулях и тестах).

## bot/ui/
Пакет aiogram-хелперов (не импортировать из служб без aiogram).
- `callback_data.pack(prefix, *parts)` = `":".join([prefix, *map(str, parts)])` — `_cb` в `bot/guests_view.py`, `bot/swarm_panel.py`, `bot/torrents_view.py` (`pack(commands.CALLBACK_PREFIX, code, *parts)`), `bot/pending_actions.py::_callback`, `bot/interactives/engine.py::_cb`.
- `unpack(data, prefix, parts) -> tuple[str, ...] | None` — `None` для пустой строки, чужого префикса или другого числа частей. `bot/interactives/engine.py::parse_callback` = `unpack(data, PREFIX, 2)` плюс проверка `parts[0] in REGISTRY` у вызывающего.
- `unpack_id_and_button(data, prefix) -> (int, str) | None` — `isdigit()` для первой части. `bot/pending_actions.py::parse_callback` и `bot/interactives/engine.py::parse_item_callback`.
- `int_or(raw, default=0)` — `_parse_int` в `bot/handlers/invites.py`, `torrents_panel.py`, `swarm_panel.py`.
- Формат строк callback_data не меняется (живые кнопки в чатах). `card_cb`/`_screen_cb` в vpn собираются через `commands.action_callback` — это другой формат, они на `pack` НЕ переводятся (`vpn_settings.card_cb`, `vpn_wizard.card_cb`, `handlers/vpn.py::_screen_cb` остаются, их три разных имени/контекста сводит зона vpn-bot).
- `redraw.redraw(callback, text, keyboard, **edit_options)` — `edit_text` с `suppress(TelegramBadRequest)`, `callback.message is None` → ничего. Заменяет `_redraw` в `bot/handlers/invites.py`, `torrents_panel.py`, `swarm_panel.py`; `_redraw_screen` и `_wizard_edit` в `bot/handlers/vpn.py` и повторяющиеся блоки `with suppress(TelegramBadRequest): await callback.message.edit_text(...)` (в `bot/handlers/node.py`, `bot/handlers/vpn.py`; для `disable_web_page_preview=True` — `**edit_options`).
- `keyboards.button(text, callback_data)`, `markup(*rows)`, `single_button(text, callback_data)` — сокращают `InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=..., callback_data=...)]])`. Переключение необязательное; `create_failed_keyboard`/`done_keyboard`/`pick_keyboard` в `vpn_wizard`/`vpn_settings`/`vpn_report` — пары-близнецы: сначала проверить эквивалентность текстов, потом свести.

## tests/unit/support/
Импорт: `from tests.unit.support.fakes import ...` (пакет `tests` уже есть).
- `fakes.FakeNotifier(fail=False, send_direct_result=..., send_voice_result=42424, first_message_id=1000)` — `send_direct` пишет `sent` как `(chat_id, text)`, параллельные списки `markups`, `reply_tos`, `threads`; `sent_with_markup` → `(chat_id, text, markup)`; `texts(chat_id)`; возвращает `first_message_id + len(sent)` (или `send_direct_result`; `fail=True` → `None`). Остальное: `send_photo_ex`/`send_photo`/`send_document`/`send_voice`/`edit_text`/`edit_markup`/`delete_message` и списки `photos`, `documents`, `voices`, `edited`, `markup_edits`, `deleted`. Копии, ждавшие `sent == [(chat, text, markup)]` или `[(chat, text, reply_to)]`, переходят на `sent_with_markup` / `list(zip(sent, reply_tos))`; копии с `return 1` — на `send_direct_result=1`.
- `fakes.FakeNodeLink(own_state=None, *, routes=None, result=None, results=None, error=None, description=None, actions=None)` — `command` пишет `calls` `(action, args, dst)` (и `commands` → `(action, args)`), возвращает `results[action]` или `result`, при `error` бросает; `get_state(dst)` отдаёт `own_state` или `routes["node:service"]`, иначе `ServiceUnavailableError`; `describe`, `actions`. Специфичные копии (счётчики `task_id`, прогрев/WoL-сценарии) остаются наследниками.
- `fakes.FakeBot` (typing, download, send_message со сценарием сбоев `send_script`, delete), `FakeSentMessage`, `FakeChat`, `FakeUser`, `FakeMessage(chat_id, text, ...)` (`answers`, `answer_markups`, `edits`, `edit_keyboards`, `reply_markup_edits`, `reply`), `FakeCallback(data, chat_id=1)` (`answered` = `[(args, kwargs)]`), `Clock(start)` (datetime или float; `advance(seconds=…, **timedelta)`, `.now`, `.t`), константы `FAKE_PNG`, `FAKE_JPEG`.
- `factories.make_subscription(chat_id=1, name="me", *allowed, **options)` (без `allowed` — владелец `*`), `make_book(*subs)`, `png_bytes(size=8, color="brown")` (настоящий PNG через PIL).
- `dispatch.prod_dispatcher(book, gate, store=None)` — контекст с настоящим `build_dispatcher`; на выходе отцепляет модульные роутеры, иначе второй `build_dispatcher` в той же сессии падает «Router is already attached». `dispatch.recording_bot()` → `(Bot, RecordingSession)`: сессия пишет запросы в `requests` (`sent(SendMessage)`), на `SendMessage` отдаёт `Message` от `BOT_USER`. Апдейт гонять `dp.feed_update(bot, update, **workflow)` с ключами как в `app.py::start_polling`.
- `waiting.wait_for(predicate, timeout=5.0) -> bool` (поведение `_wait_for` из test_netlab/multiaddr/server_hardening) и `wait_until(predicate, timeout=2.0)` (бросает `TimeoutError`, как `_wait_for` в test_discovery).
- Фикстура `store` (sqlite во `tmp_path` + миграции) — в `tests/unit/conftest.py`; локальные копии `store` в 34 тест-файлах удаляются (локальная определённая в файле перекрывает общую, поэтому переход безопасен).
- Settings-фабрики не вынесены: все 22 `_settings()` собирают разное.

## Что не вынесено и почему
- `utils/retry.py`: ollama `_post_with_retry`/`_post_stream_with_retry` — общий дедлайн + `ensure_running` на каждую попытку и условие «не ретраить, если пришёл хоть один чанк»; `llm_chat._call_chat_with_retry` — фиксированный список пауз; `torrents/service.py` — это лесенка запросов, а не ретрай. Общая функция получилась бы набором колбэков, длиннее каждого цикла. Два ollama-цикла — близнецы внутри зоны platform-llm, их сводить там. Ретраи `bot/notifier.py` — отдельный пакет высокого риска.
- `utils/lazy.py` (`AsyncLazy` для `llm/tts.py` ↔ `llm/stt.py`): две копии в одной зоне, сводятся внутри неё.
- `run_sync` для subprocess — см. выше.

## Автофикстуры conftest
`_isolated_known_vpn_servers` (autouse) подменяет `bot.vpn_nodes.KNOWN_SERVERS_PATH` на файл во `tmp_path`: память о VPN-серверах — файл в `./data`, тесты не должны ни писать в настоящий, ни читать оттуда.
