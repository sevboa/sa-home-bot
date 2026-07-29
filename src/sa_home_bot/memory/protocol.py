"""Константы протокола службы memory — намеренно без единого импорта внутри
пакета проекта (только строковые литералы), как net/protocol.py и
tasks/protocol.py.

Живут отдельно от memory/service.py, чтобы bot/tools.py и bot/ai_flow.py
могли их импортировать, не утягивая саму службу (и её зависимости) в бота.
"""

from __future__ import annotations

SERVICE_NAME = "memory"

# Нода, на которой живёт память. Как и у net — alfred: память обязана быть
# доступна ВСЕГДА, а winpc штатно спит и выключается (kind=workstation).
# Должно совпадать с [node].id той ноды, где "memory" в assignments.
NODE_ID = "alfred"

ACTION_REMEMBER = "remember"
ACTION_RECALL = "recall"
ACTION_FORGET = "forget"
ACTION_LIST = "list"

# Область видимости факта. "chat" — знает только тот разговор, где сказали;
# "common" — общее знание дома (кто такой Альфред, как он выглядит, что умеет
# рой), видно из любого чата. Границу держит запрос к БД, а не обещание
# модели молчать (см. memory/service.py::_recall).
SCOPE_CHAT = "chat"
SCOPE_COMMON = "common"
