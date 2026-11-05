from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sa_home_bot.bot.ai_context import ALFRED_TIMEZONE
from sa_home_bot.bot.group_context import DaySummaryUpdater, build_note
from sa_home_bot.db.store import ALFRED_AWAY_KEY
from sa_home_bot.utils.clock import utc_now


class FakeNodeLink:
    def __init__(self, response="новая сводка"):
        self.calls = []
        self.response = response

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append(args)
        return {"response": self.response}


SETTINGS = SimpleNamespace(llm=SimpleNamespace(request_timeout_s=5.0))
IDLE = SimpleNamespace(snapshot=lambda: {})
BUSY = SimpleNamespace(snapshot=lambda: {-100: object()})


async def _say(store, message_id, content="реплика", when=None, chat_id=-100):
    await store.record_group_message(
        chat_id, message_id, created_at=when or utc_now(),
        user_id=1, user_name="Анна", content=content,
        reply_to=None, from_alfred=False, keep=1000
    )


async def _today(chat_id=-100):
    return utc_now().astimezone(ZoneInfo(ALFRED_TIMEZONE)).date().isoformat()


class TestBuildNote:
    async def test_none_when_no_messages_no_summary(self, store):
        result = await build_note(store, -100, trigger_message_id=None, dialogue_id=None)
        assert result is None

    async def test_today_message_included_old_excluded(self, store):
        await _say(store, 1, "сегодня")
        await _say(store, 2, "вчера", when=utc_now() - timedelta(days=3))
        note = await build_note(store, -100, trigger_message_id=None, dialogue_id=None)
        assert note is not None
        assert "сегодня" in note
        assert "вчера" not in note

    async def test_summary_used_when_same_day(self, store):
        today = await _today()
        await store.save_group_day_summary(-100, today, 5, "прежняя сводка", utc_now())
        await _say(store, 6, "новая")
        note = await build_note(store, -100, trigger_message_id=None, dialogue_id=None)
        assert "прежняя сводка" in note
        assert "новая" in note

    async def test_summary_not_used_when_different_day(self, store):
        await store.save_group_day_summary(-100, "2000-01-01", 5, "старая сводка", utc_now())
        await _say(store, 1, "новая")
        note = await build_note(store, -100, trigger_message_id=None, dialogue_id=None)
        assert "старая сводка" not in note
        assert "новая" in note

    async def test_messages_before_upto_excluded(self, store):
        today = await _today()
        await store.save_group_day_summary(-100, today, 5, "прежняя", utc_now())
        await _say(store, 3, "до upto")
        await _say(store, 6, "после upto")
        note = await build_note(store, -100, trigger_message_id=None, dialogue_id=None)
        assert "до upto" not in note
        assert "после upto" in note

    async def test_trigger_message_excluded(self, store):
        await _say(store, 1, "одна")
        await _say(store, 2, "две")
        note = await build_note(store, -100, trigger_message_id=2, dialogue_id=None)
        assert "одна" in note
        assert "две" not in note

    async def test_chain_messages_excluded(self, store, monkeypatch):
        await _say(store, 1, "вне цепочки")
        await _say(store, 2, "в цепочке")
        async def fake_turns(chat_id, dialogue_id):
            return [{"message_id": 2}]
        monkeypatch.setattr(store, "ai_turns_for_dialogue", fake_turns)
        note = await build_note(store, -100, trigger_message_id=None, dialogue_id=7)
        assert "вне цепочки" in note
        assert "в цепочке" not in note

    async def test_tail_limit_and_missing_text(self, store):
        for i in range(20):
            await _say(store, i + 1, f"msg{i}")
        note = await build_note(store, -100, trigger_message_id=None, dialogue_id=None)
        assert "Ещё 5 реплик" in note

    async def test_max_chars_respected(self, store):
        for i in range(15):
            await _say(store, i + 1, "x" * 200)
        today = await _today()
        await store.save_group_day_summary(-100, today, 0, "y" * 1200, utc_now())
        note = await build_note(store, -100, trigger_message_id=None, dialogue_id=None)
        assert len(note) <= 3000
        assert "Ещё" in note

    async def test_hint_false_no_chat_recap(self, store):
        await _say(store, 1, "текст")
        note = await build_note(store, -100, trigger_message_id=None, dialogue_id=None, hint=False)
        assert "chat_recap" not in note

    async def test_hint_true_has_chat_recap(self, store):
        await _say(store, 1, "текст")
        note = await build_note(store, -100, trigger_message_id=None, dialogue_id=None, hint=True)
        assert "chat_recap" in note


