# Пакет bot/tools

Тулы (tool-calling) диалога /ai: LLM_INTEGRATION_PLAN.md §7–8. Каждый тул — узкая функция в явном реестре `registry.TOOLS`, а не общий прокси на произвольное действие роя (§7.2: общий прокси был бы дырой в правах — модель дозвонилась бы куда угодно). Декларация — формат OpenAI function-calling, который Ollama понимает нативно для tool-calling-моделей (qwen3 и др.).

Пакет сознательно не зависит от aiogram: его импортирует не только бот, но и служба `tasks`, и MCP-агент (`agent/`), которым Telegram не нужен. Поэтому литералы `ACTION_LOOK_AT_PHOTO`, `EVENT_UPDATE_FINISHED`, `EVENT_RESTART_APPLIED` объявлены в самом пакете, а не импортируются из тяжёлых `llm/service.py` и `node/service.py` (те тянут Ollama/WSL-обвязку, супервизор, пиров, discovery); сюда же `wake_core.py::_CHECK_UPDATE_ACTION` — та же причина.

## Раскладка

| Модуль | Содержимое |
|---|---|
| `core.py` | `ToolContext`, `DismissalBox`, права (`CommandRight`, `ActionRight`, `VariantRights`), `ToolSpec`, `ToolKit`, канарейка (`CANARY_TOOLS`, `for_context`), общий каркас: `text_arg`, `bounded_limit`, `call_service`, `NO_SWARM_LINK`, `people_of` |
| `registry.py` | `TOOLS` (порядок задаёт порядок деклараций в контексте модели) и `tools_for(subscription)` |
| `calc.py`, `currency.py`, `weather.py`, `get_time.py`, `translit.py` | расчёты и публичные API без ключа |
| `remind.py` | `remind` и `schedule_agent_dialogue` (постановка задач в службу `tasks`) |
| `acquaintance.py`, `people_tools.py` | знакомства гостей, карточки людей (`note_person`, `find_person`) |
| `delivery.py`, `tell.py`, `notify.py`, `guests_list.py` | личные сообщения и справочник гостей |
| `swarm.py`, `node_manage.py`, `torrents.py` | состояние роя, управление нодами, закачки |
| `memory.py`, `web_search.py`, `pictures.py`, `chat_controls.py` | память (+ эпизоды графа), поиск, картинки/фото, `dismiss`/`voice_mode`/`recall_tool_result` |
| `vpn.py` | личный VPN |
| `interactive_tools.py` | интерактивы (радио, вещи поместья, снимок кабинета, канареечные `search`/`act`/`walk`/`go`) |
| `__init__.py` | реэкспорт публичных имён: `from sa_home_bot.bot import tools` и `tools.tool_*`, `tools.ToolContext`, `tools.tools_for` и т. д. продолжают работать |

Каждый модуль семейства сам объявляет свои `ToolSpec` (константы `CALC`, `TELL`, …); `registry.TOOLS` только задаёт порядок. `ToolSpec.telegram_only` помечает тулы, завязанные на живые Telegram-объекты бота (MCP-агент их не отдаёт, см. [agent-experiment.md](agent-experiment.md)).

## Права: Альфред не может больше, чем собеседник

Исходное правило §7.2 («реестр один для всех, у кого есть `chat@llm`») держалось, пока тулы ничего не знали о системе. С появлением `swarm_status` это перестало быть правдой: без фильтра /ai стал бы обходным путём вокруг подписок. Требование владельца (2026-07-27): Альфред не ОТКАЗЫВАЕТ, а именно НЕ УМЕЕТ — фильтрация на уровне деклараций (`tools_for`), а не проверкой внутри обработчика: тула без прав модель не видит и не обещает того, чего не сделает.

- `CommandRight(name)` — право уровня команды бота (`/status` гейтится `allows_command(commands.STATUS.name)`; тул с теми же данными требует ровно того же).
- `ActionRight(action, service)` — `действие@служба` (`list@torrents`); групповые формы (`*@torrents`, голый `*`) уже поддержаны `Subscription.allows_action`.
- `VariantRights(param, rights)` — права на отдельные значения enum-параметра одного тула. Заводить по тулу на значение — значит повторять описание и раздувать контекст (декларации уходят в контекст на КАЖДОМ раунде), поэтому недоступные значения вырезаются из `enum` по подписке; не осталось ни одного — тул не объявляется. Обработчик всё равно перепроверяет значение (`VariantRights.refusal_for` → «не умею: …»): модель может передать что угодно.
- `requires=None` — тул прав не требует (чистый расчёт или публичный API). `subscription is None` (служба `tasks`, подписку удалили из конфига между постановкой задачи и срабатыванием) — остаются только тулы без `requires` и без `variants` (fail-closed).

