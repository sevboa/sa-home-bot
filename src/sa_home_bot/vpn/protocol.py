"""Константы протокола службы vpn — намеренно без единого импорта внутри
пакета проекта (только строковые литералы), как memory/protocol.py и
tasks/protocol.py.

Живут отдельно от vpn/service.py, чтобы bot/tools.py и bot/handlers/vpn.py
могли их импортировать, не утягивая саму службу (и её зависимости — работу
с awg-бинарником) в бота.

Служба выдаёт и учитывает доступ к AmneziaWG на jeeves — см. «Этап 33» в
IMPLEMENTATION_PLAN.md для дизайна в целом.
"""

from __future__ import annotations

SERVICE_NAME = "vpn"

# Единственная нода с белым IP — только там имеет смысл поднимать VPN-сервер.
# Должно совпадать с [node].id той ноды, где "vpn" в assignments.
NODE_ID = "jeeves"

# --- Действия ---
ACTION_PEERS = "peers"  # админ: все пиры всех гостей
ACTION_ISSUE = "issue"  # выдать новый конфиг {chat_id, device_label}
ACTION_REISSUE = "reissue"  # перевыпустить: старый пир снимается, новый — тот же гость/label
ACTION_REVOKE = "revoke"  # отозвать {chat_id, device_label}
ACTION_USAGE = "usage"  # с chat_id — свой расход; без — сводка по всем (админ)
ACTION_SET_QUOTA = "set_quota"  # админ: прямой грант месяца {chat_id, bytes}
ACTION_GRANT_EXTRA = "grant_extra"  # гость сам себе +extra_step_gb, пока не упёрся в потолок
ACTION_REQUEST_EXTRA = "request_extra"  # заявка админу сверх потолка самообслуживания
ACTION_RESOLVE_REQUEST = "resolve_request"  # админ: {request_id, approve}
ACTION_APK_INFO = "apk_info"  # метаданные текущего кэша APK (проверяет свежесть)
ACTION_APK_CHUNK = "apk_chunk"  # {offset, length} — кусок файла для передачи через рой
ACTION_APK_SET_FILE_ID = "apk_set_file_id"  # бот сообщает id уже загруженного в Telegram файла

# Мониторинг доступности VPN (найдена нужда 2026-08-17 — см.
# vpn_check/protocol.py и domain/vpn_check.py).
ACTION_REPORT_CHECK = "report_check"  # vpn_check пушит {node, results: {target: {ok, ms, error}}}
ACTION_CHECK_NOW = "check_now"  # админ/nodectl: разослать проверки внеочередно, без ожидания тика
ACTION_CHECK_STATUS = "check_status"  # текущее состояние по всем (node, target)

# --- События ---
EVENT_VPN_PEER_ISSUED = "vpn_peer_issued"
EVENT_VPN_QUOTA_WARNING = "vpn_quota_warning"
EVENT_VPN_QUOTA_EXCEEDED = "vpn_quota_exceeded"
EVENT_VPN_PEER_BLOCKED = "vpn_peer_blocked"
EVENT_VPN_ACCESS_RESTORED = "vpn_access_restored"
EVENT_VPN_EXTRA_REQUESTED = "vpn_extra_requested"
EVENT_VPN_EXTRA_RESOLVED = "vpn_extra_resolved"
# Общий канал VDS приближается к лимиту тарифа (см. [vpn].node_limit_gb) —
# адресуется не гостю, а админам.
EVENT_VPN_NODE_QUOTA_WARNING = "vpn_node_quota_warning"
# Переход (node, target) в/из alerting — эмитится только на самом переходе
# (гистерезис domain/vpn_check.py), не на каждый неуспешный тик, это и есть
# "мут" повторных оповещений об одной и той же проблеме.
EVENT_VPN_CHECK_FAILED = "vpn_check_failed"
EVENT_VPN_CHECK_RECOVERED = "vpn_check_recovered"

# Код ошибки ProtoError: гость упёрся в потолок самообслуживания
# (self_ceiling_gb) — бот на этот код сам оформляет request_extra вместо
# grant_extra, без лишнего похода к пользователю.
ERR_QUOTA_CEILING = "vpn_quota_ceiling"
