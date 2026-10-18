# Общие механизмы внутри llm/

Результат рефакторинга зоны: каждая вещь реализована один раз.

| Что | Где | Что заменило |
|---|---|---|
| Ленивая загрузка тяжёлой модели с блокировкой | `llm/lazy_load.py::LazyModel` | пара `_model`/`_model_lock`/`_get_model` в `stt.py` и `tts.py` |
| Словарь с TTL и уборкой | `llm/ttl_store.py::TtlStore` | три уборщика `_sweep_*` и парные `*_touched` в `service.py` |
| POST к Ollama с ретраями (обычный и потоковый) | `llm/ollama.py::_post_with_retry(..., on_chunk=None)` | близнецы `_post_with_retry` / `_post_stream_with_retry` |
| Дописывание тела HTTP-ошибки в `exc.msg` | `llm/ollama.py::_append_error_body` | два одинаковых блока |
| JSON-ответ модели -> dict | `llm/ollama.py::parse_json_object` | разбор в `photo_check.parse` и `image_prompt._parse` |
| Картинка -> JPEG -> base64 | `llm/vision.py::jpeg_bytes / jpeg_b64 / encode_b64` | `service._jpeg_b64`, ручной JPEG в `photo_check.save_original`, три `b64encode` в `vision` |
| GET JSON, `ollama_version` | `utils/http.py` | локальный `_get_json_sync` |
| Состояние Логопеда на диске | `utils/files.py::load_model / save_model` | свои `load`/`save` в `SpeechTherapyState` |
| id запросов, зерно, размер чанка, время | `proto.messages.new_id`, `utils.ids.random_seed32`, `proto.chunks.CHUNK_BYTES`, `utils.clock.utc_now` | `uuid4().hex`, ручной `from_bytes`, `_TTS_CHUNK_BYTES`, `datetime.now(tz=UTC)` |
| Имя службы | `llm.service.SERVICE_NAME = proto.addresses.LLM_SERVICE` | литерал `"llm"` |

## Что осталось раздельным сознательно

- Кэш пайплайнов `imagegen._pipelines` — кэш по ключу (repo) с выселением старых и общим `_load_lock`; это не «одна модель на процесс», поэтому `LazyModel` ему не подходит.
- Запись буфера частичной генерации (`_streaming`) использует тот же `TtlStore`, но с `touched=False`: запись не подметается, пока генерация идёт, и становится подметаемой после `touch` при завершении (`_finish_streaming`).
- Побочное исправление утечки: раньше пустая сессия загрузки STT, созданная неудачным первым чанком, никогда не получала метку времени и не подметалась. `TtlStore.put` ставит метку сразу, так что она теперь подметается.
