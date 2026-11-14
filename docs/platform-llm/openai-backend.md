# Транспорт OpenAI для службы llm (llm/openai_chat.py)

Запись 2026-11-15. Альфред переезжает на Qwen3.8 в Strata — у неё OpenAI-совместимый API
(`POST <openai_base_url>/chat/completions`), тогда как `llm/ollama.py` говорит с родным API Ollama
(`/api/chat`). Модуль `llm/openai_chat.py` даёт `chat` и `generate` той же сигнатуры и возвращает
словарь того же вида, что Ollama, поэтому читающий ответ код (`llm/service.py::_log_ollama_timings`,
обработка `ACTION_CHAT`, `llm/photo_check.py`, `llm/image_prompt.py`) не меняется. Подключение к службе
и потоковый режим — отдельные следующие задачи: сейчас модуль ни к чему не подключён, `ollama.py` и
`service.py` не тронуты.

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
- `True` и `"high"` → `reasoning_budget_tokens: 8192`, `"medium"` → 4096, `"low"` → 1024
  (константы модуля). `True` без уровня трактуются как максимум, потому что бот включает рассуждение
  именно тогда, когда модели есть о чём думать.

## options (`options_fields`)

Из ollama-опций проходят `temperature`, `top_p`, `seed` как есть, `num_predict` → `max_tokens`.
Остальное (`num_ctx` и прочее) отбрасывается: окно контекста задаётся не запросом, а запуском модели
в Strata, и ollama-овский `keep_alive`/`num_ctx` там просто нет.

## Почему не шлётся `response_format`

Вызывающий код сам вынимает JSON из текста (`ollama.parse_json_object` в `photo_check.py`,
`image_prompt.py`). `format: json` у Ollama менял поведение модели на жёсткий JSON-режим; молча
подставлять OpenAI-овский `response_format` значило бы менять ответы модели без явного запроса.
Параметр принимается сигнатурой (совместимость вызова) и игнорируется.

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

## Новые поля конфига (`[llm]`)

- `backend` — `Literal["ollama", "openai"]`, по умолчанию `"ollama"`: выбор транспорта, существующие
  конфиги не меняются.
- `openai_base_url` — адрес API без `/chat/completions`, по умолчанию `http://127.0.0.1:8090/v1`.
- `openai_api_key_file` — путь к файлу с ключом (gitignored, как `config.toml`); по умолчанию нет.

Тесты: `tests/unit/test_llm_openai_chat.py` — перевод сообщений (картинка, два вызова инструмента с
двумя ответами, все значения `think`), перевод ответа (обычный, с рассуждением, с вызовом инструмента,
с обрезкой по длине) и сеть на фейковом сервере aiohttp: тело запроса, заголовок с ключом из
временного файла, отсутствие заголовка без файла, разбор ответа, повтор после обрыва соединения,
HTTP 500 как `ProtoError`.
