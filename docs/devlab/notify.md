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
Оповещаются только задания `kind == agent` и сообщения `question`, `done`, `failed`.
`question` пропускается, если задание уже не `waiting_owner` (ответили по MCP). `test_done`,
`started`, `step_done`, `stopped` не шлются.

## Текст
Вступление-заглушка (`bot/ai_lines.py`: `DEVLAB_QUESTION_TEXT`, `DEVLAB_DONE_TEXT`,
`DEVLAB_FAILED_TEXT`) и блок фактов: проект, номер, заголовок, модель, токены и $, ветка
`agent/<id>`, текст вопроса / сводка / причина провала. Всё через `html.escape`, тело обрезается
`facts_max_chars`. Вступление через gemma добавляет 62.4.

## Доставка
Каждому админу (`WILDCARD in allowed_commands`) `Notifier.send_direct(outbox_kind=task,
dedup_key="devlab:<seq>")`: при временном сбое Telegram сообщение уходит в outbox.
`broadcast` не подходит: возвращает число доставок, а не `message_id`.

## Привязка ответа к заданию
Для `question` при успешной немедленной отправке пишется
`app_state["devlab_notice:<chat_id>:<message_id>"] = job_id` (по нему 62.3 принимает реплай).
При разбиении на чанки в `devlab_notice` пишется `message_id` первого чанка.
Если отправка ушла в outbox, `message_id` неизвестен и запись не создаётся: реплай на такое
сообщение 62.3 не свяжет, остаётся кнопка «Ответить».
