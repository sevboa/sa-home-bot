from __future__ import annotations

from sa_home_bot.bot.interactives import cellar, radio, underground
from sa_home_bot.bot.interactives.base import Scenario

# Порядок важен: радио первым (до Этапа 59 он был единственным), канареечные
# сценарии после — гостю вне списка они недоступны (Interactives.scenarios_for).
REGISTRY: dict[str, Scenario] = {
    radio.RADIO.id: radio.RADIO,
    cellar.CELLAR.id: cellar.CELLAR,
    underground.CATACOMBS.id: underground.CATACOMBS,
}
