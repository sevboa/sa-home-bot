# Транспорт OpenAI для службы llm (llm/openai_chat.py)

Запись 2026-11-15, продолжение 2026-11-16 (поток и подключение к службе). Альфред переезжает на
Qwen3.8 в Strata — у неё OpenAI-совместимый API (`POST <openai_base_url>/chat/completions`), тогда
как `llm/ollama.py` говорит с родным API Ollama (`/api/chat`). Модуль `llm/openai_chat.py` даёт
`chat`, `chat_stream` и `generate` той же сигнатуры и возвращает словарь того же вида, что Ollama,
поэтому читающий ответ код (`llm/service.py::_log_ollama_timings`, обработка `ACTION_CHAT`,
`llm/photo_check.py`, `llm/image_prompt.py`) не меняется. Служба выбирает транспорт через фасад
`llm/backend.py` и поле `[llm] backend` (запись ниже). `generate` отдаёт текст ещё и в поле
`response`, как `/api/generate` у Ollama: `ACTION_ASK` читает именно его (находка 2026-11-15 при
выкате v0.172.1 — без поля Альфред на `ask` отвечал пустой строкой).

## Почему свой клиент, а не библиотека

Тот же подход, что у `ollama.py` (см. ollama-backend.md): синхронный `urllib` в `asyncio.to_thread`,
общий срок `cfg.request_timeout_s` на все попытки, повтор только при обрыве соединения, ошибка наружу
как `ProtoError(ERR_INTERNAL, ...)`. Новых зависимостей нет. `ensure_running` здесь не нужен: Strata —
всегда поднятый HTTP-сервер на чужой машине, прогрев WSL/контейнера к нему отношения не имеет.
Тело HTTP-ошибки дописывается в `exc.msg` так же, как `_append_error_body` у Ollama, — иначе разбор
сбоя сводился бы к «HTTP Error 500» без причины.

## Перевод запроса (`to_openai_messages`)

Сообщения бот строит в своей форме (`llm_chat.py`, `bot/dialogue_context.py`), и она не совпадает с
OpenAI:

- системное сообщение ставится первым; остальные роли копируются как есть (в том числе уточнение
  роутера, которое служба дописывает в конец списка);
- `images` у сообщения пользователя превращаются в `content` из частей: `{"type": "text", ...}` и
  `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}` — base64 JPEG без
  префикса (его даёт `llm/vision.py`);
- вызовы инструментов у ассистента: `arguments` из словаря становится строкой JSON, каждому вызову
  без `id` выдаётся `call_<номер>` со сквозной нумерацией по запросу; уже готовый `id` сохраняется,
  а нумерация всё равно продвигается, чтобы следующий сгенерированный `id` не повторился;
- идущие за таким сообщением `role: tool` по порядку получают `tool_call_id` этих вызовов — без них
  OpenAI-совместимый API отвергает запрос (та же причина, по которой у Ollama был HTTP 400 на висящие
  вызовы, см. ai-tools/incidents.md);
- `content` ассистента без текста — пустая строка: у OpenAI-совместимого API сообщение ассистента без
  `content` невалидно.

## think → поля Strata (`think_fields`)

`think` приходит из профиля модели (`llm/model_profiles.py`) как `bool | str | None`:

- `None` → ничего не отправляем (поведение по умолчанию у самой модели);
- `False` и `"off"` → `{"chat_template_kwargs": {"enable_thinking": false}}` — рассуждение выключено;
- `"minimal"` → `reasoning_budget_tokens: 256` — так профиль `effort_min` передаёт `off` (см. ниже);
- `True` и `"high"` → `reasoning_budget_tokens: 8192`, `"medium"` → 4096, `"low"` → 1024
  (константы модуля). `True` без уровня трактуются как максимум, потому что бот включает рассуждение
  именно тогда, когда модели есть о чём думать.

### Почему Qwen в Strata не выключают рассуждение (стенд 2026-11-15)

Qwen3.8 IQ3_XXS в Strata с `chat_template_kwargs.enable_thinking=false` на многоходовой истории
схлопывает ответы: с 9–10 хода персона отвечает 1–3 символами или пусто (`done_reason=stop`), изредка
пишет `<think>` прямо в `content`. Без истории ответы нормальные, одиночные пробы этого не ловят.
Замер на 35 ходах персоны (сырьё `/mnt/data/claude/alfred-qwen/stand/think-*.jsonl`):

| Вариант | Нормальных из 35 | Ход ср./макс |
|---|---|---|
| `reasoning_budget_tokens=0` | 35 | 7.8 / 29.5 с — 0 не выключает рассуждение |
| `128` | 34 | 4.0 / 5.3 с — бюджет съел ответ, пусто |
| `256` | 35 | 5.8 / 9.5 с |
| `enable_thinking=false` + пустой `<think></think>` в истории | 8 из 15 | префикс утекает в ответ |

