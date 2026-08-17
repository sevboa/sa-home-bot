"""Константы протокола службы vpn_check — намеренно без импортов внутри
пакета проекта (только строковые литералы), как net/protocol.py и
tasks/protocol.py.

В отличие от net/tasks, у этой службы нет единственного NODE_ID — она
деплоится на нескольких нодах сразу (jeeves, alfred, ...), см.
``[vpn].check_nodes`` в конфиге ноды jeeves (vpn/service.py).
"""

from __future__ import annotations

SERVICE_NAME = "vpn_check"

# {"targets": ["https://...", ...]} — проверить каждую цель через локальный
# VPN-клиентский туннель и запушить результат в vpn/report_check на jeeves
# (см. vpn_check/service.py, vpn/protocol.py::ACTION_REPORT_CHECK).
ACTION_CHECK = "check"
