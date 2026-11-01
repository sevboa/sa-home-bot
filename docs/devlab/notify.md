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

## Кнопки и ответ (Этап 62.3)
`bot/handlers/devlab.py`, роутер включён сразу после `away`. Каждое нажатие и каждый реплай
проверяют `subscription.is_owner` (не одно `allows_action`); чужим `DENIED_ALERT_TEXT`, реплаи
чужих идут дальше по роутерам.
- Кнопки (`bot/devlab_notify.py`, `callback_data = dl:<код>:<job_id>`): `question` — «Ответить»
  (`ans`), «Отпустить спать» (`rel`), «Остановить» (`stop`); `failed` — «Продолжить» (`res`);
  `done` без кнопок.
- `stop` не шлёт ничего, а меняет клавиатуру на «Точно остановить?» (`stopc`) и «Отмена» (`cnl`,
  возвращает клавиатуру вопроса). `rel`/`stopc`/`res` → `owner_post id kind`; успех: toast со
  статусом, клавиатура снята, `DevlabNotifier.kick()` (если опрос включён). Отказ хаба
  (`bad_request`): toast с текстом, клавиатура снята. Хаб недоступен: toast, клавиатура остаётся.
- `ans` → `owner_job`; не `waiting_owner` — toast «Задание уже <статус>», клавиатура снята; иначе
  ForceReply и `app_state["devlab_reply:<chat_id>:<message_id>"] = {job_id, user_id, created}`,
  срок 24 ч.
- `DevlabReplyFilter`: реплай владельца (текст не пуст, не команда) на ForceReply-подсказку (тот же
  пользователь) или на оповещение `question` (ключ `devlab_notice`) → `owner_post kind=answer`,
  ключ удаляется, ответ «Передал агенту.». Просроченная подсказка: ответ «подсказка устарела» и в
  диалог не уходит. Отказ хаба: текст ошибки, ключ удаляется. Состояние в `app_state`, поэтому
  переживает рестарт.