Поэтому у профиля `strata-alfred` контроль `effort_min`: `off` превращается в бюджет 256. Ведущий и
сжатие на длинной истории с ним в порядке (Ведущий ~10–13 с вместо ~5 с). Gemma так не ломается и
выключается обычным `enable_thinking: false`.

## options (`options_fields`)

Из ollama-опций проходят `temperature`, `top_p`, `seed` как есть, `num_predict` → `max_tokens`.
Остальное (`num_ctx` и прочее) отбрасывается: окно контекста задаётся не запросом, а запуском модели
в Strata, и ollama-овский `keep_alive`/`num_ctx` там просто нет.

## `response_format`: JSON-режим

`response_format="json"` (так зовут `photo_check`, `image_prompt`, разметка стола в `service.py`)
уходит как `{"type": "json_object"}`, по аналогии с `format: json` у Ollama. Другие значения и `None`
поле не добавляют.

Находка 2026-11-15 (v0.173.1): раньше параметр игнорировался в расчёте, что вызывающий код сам
вынет JSON из текста. На Gemma через PolyStrata это не сработало: без JSON-режима Gemma
оборачивает ответ в ```json ... ```, `parse_json_object` его не разбирал. Из-за этого проверка снимка
зрением (около 30 с впустую), промптер картинок и разметка стола уходили в запасные ветки, и
«фотоаппарат наводился» долго. Живой замер: с `json_object` ответ приходит чистым JSON. Для
страховки `ollama.parse_json_object` теперь снимает обёртку ```json, если она всё же пришла.

## Перевод ответа (`from_openai_response`)

`choices[0].message` → `message` в форме Ollama: `content`, `thinking` (из `reasoning_content`, если
непусто — его читает `_log_ollama_timings`), `tool_calls` в форме `[{"function": {"name",
"arguments": словарь}}]` (строку JSON разбираем; неразбираемая строка → `{}`, как `_close_pending_tool_calls`
уже терпит битые аргументы в истории). `finish_reason == "length"` → `done_reason: "length"` — на нём
держится страховка переполнения окна (ai-flow/context-window.md), иначе `"stop"`. Токены:
`usage.prompt_tokens` → `prompt_eval_count`, `usage.completion_tokens` → `eval_count`.
`total_duration` считается по замеру запроса в наносекундах: у OpenAI-совместимого API нет полей
`load_duration`/`prompt_eval_duration`/`eval_duration`, поэтому `_log_ollama_timings` покажет скорость
только по общему времени.

## Сеть и ключ

`chat` собирает тело: `model`, `messages`, `stream: false`, `tools` как есть (формат инструментов у
Ollama и OpenAI совпадает), плюс поля из `think_fields` и `options_fields`. Запрос идёт на
`cfg.openai_base_url + "/chat/completions"`. Если задан `openai_api_key_file`, ключ читается из файла
(пробелы обрезаются) и уходит заголовком `Authorization: Bearer <ключ>`; без файла заголовка нет.
Ключ не логируется и не попадает в текст ошибок: в `ProtoError` попадают только URL и причина
соединения/тело HTTP-ошибки.

## Поток (`chat_stream`, `parse_stream`)

`chat_stream` собирает то же тело, что `chat`, но со `stream: true` и
`stream_options: {"include_usage": true}`: без `include_usage` Strata в потоке не отдаёт `usage`, и
`prompt_eval_count`/`eval_count` в `_log_ollama_timings` и в статистике окна (`_usage_stats`) были бы
пустыми. `options` у потока нет — сэмплинг служебных проходов (`temperature`, `num_predict`) нужен
только обычным вызовам, а потоком служба ходит лишь в персонажном проходе.

Разбор потока вынесен в чистую `parse_stream(lines, on_chunk)`: она работает над итератором строк
(её тестируют без сети), а `_post_json_stream_sync` лишь открывает `urllib`-ответ и передаёт ей
строки. Строки SSE: `data: {json}`, последняя `data: [DONE]`. Строки без префикса `data:` (keep-alive
комментарии) и неразбираемый JSON пропускаются с WARNING — обрыв одной строки не должен обрывать
ответ; после `[DONE]` чтение прекращается, хвост игнорируется.

Из `choices[0].delta`: `content` накапливается и `on_chunk` получает весь накопленный текст (не
кусок) — ровно как у Ollama, поэтому буфер `chat_progress` и `bot/ai_flow.py` не менялись;
`reasoning_content` накапливается отдельно; `tool_calls` склеиваются по `index` (`id` и
`function.name` приходят один раз, `function.arguments` — кусками). `finish_reason` берётся из
последнего куска с ним. Отдельный кусок с `usage` (у него пустые `choices`) даёт счётчики.
`parse_stream` возвращает ответ в форме **не-стримового** OpenAI-ответа (`choices[0].message` с
`content`/`reasoning_content`/`tool_calls`, `finish_reason`, `usage`), поэтому `from_openai_response`
служит одна и та же функция обоим режимам, а `tool_calls` в форме Ollama получаются тем же
`_ollama_tool_call`. Куски `tool_calls` без `id` получают `call_<позиция>` — та же нумерация, что
`to_openai_messages` выдаёт вызовам в истории.

Повтор при обрыве — только пока не пришло ни одного куска (`sent_any` в `_post_with_retry`), как у
Ollama: половину ответа второй попыткой не повторяем, пользователь уже видит текст. HTTP-ошибка
обогащается телом (`_append_error_body`) и наружу идёт как `ProtoError(ERR_INTERNAL, ...)`.

## Фасад транспорта (`llm/backend.py`)

`service.py`, `photo_check.py` и `image_prompt.py` зовут модель через `backend.chat`,
`backend.chat_stream`, `backend.generate` — те же сигнатуры, что у `ollama.*`. Фасад берёт модуль
транспорта по `cfg.backend` и вызывает функцию **атрибутом модуля в момент вызова**, а не своим
импортом: подмена `ollama.chat` в тестах службы продолжает работать.

`backend.preload`, `backend.ensure_running` и `backend.stop` при `backend = "openai"` не делают
ничего. Strata — всегда поднятый HTTP-сервер на чужой машине: прогревать нечего (`ensure_running`
у Ollama поднимает WSL/контейнер и ждёт `/api/version`), модель не выгружается по нашему запросу
(`preload` у Ollama — находка 2026-07-27: без явной загрузки первый запрос платит десятки секунд),
и выключать её нельзя — ею пользуются другие клиенты, а `stop` у Ollama гасит контейнер. Поэтому
`ACTION_WARMUP` при openai остаётся быстрым no-op (служба лишь снимает флаг «спит»), а `sleep` не
трогает чужую машину: усыпление службы llm при openai сводится к её собственному состоянию и
остановке WSL-keepalive, которого при openai и так нет (`container_backend` при таком транспорте не
используется).

`ollama.parse_json_object` и `ollama.WslKeepalive` остаются в `ollama.py`: их берут по имени там же,
где они живут, фасад их не перекладывает.

## Профиль `strata-alfred` (`model-profiles.toml`)

`name = "alfred"` — псевдоним Qwen3.8 в Strata, его и пишет `[llm] model`. Профиль
`think_control = "effort_min"`, `router = true`, `num_ctx = 32768`, `thinking_hidden = true`:

- `effort_min` → `think_arg` отдаёт `"minimal"` для `off` и строки `low`/`medium`/`high` для уровней —
  ровно то, что понимает `openai_chat.think_fields` (`"minimal"` → `reasoning_budget_tokens` 256,
  уровень → 1024/4096/8192). Gemma 4 в PolyStrata идёт по своему профилю `gemma-4`
  (`flag_implicit_on`): там `off` → `False` → `enable_thinking: false`, и это для неё правильно;
- `num_ctx = 32768` — окно запроса боту; в самой Strata окно задаётся запуском модели, запросом его
  не передать (`options_fields` отбрасывает `num_ctx`), но по этому числу бот считает страховку
  переполнения и сжатие истории;
- `thinking_hidden = true` — рассуждение приходит в невидимый `message.thinking`, бот показывает
  «задумался».

## Пример `[llm]` для mycraft

```toml
[llm]
backend = "openai"
openai_base_url = "http://127.0.0.1:8090/v1"
model = "alfred"
openai_api_key_file = "secrets/strata_key.txt"
```

## Новые поля конфига (`[llm]`)

- `backend` — `Literal["ollama", "openai"]`, по умолчанию `"ollama"`: выбор транспорта, существующие
  конфиги не меняются.
- `openai_base_url` — адрес API без `/chat/completions`, по умолчанию `http://127.0.0.1:8090/v1`.
- `openai_api_key_file` — путь к файлу с ключом (gitignored, как `config.toml`); по умолчанию нет.

