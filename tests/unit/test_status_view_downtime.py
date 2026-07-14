"""status_view.build_downtime_page: история отключений по протоколу с dst."""

from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.bot.status_view import (
    DOWNTIME_UNSUPPORTED_TEXT,
    MONITOR_DOWN_TEXT,
    build_downtime_page,
)
from sa_home_bot.proto.messages import ERR_INTERNAL, ERR_UNKNOWN_ACTION, ProtoError

_EVENT = {
    "kind": "unexpected",
    "boot_at": "2026-07-05T00:23:52+00:00",
    "down_at": "2026-07-04T15:12:00+00:00",
    "up_at": None,
    "down_approx": True,
    "downtime_s": None,
}


class FakeNodeLink:
    display_name = "нода"

    def __init__(self, result=None, error: Exception | None = None):
        self._result = result or {"events": [], "offset": 0, "has_next": False}
        self._error = error
        self.calls: list[tuple[str, dict, object]] = []

    async def command(self, action, args=None, dst=None):
        if self._error is not None:
            raise self._error
        self.calls.append((action, args or {}, dst))
        return self._result


async def test_downtime_calls_monitor_of_target_node():
    link = FakeNodeLink(result={"events": [_EVENT], "offset": 0, "has_next": True})
    text, keyboard = await build_downtime_page(link, node_id="arch-t480")

    action, args, dst = link.calls[0]
    assert action == "downtime"
    assert args == {"offset": 0, "limit": 10}
    assert dst.node == "arch-t480" and dst.service == "monitor"
    assert "⚡" in text or "отключ" in text.lower()  # событие отрендерено
    # has_next → кнопка следующей страницы несёт node_id.
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert callbacks == ["st:downtime_page:10:arch-t480"]


async def test_downtime_own_node_without_id_segment():
    link = FakeNodeLink(result={"events": [_EVENT], "offset": 10, "has_next": False})
    _, keyboard = await build_downtime_page(link, offset=10)
    _, _, dst = link.calls[0]
    assert dst.node is None and dst.service == "monitor"
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert callbacks == ["st:downtime_page:0"]  # без node_id-сегмента, как раньше


async def test_downtime_old_monitor_says_update():
    link = FakeNodeLink(error=ProtoError(ERR_UNKNOWN_ACTION, "нет такого действия"))
    text, keyboard = await build_downtime_page(link, node_id="arch-t480")
    assert text == DOWNTIME_UNSUPPORTED_TEXT
    assert keyboard is None


async def test_downtime_unavailable_monitor():
    link = FakeNodeLink(error=ServiceUnavailableError("нет связи"))
    text, keyboard = await build_downtime_page(link)
    assert text == MONITOR_DOWN_TEXT
    assert keyboard is None


async def test_downtime_other_proto_error_is_monitor_down():
    link = FakeNodeLink(error=ProtoError(ERR_INTERNAL, "внутренняя ошибка"))
    text, _ = await build_downtime_page(link)
    assert text == MONITOR_DOWN_TEXT
