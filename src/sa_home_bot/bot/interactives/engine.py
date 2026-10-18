from __future__ import annotations

from sa_home_bot.bot.interactives.acts_flow import ActsMixin
from sa_home_bot.bot.interactives.debug import DebugMixin
from sa_home_bot.bot.interactives.forms import FormsMixin
from sa_home_bot.bot.interactives.found_items import FoundItemsMixin
from sa_home_bot.bot.interactives.item_flow import ItemFlowMixin
from sa_home_bot.bot.interactives.mood_selfie import MoodSelfieMixin
from sa_home_bot.bot.interactives.offers import OffersMixin
from sa_home_bot.bot.interactives.photo_review import PhotoReviewMixin
from sa_home_bot.bot.interactives.photos import PhotosMixin
from sa_home_bot.bot.interactives.positions import PositionsMixin
from sa_home_bot.bot.interactives.search import SearchMixin
from sa_home_bot.bot.interactives.turns import TurnsMixin
from sa_home_bot.bot.interactives.underground_flow import UndergroundMixin


class Interactives(
    TurnsMixin,
    FormsMixin,
    OffersMixin,
    PositionsMixin,
    SearchMixin,
    ActsMixin,
    DebugMixin,
    UndergroundMixin,
    PhotosMixin,
    MoodSelfieMixin,
    PhotoReviewMixin,
    ItemFlowMixin,
    FoundItemsMixin,
):
    pass
