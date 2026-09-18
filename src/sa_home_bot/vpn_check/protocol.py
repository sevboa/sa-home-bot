"""Константы протокола службы vpn_check — намеренно без импортов внутри
пакета проекта (только строковые литералы), как net/protocol.py и
tasks/protocol.py.

В отличие от net/tasks, у этой службы нет единственного NODE_ID — она
деплоится на нескольких нодах сразу (jeeves, alfred, ...), см.
``[vpn].check_nodes`` в конфиге ноды jeeves (vpn/service.py).
"""

from __future__ import annotations

SERVICE_NAME = "vpn_check"

# {"server": "<кого проверяем>", "targets": ["https://...", ...]} —
# проверить каждую цель через локальный VPN-клиентский туннель, если он
# ведёт именно к этому server (см. VpnCheckConfig.probe_server,
# self-exclude при server == своя нода), и запушить результат фанаутом на
# все живые vpn-инстансы (vpn_check/service.py, vpn/protocol.py::
# ACTION_REPORT_CHECK). server — этап 39.0.7 (2026-09-18): раньше
# проверялся один статичный список целей без привязки к серверу.
ACTION_CHECK = "check"
