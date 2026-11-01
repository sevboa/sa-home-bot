# Оповещения владельцу о заданиях агентов (Этап 62.2)

Бот (`bot/devlab_notify.py::DevlabNotifier`) раз в `poll_s` смотрит ящик хаба всех клиентов
действием `owner_inbox` (proto, `Address(hub_node, devlab_hub)`) и шлёт админам сообщения о
заданиях агентов. Выключено по умолчанию: `[devlab_notify] enabled = false`, задача не стартует.
Поля конфига: `docs/platform/config-reference.md` (`DevlabNotifyConfig`).

## Курсор
- `app_state["devlab_inbox_cursor"]` — seq последнего обработанного сообщения.
- Нет курсора: `owner_inbox since=-1`, записывается текущий максимум seq, историю не шлём.
- На каждое сообщение: отправка, затем запись курсора. Сообщение, пропущенное фильтром, тоже
  двигает курсор. Дубль возможен только при падении между отправкой и записью курсора.
- Хаб недоступен (`UNREACHABLE_ERRORS`): запись в лог, курсор не двигается, повтор на следующем проходе.

## Фильтр
Оповещаются только задания `kind == agent` и сообщения `question`, `done`, `failed`, `stalled`.
`question` пропускается, если задание уже не `waiting_owner` (ответили по MCP); `stalled` — если
задание уже не `running` (дошло, остановили). `test_done`,
`started`, `step_done`, `stopped` не шлются.

## Текст
Вступление-заглушка (`bot/ai_lines.py`: `DEVLAB_QUESTION_TEXT`, `DEVLAB_DONE_TEXT`,
`DEVLAB_FAILED_TEXT`) и блок фактов: проект, номер, заголовок, модель, токены и $, ветка
`agent/<id>`, текст вопроса / сводка / причина провала. Всё через `html.escape`, тело обрезается
`facts_max_chars`.

## «Работа встала» (Этап 62.6, 2026-11-02)
Сообщение `stalled` кладёт хаб (`docs/devlab/queue.md`, «Застой»). Текст по образцу `failed`:
заглушка `DEVLAB_STALLED_TEXT` или вступление gemma (событие «работа агента встала») и факты:
проект, номер, заголовок, модель, токены, ветка, «Без вестей: N мин», «Исполнитель: на связи /
не отвечает». Кнопка одна: «Остановить» (`stopw`) → «Точно остановить?» (`stopc`) / «Отмена» (`cnlw`,
возвращает кнопку «Остановить»; свои коды, чтобы отмена не показывала кнопки `question`).
MCP-клиент видит `stalled` в `devlab_inbox` как обычное сообщение ящика.

## Вступление через gemma (Этап 62.4)
`DevlabNotifier._compose_lead(kind)`: один вызов `chat` службы `llm` на сообщение (до цикла
по админам, текст общий), `role="persona"`, `think=False`, `chat_id` не передаётся (активные чаты llm получают реплики ухода/рестарта), таймаут
`llm_timeout_s`. В промпт идёт только инструкция по виду события (`LEAD_INSTRUCTION`,
`LEAD_EVENTS`): «одна-две фразы, подробности ниже, чисел и названий не повторяй». Факты, цифры и
текст вопроса в промпт не попадают, их вставляет код. Ответ: `clip(…, 400, at_word=True)`,
`html.escape`, префикс `ALFRED_PREFIX_HTML`.
Сухая заглушка из `ai_lines.py` ставится, если Альфред «в городе» (`AwayService.load()` не `None`,
`chat` не зовётся), нет `node_link`/админов, ответ пуст или `done_reason == "length"`, любое исключение
в теле вступления (лог warning с трассировкой): сбой необязательной фразы не стопорит очередь.
mycraft (хост) ради оповещения не будится: спящая или выключенная нода даёт заглушку. Но если
нода жива, а ollama усыплена по простою, вызов `chat` поднимет модель.

## Доставка
Каждому админу (`WILDCARD in allowed_commands`) `Notifier.send_direct(outbox_kind=task,
dedup_key="devlab:<seq>")`: при временном сбое Telegram сообщение уходит в outbox.
`broadcast` не подходит: возвращает число доставок, а не `message_id`.

## Кнопки (Этап 62.3)
`bot/handlers/devlab.py`, роутер включён сразу после `away`, ловит только callback `dl:`.
Каждое нажатие проверяет `subscription.is_owner` и `from_user.id == subscription.chat_id` (не одно
`allows_action`); чужим `DENIED_ALERT_TEXT`.
- Кнопки (`bot/devlab_notify.py`, `callback_data = dl:<код>:<job_id>`): `question` — «Отпустить
  спать» (`rel`), «Остановить» (`stop`); `failed` — «Продолжить» (`res`); `done` без кнопок.
- `stop` не шлёт ничего, а меняет клавиатуру на «Точно остановить?» (`stopc`) и «Отмена» (`cnl`,
  возвращает клавиатуру вопроса). `rel`/`stopc`/`res` → `owner_post id kind`; успех: toast со
  статусом, клавиатура снята, `DevlabNotifier.kick()` (если опрос включён). Отказ хаба
  (`bad_request`): toast с текстом, клавиатура снята. Хаб недоступен: toast, клавиатура остаётся.
- В оповещении `question` последней строкой `DEVLAB_ANSWER_HINT`: ответить агенту можно через
  Claude (`devlab_answer` по MCP).
- Старые кнопки «Ответить» (`dl:ans:…`) в уже отправленных сообщениях: нажатие просто гасит
  часики, ничего не делает.

## Ответ агенту из Telegram убран (решение владельца 2026-11-02, 62.3.1)
Кнопка «Ответить», ForceReply-подсказка, `DevlabReplyFilter` и ключи `app_state`
`devlab_notice:*`/`devlab_reply:*` удалены. Реплай владельца на оповещение идёт в обычный диалог с
Альфредом. Отвечать агенту — через Claude (`devlab_answer`); позже прямой диалог с агентами
переедет в отдельный Telegram-бот мастерской. `owner_post` хаба не менялся, `kind=answer` из бота
просто больше не шлётся. Оставшиеся на проде ключи `devlab_notice:*`/`devlab_reply:*`
`DevlabNotifier.run` один раз сносит при старте (`purge_legacy_keys`); убрать вместе с
константой `LEGACY_STATE_PREFIXES`, когда выкат пройдёт.

Урок 2026-11-02: в проде реплай «go» на подсказку ушёл Альфреду, а не в хаб.
`AuthorizationMiddleware` висит inner-middleware на `dp.message`, а aiogram зовёт фильтры роутера ДО
inner-middleware, поэтому `subscription` в фильтре всегда была `None`, и фильтр молча отвечал False
(юнит-тесты передавали `subscription` руками). Фильтру видны только workflow-данные
`start_polling` (`store`, `book`, …) и то, что кладут outer-middleware. Кнопки не страдали: их фильтр
только `F.data`, а `subscription` получает уже хендлер. Страж для такого класса ошибок —
`tests/unit/test_devlab_dispatch.py`: апдейты идут через настоящий `build_dispatcher` +
`feed_update` с workflow-данными как в `app.py`.
