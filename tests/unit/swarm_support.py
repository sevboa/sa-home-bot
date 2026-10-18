from sa_home_bot.bot.service_link import ServiceUnavailableError
from tests.unit.support.fakes import FakeNodeLink


class DeadNodeLink(FakeNodeLink):
    def __init__(self) -> None:
        super().__init__(error=ServiceUnavailableError("нет связи"))

    async def get_state(self, dst=None):
        raise ServiceUnavailableError("нет связи")


def callback_codes(keyboard) -> list[str]:
    if keyboard is None:
        return []
    return [b.callback_data for row in keyboard.inline_keyboard for b in row]
