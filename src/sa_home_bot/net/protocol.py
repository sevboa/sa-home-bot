"""Константы протокола службы net — намеренно без единого импорта внутри
пакета проекта (только строковые литералы), как tasks/protocol.py.

Живут отдельно от net/service.py, чтобы bot/tools.py (тул web_search) мог их
импортировать, не утягивая саму службу.
"""

from __future__ import annotations

SERVICE_NAME = "net"

# Нода, на которой развёрнут SearXNG и эта служба-обёртка — тот же приём, что
# NODE_ID в tasks/protocol.py. alfred выбран как единственная круглосуточная
# headless-нода: winpc спит и перезагружается под игры, и утяжелять его и без
# того хрупкий GPU-стек (WSL2 + Docker) не относящимся к инференсу сервисом
# не стоит. Должно совпадать с [node].id той ноды, где "net" в assignments.
NODE_ID = "alfred"

ACTION_SEARCH = "search"