Тесты: `tests/unit/test_llm_openai_chat.py` — перевод сообщений (картинка, два вызова инструмента с
двумя ответами, все значения `think`), перевод ответа (обычный, с рассуждением, с вызовом инструмента,
с обрезкой по длине), разбор потока (`parse_stream` на списке строк: накопленный текст в `on_chunk`,
рассуждение, склейка `tool_calls` по `index` и нумерация без `id`, `usage`, `finish_reason: length`,
мусорные строки и хвост после `[DONE]`, байтовые строки) и сеть на фейковом сервере aiohttp: тело
запроса, заголовок с ключом из временного файла, отсутствие заголовка без файла, разбор ответа,
повтор после обрыва соединения, HTTP 500 как `ProtoError`, а для потока — тело со `stream: true` и
`stream_options`, SSE-ответ с чанками и `usage`, повтор пока не пришёл ни один кусок и отказ
повторять после первого куска. `tests/unit/test_llm_backend.py` — выбор транспорта для обоих
значений `backend`, проброс именованных аргументов и то, что при openai `preload`/`ensure_running`/
`stop` не зовут ничего. `tests/unit/test_model_profiles.py` — разрешение профиля по имени `alfred` и
что `think_arg` этого профиля отдаёт значения, которые принимает `openai_chat.think_fields`.
