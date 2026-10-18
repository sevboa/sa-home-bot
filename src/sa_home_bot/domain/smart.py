from __future__ import annotations

from sa_home_bot.domain.models import (
    DISK_FAIL,
    DISK_OK,
    DISK_WARN,
    EVENT_SMART_DEGRADED,
    EVENT_SMART_RECOVERED,
    SmartAttrChange,
    SmartChange,
    SmartSnapshot,
)

MONITORED_SMART_ATTRS: dict[int, str] = {
    5: "Reallocated_Sector_Ct",
    197: "Current_Pending_Sector",
    198: "Offline_Uncorrectable",
    199: "UDMA_CRC_Error_Count",
}

_HEALTH_RANK = {DISK_OK: 0, DISK_WARN: 1, DISK_FAIL: 2}


def diff_smart(prev: SmartSnapshot | None, curr: SmartSnapshot) -> SmartChange | None:
    if prev is None:
        return None

    attr_changes: list[SmartAttrChange] = []
    worsened = False
    improved = False
    for attr_id, name in MONITORED_SMART_ATTRS.items():
        old = prev.attrs.get(attr_id)
        new = curr.attrs.get(attr_id)
        if old is None or new is None or old == new:
            continue
        attr_changes.append(SmartAttrChange(attr_id, name, old, new))
        if new > old:
            worsened = True
        else:
            improved = True

    rank_prev = _HEALTH_RANK.get(prev.health)
    rank_curr = _HEALTH_RANK.get(curr.health)
    if rank_prev is not None and rank_curr is not None:
        if rank_curr > rank_prev:
            worsened = True
        elif rank_curr < rank_prev:
            improved = True

    if not worsened and not improved:
        return None

    return SmartChange(
        component_id=curr.component_id,
        label=curr.label,
        event_type=EVENT_SMART_DEGRADED if worsened else EVENT_SMART_RECOVERED,
        health_from=prev.health,
        health_to=curr.health,
        attr_changes=tuple(attr_changes),
        at=curr.taken_at,
    )
