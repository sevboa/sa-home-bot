from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sa_home_bot.utils.clock import utc_now
from sa_home_bot.utils.timefmt import format_dhms


@dataclass
class Runtime:
    started_at: datetime = field(default_factory=utc_now)

    def uptime_seconds(self) -> float:
        return (utc_now() - self.started_at).total_seconds()


def format_duration(seconds: float) -> str:
    return format_dhms(seconds)
