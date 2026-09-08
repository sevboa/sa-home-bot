"""Константы протокола службы ``reality`` — намеренно без единого импорта
внутри пакета проекта (только строковые литералы), как ``vpn/protocol.py`` и
``memory/protocol.py``.

Живут отдельно от ``reality/service.py``, чтобы ``bot/`` мог импортировать их,
не утягивая саму службу (и её зависимость — вызовы бинарника ``xray``) в
процесс бота.

Служба выдаёт и учитывает доступ VLESS+Reality (xray-core) на ноде роя с
белым IP — единственный транспорт, реально проходящий ТСПУ из РФ на
зарубежный сервер (см. подэтап 39.0.x в IMPLEMENTATION_PLAN.md и
``~/.claude/plans/functional-imagining-otter.md``).

Служба мультинодовая с рождения (как ``vpn`` после 39.0.2): адресата бот
находит динамически по списку служб (``bot/reality_nodes.py``), хардкода
``NODE_ID`` тут нет.
"""

from __future__ import annotations

SERVICE_NAME = "reality"

# --- Действия ---
ACTION_PEERS = "peers"  # админ: все пиры всех гостей
ACTION_ISSUE = "issue"  # выдать новый конфиг {chat_id}
ACTION_REISSUE = "reissue"  # перевыпустить: старый пир снимается, новый — тот же гость/label
ACTION_REVOKE = "revoke"  # отозвать {chat_id, device_label}
ACTION_USAGE = "usage"  # с chat_id — свой расход; без — сводка по всем (админ)
ACTION_SET_QUOTA = "set_quota"  # админ: прямой грант месяца {chat_id, bytes}
ACTION_GRANT_EXTRA = "grant_extra"  # гость сам себе +extra_step_gb, пока не упёрся в потолок
ACTION_REQUEST_EXTRA = "request_extra"  # заявка админу сверх потолка самообслуживания
ACTION_RESOLVE_REQUEST = "resolve_request"  # админ: {request_id, approve}

# --- События ---
EVENT_REALITY_PEER_ISSUED = "reality_peer_issued"
EVENT_REALITY_QUOTA_WARNING = "reality_quota_warning"
EVENT_REALITY_QUOTA_EXCEEDED = "reality_quota_exceeded"
EVENT_REALITY_PEER_BLOCKED = "reality_peer_blocked"
EVENT_REALITY_ACCESS_RESTORED = "reality_access_restored"
EVENT_REALITY_EXTRA_REQUESTED = "reality_extra_requested"
EVENT_REALITY_EXTRA_RESOLVED = "reality_extra_resolved"
# Общий канал VDS приближается к лимиту тарифа ([reality].node_limit_gb) —
# адресуется не гостю, а админам.
EVENT_REALITY_NODE_QUOTA_WARNING = "reality_node_quota_warning"

# Код ошибки ProtoError: гость упёрся в потолок самообслуживания
# (self_ceiling_gb) — бот на этот код сам оформляет request_extra вместо
# grant_extra, без лишнего похода к пользователю.
ERR_QUOTA_CEILING = "reality_quota_ceiling"
