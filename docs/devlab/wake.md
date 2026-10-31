# Пробуждение исполнителя (60.3.6, 2026-11-01)

Хаб сам будит ноду исполнителя, когда для неё есть работа, а исполнитель молчит. Код:
`devlab/hub/waker.py` (`ExecutorWaker.step`), цикл `app.wake_loop` рядом с `purge_loop`.
- Работа есть (`HubQueue.pending_work()`): задание `agent` в `queued` либо непрочитанное
  сообщение к исполнителю (answer, resume, stop) по заданию, которое уже кто-то взял. `stop` по
  заданию, которое никто не брал, и `release` не считаются: хаб не будит mycraft ради задания, которое владелец
  отпустил, пока не придёт ответ (после него `answer` или `resume` дадут работу).
- Исполнитель молчит: нет строки `heartbeats` с ролью `executor` (любого токена) свежее
  `HEARTBEAT_FRESH_S` (`HubStore.executor_alive`, та же граница, что у каталога моделей).
- Будит `wake_core.ensure_service_ready(node_link, wake_store, executor_node, EXECUTOR_SERVICE)`:
  тот же механизм, что у `tasks` и бота (поиск MAC, живой ноды в той же сети, `send_wol`, ожидание
  службы). Свой WoL хаб не пишет.
- Шаг раз в `WAKE_STEP_INTERVAL_S` (60 с). Между попытками `WAKE_BACKOFF_S` (10 мин): время
  отсчитывается от начала попытки, в том числе неудачной. Ошибка шага пишется в журнал, цикл живёт.
- Конфиг `[devlab_hub] executor_node` (по умолчанию `mycraft`) нужен только для WoL и
  `ensure_service_ready`; от имени токена исполнителя будильник не зависит (исполнитель один).
- Кэш MAC `wake_core` живёт в `<data_dir>/wake.sqlite` (таблица `app_state` обычного `Store`),
  отдельно от `hub.sqlite`; связь с нодой — `ServiceLink` на сокет локальной ноды, как у `tasks`.
