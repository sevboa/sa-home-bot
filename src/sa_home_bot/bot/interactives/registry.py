from __future__ import annotations

from sa_home_bot.bot.interactives import cellar, radio, underground
from sa_home_bot.bot.interactives.base import Scenario

REGISTRY: dict[str, Scenario] = {
    radio.RADIO.id: radio.RADIO,
    cellar.CELLAR.id: cellar.CELLAR,
    underground.CATACOMBS.id: underground.CATACOMBS,
}