class TestDaySummaryUpdater:
    async def test_no_call_when_29_short_messages(self, store):
        for i in range(29):
            await _say(store, i + 1, "x")
        updater = DaySummaryUpdater(settle_s=0)
        link = FakeNodeLink()
        updater.schedule(link, store, SETTINGS, -100, active_chats=IDLE)
        await updater.wait(-100)
        assert not link.calls

    async def test_call_when_30_messages(self, store):
        for i in range(30):
            await _say(store, i + 1, "x")
        updater = DaySummaryUpdater(settle_s=0)
        link = FakeNodeLink()
        updater.schedule(link, store, SETTINGS, -100, active_chats=IDLE)
        await updater.wait(-100)
        assert len(link.calls) == 1
        row = await store.group_day_summary(-100)
        assert row["upto_message_id"] == 30
        assert row["summary"] == "новая сводка"
        assert row["day"] == await _today()

    async def test_call_when_one_long_message(self, store):
        await _say(store, 1, "x" * 3000)
        updater = DaySummaryUpdater(settle_s=0)
        link = FakeNodeLink()
        updater.schedule(link, store, SETTINGS, -100, active_chats=IDLE)
        await updater.wait(-100)
        assert len(link.calls) == 1

    async def test_no_call_when_busy(self, store):
        for i in range(30):
            await _say(store, i + 1, "x")
        updater = DaySummaryUpdater(settle_s=0)
        link = FakeNodeLink()
        updater.schedule(link, store, SETTINGS, -100, active_chats=BUSY)
        await updater.wait(-100)
        assert not link.calls

    async def test_no_call_when_away(self, store):
        for i in range(30):
            await _say(store, i + 1, "x")
        await store.set_state(ALFRED_AWAY_KEY, "1")
        updater = DaySummaryUpdater(settle_s=0)
        link = FakeNodeLink()
        updater.schedule(link, store, SETTINGS, -100, active_chats=IDLE)
        await updater.wait(-100)
        assert not link.calls

    async def test_no_save_when_empty_response(self, store):
        for i in range(30):
            await _say(store, i + 1, "x")
        updater = DaySummaryUpdater(settle_s=0)
        link = FakeNodeLink(response="")
        updater.schedule(link, store, SETTINGS, -100, active_chats=IDLE)
        await updater.wait(-100)
        row = await store.group_day_summary(-100)
        assert row is None

    async def test_chunk_upto_message_id(self, store):
        for i in range(10):
            await _say(store, i + 1, "x" * 2000)
        updater = DaySummaryUpdater(settle_s=0)
        link = FakeNodeLink()
        updater.schedule(link, store, SETTINGS, -100, active_chats=IDLE)
        await updater.wait(-100)
        row = await store.group_day_summary(-100)
        assert row["upto_message_id"] == 6

    async def test_schedule_returns_false_when_running(self, store):
        for i in range(30):
            await _say(store, i + 1, "x")
        updater = DaySummaryUpdater(settle_s=0.05)
        link = FakeNodeLink()
        first = updater.schedule(link, store, SETTINGS, -100, active_chats=IDLE)
        second = updater.schedule(link, store, SETTINGS, -100, active_chats=IDLE)
        await updater.wait(-100)
        assert first is True
        assert second is False

    async def test_previous_summary_in_request(self, store):
        today = await _today()
        await store.save_group_day_summary(-100, today, 5, "прежняя сводка", utc_now())
        for i in range(6, 36):
            await _say(store, i, "x")
        updater = DaySummaryUpdater(settle_s=0)
        link = FakeNodeLink()
        updater.schedule(link, store, SETTINGS, -100, active_chats=IDLE)
        await updater.wait(-100)
        args = link.calls[0]
        content = args["messages"][0]["content"]
        assert "Прежняя сводка:" in content
        assert "прежняя сводка" in content

    async def test_store_error_swallowed(self, store, monkeypatch):
        for i in range(30):
            await _say(store, i + 1, "x")
        async def fail_save(*args, **kwargs):
            raise RuntimeError("db error")
        monkeypatch.setattr(store, "save_group_day_summary", fail_save)
        updater = DaySummaryUpdater(settle_s=0)
        link = FakeNodeLink()
        updater.schedule(link, store, SETTINGS, -100, active_chats=IDLE)
        await updater.wait(-100)
