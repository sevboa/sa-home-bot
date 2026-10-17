from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.proto.messages import Address
from tests.unit.support.factories import make_book, make_subscription, png_bytes
from tests.unit.support.fakes import (
    Clock,
    FakeBot,
    FakeCallback,
    FakeMessage,
    FakeNodeLink,
    FakeNotifier,
)
from tests.unit.support.waiting import wait_for, wait_until


async def test_fake_notifier_records_and_numbers_messages():
    notifier = FakeNotifier()
    first = await notifier.send_direct(1, "a", reply_markup="kb", reply_to_message_id=9)
    second = await notifier.send_direct(2, "b")
    assert (first, second) == (1001, 1002)
    assert notifier.sent == [(1, "a"), (2, "b")]
    assert notifier.sent_with_markup[0] == (1, "a", "kb")
    assert notifier.reply_tos == [9, None]
    assert notifier.texts(2) == ["b"]
    assert await FakeNotifier(fail=True).send_direct(1, "x") is None
    assert await FakeNotifier(send_direct_result=1).send_direct(1, "x") == 1


async def test_fake_node_link_routes_results_and_errors():
    dst = Address(node="mycraft", service="llm")
    link = FakeNodeLink({"node": "alfred"}, routes={"mycraft:llm": {"node": "mycraft"}},
                        results={"ping": {"pong": 1}}, result={"ok": True})
    assert await link.get_state() == {"node": "alfred"}
    assert await link.get_state(dst) == {"node": "mycraft"}
    with pytest.raises(ServiceUnavailableError):
        await link.get_state(Address(node="x", service="y"))
    assert await link.command("ping") == {"pong": 1}
    assert await link.command("other", {"a": 1}, dst) == {"ok": True}
    assert link.commands == [("ping", {}), ("other", {"a": 1})]
    failing = FakeNodeLink(error=ServiceUnavailableError("x"))
    with pytest.raises(ServiceUnavailableError):
        await failing.command("any")


async def test_fake_message_callback_and_bot():
    callback = FakeCallback("d", chat_id=5)
    await callback.message.answer("hi", reply_markup="m")
    await callback.message.edit_text("e", reply_markup="k")
    await callback.answer("ok", show_alert=True)
    assert callback.message.chat.id == 5
    assert callback.message.answers == ["hi"] and callback.message.edit_keyboards == ["k"]
    assert callback.answered == [(("ok",), {"show_alert": True})]
    bot = FakeBot({"f": b"bytes"})
    assert (await bot.download(type("F", (), {"file_id": "f"})())).read() == b"bytes"
    assert FakeMessage(user_id=3).from_user.id == 3 and FakeMessage().from_user is None


def test_clock_datetime_and_float():
    moment = Clock(datetime(2026, 1, 1, tzinfo=UTC))
    moment.advance(hours=1, seconds=30)
    assert moment() == datetime(2026, 1, 1, 1, 0, 30, tzinfo=UTC)
    ticks = Clock()
    ticks.advance(5)
    assert ticks() == ticks.t == 1005.0


def test_factories():
    book = make_book(make_subscription(1, "owner"), make_subscription(2, "g", "chat@llm"))
    assert book.for_chat(1).is_owner and not book.for_chat(2).is_owner
    assert png_bytes().startswith(b"\x89PNG")


async def test_waiting_helpers():
    flag = []
    assert await wait_for(lambda: bool(flag), timeout=0.05) is False
    flag.append(1)
    assert await wait_for(lambda: bool(flag)) is True
    await wait_until(lambda: bool(flag))
    with pytest.raises(TimeoutError):
        await wait_until(lambda: False, timeout=0.05)


async def test_shared_store_fixture(store):
    await store.set_state("k", "v")
    assert await store.get_state("k") == "v"
