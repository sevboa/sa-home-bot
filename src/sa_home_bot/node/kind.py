"""Тип машины, на которой развёрнута нода, и выводимые из него свойства.

Рой равноправен (ARCHITECTURE §11 п.1) — тип НЕ делает ноду главной и не даёт
ей особых прав. Он отвечает на вопросы, ответы на которые раньше были нигде не
записаны и подразумевались:

- alfred должен быть включён всегда, его пропажа — авария;
- winpc может быть выключен, это норма, зато его можно разбудить по WoL;
- jeeves — виртуалка: включена всегда, но термозон и SMART у неё нет, монитор
  туда ставить бессмысленно.

Плюс тип задаёт базовый приоритет в аренде лидерства служб-синглтонов
(`node/lease.py`): при прочих равных синглтон живёт на машине, которая с
большей вероятностью включена.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

NodeKind = Literal["server", "workstation", "vps"]

KIND_SERVER: NodeKind = "server"
KIND_WORKSTATION: NodeKind = "workstation"
KIND_VPS: NodeKind = "vps"


@dataclass(frozen=True)
class NodeTraits:
    """Что следует из типа машины.

    ``always_on`` — машина обязана быть в сети: длительная недоступность это
    авария, о которой рой сообщает (событие ``node_down``). У ноды с
    ``always_on=False`` недоступность — норма и молчит.

    ``wakeable`` — машину осмысленно будить magic-пакетом (WoL): только для
    той, что штатно бывает выключена и физически стоит в локальной сети.

    ``hardware_sensors`` — у машины есть настоящие термозоны и диски со SMART.

    ``lease_priority`` — база приоритета в аренде синглтонов; больше = охотнее
    держит службу. Явный ``prio=N`` в назначении перебивает это значение.
    """

    always_on: bool
    wakeable: bool
    hardware_sensors: bool
    lease_priority: int

    @property
    def alerts_when_unreachable(self) -> bool:
        """Сообщать ли рою о длительной недоступности этой ноды."""
        return self.always_on


TRAITS: dict[str, NodeTraits] = {
    # Домашний сервер: 24/7, реальное железо, первый кандидат на синглтоны.
    KIND_SERVER: NodeTraits(
        always_on=True, wakeable=False, hardware_sensors=True, lease_priority=100
    ),
    # Виртуалка: 24/7, но датчиков железа нет; резерв синглтонов — её роль.
    KIND_VPS: NodeTraits(
        always_on=True, wakeable=False, hardware_sensors=False, lease_priority=50
    ),
    # Рабочая станция/ноутбук: спит и выключается — это норма, зато будится.
    KIND_WORKSTATION: NodeTraits(
        always_on=False, wakeable=True, hardware_sensors=True, lease_priority=10
    ),
}

# Тип неизвестной (например, более старой) ноды: считаем её обычным сервером —
# самое безобидное допущение, кроме одного, поэтому alerts о ней не шлём
# (см. traits_for: неизвестный тип отдаёт always_on=False).
_UNKNOWN = NodeTraits(always_on=False, wakeable=False, hardware_sensors=True, lease_priority=0)


def traits_for(kind: str | None) -> NodeTraits:
    """Свойства по типу. Незнакомый/пустой тип — консервативный набор: ни
    алертов о пропаже, ни WoL, минимальный приоритет аренды. Так нода старой
    версии в рое не начинает шуметь и не отбирает синглтон."""
    if not kind:
        return _UNKNOWN
    return TRAITS.get(kind, _UNKNOWN)


def known_kinds() -> tuple[str, ...]:
    return (KIND_SERVER, KIND_WORKSTATION, KIND_VPS)