Канарейка Этапа 59: `search`, `walk`, `go`, `act` модель видит только у гостей из `llm.interactives_canary_user_ids` (`for_context`). Тулы из `SERIAL_TOOLS` (`act`, `search`) исполняются по одному за раунд (Этап 59.7): исход решает Ведущий, и модель обязана его увидеть, прежде чем решать следующее действие; лишние вызовы того же ответа не исполняются (`llm_chat.run_chat_loop`), им отдаётся `SERIAL_DEFERRED`.

## Общий каркас обработчиков

- `text_arg(args, key)` — `str(args.get(key) or "").strip()`.
- `VariantRights.refusal_for(subscription, value)` — «не умею: …» или `None`.
- `bounded_limit(raw, default, maximum)` — `limit` числом в пределах `[1, maximum]` или текст-ошибка (`swarm_events`, `guests_list`).
- `call_service(ctx, action, payload, dst, unavailable_subject=…, proto_refusal=…)` — вызов службы с единым переводом сбоев в текст для модели: `ProtoError` → «не вышло: <сообщение службы>» (служба сама объяснила, что не так — это готовый ответ, а не сбой), `ServiceUnavailableError`/`TimeoutError` → «недоступно: <кто не отвечает> (<причина>)». Отказ тула — обычный результат для модели (§7.3), а не сбой цикла: спящая нода не должна ронять диалог. `proto_refusal` — хук для особых кодов (VPN: `ERR_QUOTA_CEILING`).
- `NO_SWARM_LINK` — «недоступно: нет связи с роем».

Формулировки ответов модели сохранены дословно; различия подписи («нода», «память», «VPN-служба») передаются параметром.

## ToolContext

Поля и зачем они:
- `history` — сообщения, которые ПРЯМО СЕЙЧАС видит модель (та же ссылка, что `messages` в `llm_chat.run_chat_loop`, находка 2026-07-24). `remind` берёт снимок диалога отсюда, а не из БД: у службы `tasks` нет `ai_turns` бота, а живому /ai читать БД ради того же незачем.
- `node_link` — подключение к ноде роя; `subscription` — права собеседника.
- `book`/`notifier`/`store`/`author` — для тулов, шлющих личные сообщения (`tell`, `notify_guest`, `guests_list`): найти получателя среди подписок, отправить и записать как ход диалога, чтобы получатель мог ответить реплаем. `book` не типизирован (`Any`), чтобы тулы не зависели от подписок (те зависят от конфига, а конфиг импортирует ноду).
- `emit` — мост доставки для службы `tasks` (находка 2026-08-06): там нет ни `notifier`, ни `store`, но есть `SubscriptionBook` и очередь событий роя. `tell`/`notify_guest` просят БОТА (у него настоящий `Notifier`) отправить сообщение через `tasks.protocol.EVENT_DELIVER_MESSAGE` (`bot/node_events.py::_handle_deliver_message`). У живого /ai мост не нужен.
- `people` — снимок карточек людей на ход (Этап 58, `people/book.py::PeopleBook`); `None` — без БД (служба `tasks`, MCP-агент), тогда только имена подписок.
- `message_thread_id` — реальный Telegram-тред; `dialogue_id` в чате без топика подменяется `message_id`, что для Bot API не тред и приведёт к 400. Проактивные `notifier.send_*` должны получать именно `message_thread_id`.
- `woken_by` — `(node, event_type)`, из-за которого сработала задача-продолжение (`remind after_event`); см. [incidents.md](incidents.md), 2026-08-05.
- `tool_result_cache` — полные тексты результатов, укороченных в контексте модели (`llm_chat.py::_inline_or_cache`); живёт только в памяти одного прохода `run_chat_loop`.
- `pending_actions` (Этап 45), `interactives` (Этап 47), `user_id`, `is_private` — только у живого /ai. `user_id` — кто говорит (в общем чате это не `chat_id`), сцены интерактивов идут только в личке.
- `end_turn` — тул закончил ход сам, реплики Альфреда не будет (`take_photo`, карточка вещи: описание придёт вместе со снимком). `run_chat_loop` прекращает раунды, `ai_flow.request_alfred` отдаёт `SILENT_REPLY`.
- `reply_to_message_id` — на какое сообщение ответил гость; для `look_at_photo` ответ на картинку Альфреда значит, что речь о ней.

`DismissalBox` — ячейка намерения «Альфреда распустили»: тул `dismiss` ничего не делает в момент вызова (ответ модели ещё не сгенерирован, погашенная Ollama оборвала бы прощание), исполняет `bot/handlers/ai.py` после отправки ответа (`bot/ai_flow.py::perform_dismissal`). `ctx.dismissal is None` (служба `tasks`, тесты) — исполнять некому, тул честно говорит, что не умеет.

## Зависимости и циклы

`ai_flow` импортирует пакет, поэтому `LLM_NODE`/`LLM_SERVICE` здесь — из `proto.addresses` (обратный импорт `ai_flow` был бы циклом). `image_tools` не импортирует `bot.tools`: эпизод графа в `generate_image` передаётся колбэком `remember`. `delivery.py` не знает про `acquaintance.py` (знакомство проверяют в `tell.py`), поэтому цикла между доставкой и знакомствами нет.
