from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramBadRequest

from sa_home_bot.bot.ui.callback_data import int_or, pack, unpack, unpack_id_and_button
from sa_home_bot.bot.ui.keyboards import button, markup, single_button
from sa_home_bot.bot.ui.redraw import redraw
from tests.unit.support.fakes import FakeCallback


def test_pack_matches_legacy_formats():
    assert pack("sw", "node", "alfred", 5) == "sw:node:alfred:5"
    assert pack("pa", 12, "ok") == "pa:12:ok"
    assert pack("x") == "x"


@pytest.mark.parametrize(
    ("data", "parts", "expected"),
    [
        ("p:a:b", 2, ("a", "b")),
        ("p:a", 1, ("a",)),
        ("p:a:b", 1, None),
        ("q:a:b", 2, None),
        ("", 2, None),
        (None, 2, None),
    ],
)
def test_unpack(data, parts, expected):
    assert unpack(data, "p", parts) == expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [("p:12:ok", (12, "ok")), ("p:x:ok", None), ("p:-1:ok", None), ("p:12", None), (None, None)],
)
def test_unpack_id_and_button(data, expected):
    assert unpack_id_and_button(data, "p") == expected


@pytest.mark.parametrize(
    ("raw", "default", "expected"),
    [("7", 0, 7), (None, 3, 3), ("abc", 5, 5), ("", 2, 2), ("-4", 0, -4)],
)
def test_int_or(raw, default, expected):
    assert int_or(raw, default) == expected


def test_keyboard_builders():
    one = single_button("Назад", "back")
    assert one.inline_keyboard[0][0].callback_data == "back"
    grid = markup([button("a", "1"), button("b", "2")], [button("c", "3")])
    assert [[b.text for b in row] for row in grid.inline_keyboard] == [["a", "b"], ["c"]]


async def test_redraw_edits_and_swallows_bad_request():
    callback = FakeCallback("x")
    await redraw(callback, "текст", None)
    assert callback.message.edits == ["текст"]

    async def reject(*_a, **_k):
        raise TelegramBadRequest(method=None, message="not modified")  # type: ignore[arg-type]

    callback.message.edit_text = reject
    await redraw(callback, "ещё", None)


async def test_redraw_without_message_is_noop_and_forwards_options():
    callback = FakeCallback("x")
    seen = {}

    async def edit(text, reply_markup=None, **options):
        seen.update(text=text, options=options)

    callback.message.edit_text = edit
    await redraw(callback, "t", None, disable_web_page_preview=True)
    assert seen == {"text": "t", "options": {"disable_web_page_preview": True}}
    callback.message = None
    await redraw(callback, "t", None)
