from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from sa_home_bot.config import Settings

PUBLISH_MARKER = "backup-publish.ok"


def marker_path(settings: Settings) -> Path:
    return Path(settings.node.state_path).parent / PUBLISH_MARKER


def publish_allowed(settings: Settings) -> bool:
    return marker_path(settings).exists()


def allow_publish(settings: Settings, reason: str) -> Path:
    path = marker_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"{datetime.now(tz=UTC).isoformat()} {reason}\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


HOLD_HINT = (
    "публикация бэкапа приостановлена: нет маркера backup-publish.ok. После пересборки "
    "сервера восстановите данные (`sa-home-bot backup restore <нода> --apply` на alfred), "
    "для нового сервера или первого деплоя — `nodectl backup-release`"
)
