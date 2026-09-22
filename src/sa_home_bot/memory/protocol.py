"""Константы протокола службы memory — намеренно без единого импорта внутри
пакета проекта (только строковые литералы), как net/protocol.py и
tasks/protocol.py.

Живут отдельно от memory/service.py, чтобы bot/tools.py и bot/ai_flow.py
могли их импортировать, не утягивая саму службу (и её зависимости) в бота.
"""

from __future__ import annotations

SERVICE_NAME = "memory"

# Этап 42.1 (2026-09-23): перенесена с alfred на mycraft. Прежний инвариант
# «обязана быть доступна ВСЕГДА» не подтвердился реальным использованием —
# recall_facts (bot/ai_flow.py) и tool_memory (bot/tools.py) дёргаются
# только внутри /ai-разговора, а тот и так не ответит, пока mycraft не
# проснулась (там персонажная модель). Держать память на отдельной
# круглосуточной ноде не давало пользы, только требовало сетевого прыжка.
# Недоступность mycraft (сон, перезагрузка) — предусмотренная деградация:
# recall_facts отдаёт [], tool_memory — текст «недоступно» (тот же приём,
# что и у graph_memory, см. graph_memory/protocol.py::NODE_ID).
# Должно совпадать с [node].id той ноды, где "memory" в assignments.
NODE_ID = "mycraft"

ACTION_REMEMBER = "remember"
ACTION_RECALL = "recall"
ACTION_FORGET = "forget"
ACTION_LIST = "list"

# Область видимости факта. "chat" — знает только тот разговор, где сказали;
# "family" — видно из чатов, перечисленных в [memory].family_chat_ids (свои
# люди, но не любой гость); "common" — общее знание дома (кто такой Альфред,
# как он выглядит, что умеет рой), видно из любого чата. Границу держит запрос
# к БД, а не обещание модели молчать (см. memory/service.py::_recall).
SCOPE_CHAT = "chat"
SCOPE_FAMILY = "family"
SCOPE_COMMON = "common"
