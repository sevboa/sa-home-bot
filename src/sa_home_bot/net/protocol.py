"""Константы протокола службы net — намеренно без единого импорта внутри
пакета проекта (только строковые литералы), как tasks/protocol.py.

Живут отдельно от net/service.py, чтобы bot/tools.py (тул web_search) мог их
импортировать, не утягивая саму службу.
"""

from __future__ import annotations

SERVICE_NAME = "net"

# Этап 42.1 (2026-09-23): перенесена с alfred на mycraft вместе с memory —
# та же логика (см. memory/protocol.py::NODE_ID). tool_web_search дёргается
# только внутри /ai-разговора, который и так не ответит, пока mycraft не
# проснулась, так что круглосуточная доступность net на отдельной ноде была
# невостребованной. Недоступность mycraft — деградация: web_search в этом
# случае отвечает "недоступно", а не роняет весь /ai-ответ.
# Должно совпадать с [node].id той ноды, где "net" в assignments.
NODE_ID = "mycraft"

ACTION_SEARCH = "search"
