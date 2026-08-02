"""Адаптер температуры GPU через `nvidia-smi`.

С проприетарным NVIDIA-драйвером `hwmon` температуру не публикует (в отличие
от `nouveau`) — единственный источник это `nvidia-smi` (живая находка при
установке Tesla V100 SXM2 на mycraft, см. CLAUDE.md/память). Один вызов
опрашивает сразу все карты на машине (например V100 + RTX 3060 одновременно),
парсинг вынесен в чистую функцию для тестов на фикстурах.
"""

from __future__ import annotations

import csv
import io
import logging
import subprocess
from datetime import datetime

from sa_home_bot.domain.models import KIND_GPU, SensorReading
from sa_home_bot.utils.requirements import (
    Requirement,
    RequirementStatus,
    requirements_registry,
)

log = logging.getLogger(__name__)

NVIDIA_SMI_TIMEOUT_S = 10

NVIDIA_SMI_REQUIREMENT = Requirement(
    program="nvidia-smi",
    package="nvidia-utils",
    note="температура GPU (nvidia-smi, проприетарный драйвер NVIDIA)",
)

_QUERY_FIELDS = "index,name,temperature.gpu"


def parse_nvidia_smi_csv(output: str, now: datetime) -> list[SensorReading]:
    """Разобрать `nvidia-smi --query-gpu=index,name,temperature.gpu --format=csv,noheader,nounits`.

    Одна строка на карту. Температура ``[N/A]`` (карта её не отдаёт) —
    строка пропускается, а не падает со скана остальных.
    """
    readings: list[SensorReading] = []
    for row in csv.reader(io.StringIO(output)):
        if len(row) < 3:
            continue
        index, name, temp = (c.strip() for c in row[:3])
        if not index:
            continue
        try:
            temp_c = float(temp)
        except ValueError:
            continue
        readings.append(
            SensorReading(
                component_id=f"gpu:{index}",
                kind=KIND_GPU,
                label=name or f"GPU{index}",
                temperature_c=temp_c,
                taken_at=now,
            )
        )
    return readings


def read_gpu_sync(now: datetime) -> list[SensorReading]:
    """Снять температуры всех GPU (блокирующе, через `nvidia-smi`)."""
    diagnosis = NVIDIA_SMI_REQUIREMENT.diagnose()
    if diagnosis is not RequirementStatus.OK:
        requirements_registry.report(NVIDIA_SMI_REQUIREMENT, diagnosis)
        log.warning("nvidia-smi недоступен: %s", NVIDIA_SMI_REQUIREMENT.install_hint())
        return []
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={_QUERY_FIELDS}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=NVIDIA_SMI_TIMEOUT_S,
            check=True,
        )
    except Exception as exc:  # noqa: BLE001 — источник опционален
        log.warning("nvidia-smi не сработал: %s", exc)
        return []
    requirements_registry.report(NVIDIA_SMI_REQUIREMENT, RequirementStatus.OK)
    return parse_nvidia_smi_csv(out.stdout, now)
