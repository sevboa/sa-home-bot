"""Дельта SMART-снимков → событие деградации/восстановления. Чистые функции.

Отслеживаемые атрибуты — счётчики деградации, которые в исправном диске НЕ
растут; любой рост = тревога, снижение (например перезапись pending-сектора) =
восстановление. Здесь только сравнение двух снимков; чтение smartctl — в
``sensors.disks``. Без БД, сети и aiogram — тестируется изолированно.
"""

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

# Отслеживаемые SMART-атрибуты: id -> человекочитаемое имя. Все read-only,
# чистые счётчики (в норме постоянны); их рост — надёжный ранний признак
# умирающего диска. Значение берём как ведущее число ``raw.string`` — ``raw.value``
# у части дисков пакует доп. поля (напр. Hitachi отдаёт 589855 вместо 31).
#
# Атрибут 187 (Reported_Uncorrect) сознательно НЕ отслеживаем: его raw ненадёжен
# (65535-заглушка / упакованное значение у разных вендоров) и давал бы ложные
# тревоги. Его реальная деградация всё равно всплывает через 197/198 и общий
# smart_status (см. domain.render._health_word / sensors.disks.parse_health).
MONITORED_SMART_ATTRS: dict[int, str] = {
    5: "Reallocated_Sector_Ct",  # переназначенные сектора
    197: "Current_Pending_Sector",  # кандидаты на переназначение (сбойные)
    198: "Offline_Uncorrectable",  # нечитаемые при offline-скане
    199: "UDMA_CRC_Error_Count",  # ошибки интерфейса (кабель/USB-мост)
}

# Порядок классов здоровья по тяжести: ok < warning < failed.
_HEALTH_RANK = {DISK_OK: 0, DISK_WARN: 1, DISK_FAIL: 2}


def diff_smart(prev: SmartSnapshot | None, curr: SmartSnapshot) -> SmartChange | None:
    """Сравнить прошлый и текущий снимок; вернуть событие при значимом изменении.

    Первое наблюдение (``prev is None``) события не даёт — только фиксирует
    baseline. Рост любого счётчика или ухудшение класса здоровья → degraded
    (деградация доминирует). Иначе снижение счётчика/улучшение класса →
    recovered. Если значимых сигналов нет — None (в т.ч. когда SMART только стал
    доступен: None → ok не считается изменением).
    """
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
