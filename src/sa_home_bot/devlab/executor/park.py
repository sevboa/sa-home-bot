from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ParkMode:
    active: bool = False
