# Журнал датированных находок зоны node-core

Подробности — в тематических файлах рядом.

| Дата | Что | Где |
|---|---|---|
| 2026-07-17 | Шторм ретрансляции событий в рое из 3 нод, Telegram 429 (retry-after 578 с) | swarm-links (SeenEvents) |
| 2026-07-17 | LHM: пороги NVMe принимались за показания; гонка инициализации pythonnet; WinError 5 при pipx | sensors, lifecycle |
| 2026-07-18 | smartctl на каждый запрос делал /status и /swarm медленными -> кэш дисков | sensors |
| 2026-07-20 | TCP без keepalive не замечает пропавшего собеседника; зависший `hw.Update()` | swarm-links, sensors |
| 2026-07-23 | WSL из Session-0 не запускается -> llm внешне управляемая; presence-таймаут | services, wake |
| 2026-07-24 | `task_result` терялся из-за эхо-проверки локальных событий; осиротевшая /ai-задача при останове | swarm-links, lifecycle |
| 2026-07-25 | 3 с presence ловило ложные «недоступна» -> 6 с | wake |
| 2026-07-27 | Кэш WoL не наполнялся у tasks; 90 с мало для холодного старта | wake |
| 2026-07-28 | Heartbeat линков, инкарнация, TCP_USER_TIMEOUT, bind tailscale-адреса, фабрики линков, llm в рое | swarm-links, lifecycle |
| 2026-07-30 | Прогрев 180 с мало для холодной модели -> 360 с | wake |
| 2026-08-03 | tmux переживает разрыв SSH; llm_went_idle vs llm_idle_sleep | lifecycle, swarm-links |
| 2026-08-04 | Стухшие выученные адреса, линк «к себе»; `up_to_date` без учёта running | swarm-links, lifecycle |
| 2026-08-05 | `restart_applied` не долетал (emit в пустоту после старта) | swarm-links |
| 2026-08-08 | Падение процесса бота терялось, служба «ничья» ~18 мин; необработанное исключение в supervisor._run | leases, lifecycle |
| 2026-08-12 | Гонка proc.wait/slot.stop маскировала падение; сеть после ребута и bounded retry get_me | leases, lifecycle |
| 2026-08-26 | CRASHED vs UNREACHABLE в ensure_service_ready | wake |
| 2026-09-22/23 | hybrid-sleep ломает GPU; возвращён poweroff | lifecycle |
| 2026-09-24 | pipx терял экстры (numpy 2.5.3 сломал TTS) | lifecycle |
| 2026-09-25 | Таймаут одного запроса рвал линк с mycraft | swarm-links |
