from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest_asyncio

from sa_home_bot import wake_core
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import GuestSubscriptionConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_UNAVAILABLE,
    ERR_UNKNOWN_ACTION,
    ProtoError,
)
from sa_home_bot.tasks import protocol
from sa_home_bot.tasks import service as tasks_service
from sa_home_bot.tasks.service import TasksService

WINPC_WAKE = {"mac": "04:92:26:da:63:7c", "ip": "192.168.0.105", "broadcast": "192.168.0.255"}

OWN_STATE = {
    "node": "alfred",
    "wake": {"mac": "7c:83:34:b4:59:ac", "ip": "192.168.0.100", "broadcast": "192.168.0.255"},
    "peers": [{"id": "winpc", "alive": False, "wake": WINPC_WAKE}],
}

META = {"kind": protocol.TASK_KIND_LLM_CHAT, "chat_id": 42, "dialogue_id": 7}


class FakeNodeLink:
    display_name = "нода"

    def __init__(self, own, routes=None, *, wake_reveals=None):
        self._own = own
        self._routes = dict(routes or {})
        self._wake_reveals = wake_reveals or {}
        self.commands: list[str] = []

    async def get_state(self, dst=None):
        if dst is None:
            return self._own
        key = f"{dst.node}:{dst.service}"
        if key in self._routes:
            return self._routes[key]
        raise ServiceUnavailableError("нет связи")

    async def command(self, action, args=None, dst=None, timeout=None):
        self.commands.append(action)
        if action == "send_wol":
            self._routes.update(self._wake_reveals)
            return {"sent": True}
        if action == "warmup":
            return {"asleep": False}
        raise ProtoError(ERR_UNKNOWN_ACTION, f"нет действия {action}")


class FakeEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))

    def statuses(self) -> list[str]:
        return [d.get("status") for t, d in self.events if t == protocol.EVENT_TASK_PREWAKE]

    def results(self) -> list[dict]:
        return [d for t, d in self.events if t == protocol.EVENT_TASK_RESULT]


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


def _chat_row(task_id: int = 1, timeout_s: float = 60.0) -> dict:
    return {
        "id": task_id,
        "dst_node": "winpc",
        "dst_service": "llm",
        "action": protocol.ACTION_CHAT_LOOP,
        "args_json": json.dumps({"messages": [{"role": "user", "content": "блины"}]}),
        "meta_json": json.dumps(META),
        "timeout_s": timeout_s,
    }


def _service(store, link, emitter):
    return TasksService(Settings(), store, link, emit=emitter)


async def test_create_defaults_timeout_to_llm_budget(store):
    svc = _service(store, FakeNodeLink(OWN_STATE), FakeEmitter())
    due = (datetime.now(tz=UTC) + timedelta(minutes=30)).isoformat()
    await svc.run_command(
        protocol.ACTION_CREATE,
        {
            "due_at": due,
            "dst_node": "winpc",
            "dst_service": "llm",
            "action": protocol.ACTION_CHAT_LOOP,
        },
    )
    rows = await store.due_tasks(datetime.now(tz=UTC) + timedelta(hours=1))
    assert rows[0]["timeout_s"] == Settings().llm.request_timeout_s


async def test_fire_wakes_sleeping_node_and_delivers(store, monkeypatch):
    async def fake_chat_loop(*args, **kwargs):
        return "Блины ждут, сэг"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, wake_reveals={"winpc:llm": {"asleep": True}})
    emitter = FakeEmitter()

    await _service(store, link, emitter)._fire_one(_chat_row())

    assert "send_wol" in link.commands
    assert emitter.results() == [
        {
            "task_id": 1,
            "meta": META,
            "ok": True,
            "result": {"response": "Блины ждут, сэг"},
        }
    ]


async def test_fire_chat_loop_emits_tool_call_with_args_and_result(store, monkeypatch):
    async def fake_chat_loop(*args, **kwargs):
        await kwargs["on_tool_call"]("calc", {"expression": "1+1"}, "2")
        return "готово"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    emitter = FakeEmitter()

    await _service(store, link, emitter)._fire_one(_chat_row())

    tool_call_events = [d for t, d in emitter.events if t == protocol.EVENT_TOOL_CALL]
    assert tool_call_events == [{"name": "calc", "args": {"expression": "1+1"}, "result": "2"}]


async def test_fire_chat_loop_restores_woken_by_from_meta(store, monkeypatch):
    captured: dict = {}

    async def fake_chat_loop(*args, **kwargs):
        captured["tool_ctx"] = args[4]
        return "готово"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    svc = _service(store, link, FakeEmitter())

    row = _chat_row()
    row["meta_json"] = json.dumps(
        {
            "kind": protocol.TASK_KIND_LLM_CHAT,
            "chat_id": 42,
            "dialogue_id": 7,
            "awaited_node": "jeeves",
            "awaited_event": "update_finished",
        }
    )
    await svc._fire_one(row)

    assert captured["tool_ctx"].woken_by == ("jeeves", "update_finished")


async def test_fire_chat_loop_restores_message_thread_id_from_meta(store, monkeypatch):
    captured: dict = {}

    async def fake_chat_loop(*args, **kwargs):
        captured["tool_ctx"] = args[4]
        return "готово"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    svc = _service(store, link, FakeEmitter())

    row = _chat_row()
    row["meta_json"] = json.dumps(
        {
            "kind": protocol.TASK_KIND_LLM_CHAT,
            "chat_id": 42,
            "dialogue_id": 7,
            "message_thread_id": 42,
        }
    )
    await svc._fire_one(row)

    assert captured["tool_ctx"].message_thread_id == 42


async def test_fire_chat_loop_woken_by_none_without_awaited_meta(store, monkeypatch):
    captured: dict = {}

    async def fake_chat_loop(*args, **kwargs):
        captured["tool_ctx"] = args[4]
        return "готово"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    svc = _service(store, link, FakeEmitter())

    await svc._fire_one(_chat_row())

    assert captured["tool_ctx"].woken_by is None


async def test_fire_chat_loop_wires_book_and_emit_bridge(store, monkeypatch):
    captured: dict = {}

    async def fake_chat_loop(*args, **kwargs):
        captured["tool_ctx"] = args[4]
        return "готово"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    emitter = FakeEmitter()
    svc = _service(store, link, emitter)

    await svc._fire_one(_chat_row())

    tool_ctx = captured["tool_ctx"]
    assert tool_ctx.book is svc._book
    assert tool_ctx.notifier is None
    assert tool_ctx.store is None
    assert tool_ctx.emit is not None
    await tool_ctx.emit("some_event", {"x": 1})
    assert ("some_event", {"x": 1}) in emitter.events


def test_book_includes_guest_subscriptions_not_just_owner(store):
    settings = Settings(
        guest_subscriptions=[
            GuestSubscriptionConfig(name="Гость", chat_id=999, allowed_commands=["chat@llm"])
        ]
    )
    link = FakeNodeLink(OWN_STATE, routes={})
    svc = TasksService(settings, store, link, emit=FakeEmitter())
    assert [g.chat_id for g in svc._book.guests()] == [999]


async def test_fire_gives_up_after_grace(store, monkeypatch):
    monkeypatch.setattr(tasks_service, "FIRE_GRACE_S", 0.05)
    monkeypatch.setattr(tasks_service, "FIRE_RETRY_INTERVAL_S", 0.01)
    monkeypatch.setattr(wake_core, "WAKE_POLL_TIMEOUT_S", 0.0)
    link = FakeNodeLink(OWN_STATE)
    emitter = FakeEmitter()

    await _service(store, link, emitter)._fire_one(_chat_row())

    assert link.commands.count("send_wol") >= 2
    assert emitter.results()[0]["ok"] is False
    assert emitter.statuses() == ["waking"]


async def test_fire_waits_for_inflight_prewake(store, monkeypatch):
    order: list[str] = []

    async def slow_prewake(row):
        order.append("prewake-начался")
        await asyncio.sleep(0.05)
        order.append("prewake-кончился")

    async def fake_chat_loop(*args, **kwargs):
        order.append("chat")
        return "готово"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    svc = _service(store, link, FakeEmitter())
    monkeypatch.setattr(svc, "_prewake_one", slow_prewake)

    row = _chat_row()
    job = asyncio.create_task(svc._prewake_one_safe(row))
    svc._prewake_inflight[row["id"]] = job
    await asyncio.sleep(0)
    await svc._fire_one(row)
    await job

    assert order == ["prewake-начался", "prewake-кончился", "chat"]


async def test_prewake_failure_is_not_reported_as_final(store, monkeypatch):
    monkeypatch.setattr(wake_core, "WAKE_POLL_TIMEOUT_S", 0.0)
    emitter = FakeEmitter()
    svc = _service(store, FakeNodeLink(OWN_STATE), emitter)

    await svc._prewake_one(_chat_row())

    assert emitter.statuses() == ["waking"]
    assert "failed" not in emitter.statuses()
    assert emitter.results() == []


async def test_prewake_is_silent_when_target_already_warm(store):
    emitter = FakeEmitter()
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})

    await _service(store, link, emitter)._prewake_one(_chat_row())

    assert emitter.events == []
    assert link.commands == []


async def test_fire_now_fires_pending_task_before_due_at(store, monkeypatch):
    async def fake_chat_loop(*args, **kwargs):
        return "разбужен по событию"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    emitter = FakeEmitter()
    svc = _service(store, link, emitter)

    far_future = datetime.now(tz=UTC) + timedelta(hours=1)
    task_id = await store.create_task(
        "winpc",
        "llm",
        protocol.ACTION_CHAT_LOOP,
        {"messages": [{"role": "user", "content": "блины"}]},
        60.0,
        META,
        far_future,
        datetime.now(tz=UTC),
    )

    result = await svc.run_command(protocol.ACTION_FIRE_NOW, {"task_id": task_id})
    assert result == {"fired": True}
    for _ in range(10):
        await asyncio.sleep(0)

    assert emitter.results() == [
        {
            "task_id": task_id,
            "meta": META,
            "ok": True,
            "result": {"response": "разбужен по событию"},
        }
    ]
    assert await store.due_tasks(datetime.now(tz=UTC)) == []


async def test_fire_chat_loop_passes_reason_from_task_args(store, monkeypatch):
    captured: dict = {}

    async def fake_chat_loop(node_link, dst, timeout, messages, tool_ctx, reason, **kwargs):
        captured["reason"] = reason
        return "готово"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    svc = _service(store, link, FakeEmitter())

    row = _chat_row()
    row["args_json"] = json.dumps(
        {"messages": [{"role": "user", "content": "блины"}], "reason": "off"}
    )
    await svc._fire_one(row)

    assert captured["reason"] == "off"


async def test_fire_chat_loop_maps_legacy_think_to_reason(store, monkeypatch):
    captured: dict = {}

    async def fake_chat_loop(node_link, dst, timeout, messages, tool_ctx, reason, **kwargs):
        captured["reason"] = reason
        return "готово"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    svc = _service(store, link, FakeEmitter())

    row = _chat_row()
    row["args_json"] = json.dumps(
        {"messages": [{"role": "user", "content": "блины"}], "think": False}
    )
    await svc._fire_one(row)

    assert captured["reason"] == "off"


async def test_fire_chat_loop_retries_transient_unavailable_then_succeeds(store, monkeypatch):
    monkeypatch.setattr(tasks_service, "FIRE_RETRY_INTERVAL_S", 0.01)
    attempts: list[int] = []

    async def flaky_chat_loop(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise ServiceUnavailableError("сеть моргнула")
        return "получилось со второй попытки"

    monkeypatch.setattr(tasks_service, "run_chat_loop", flaky_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    emitter = FakeEmitter()

    await _service(store, link, emitter)._fire_one(_chat_row())

    assert len(attempts) == 2
    assert emitter.results() == [
        {
            "task_id": 1,
            "meta": META,
            "ok": True,
            "result": {"response": "получилось со второй попытки"},
        }
    ]


async def test_fire_chat_loop_gives_up_after_grace_on_persistent_unavailable(store, monkeypatch):
    monkeypatch.setattr(tasks_service, "FIRE_GRACE_S", 0.05)
    monkeypatch.setattr(tasks_service, "FIRE_RETRY_INTERVAL_S", 0.01)
    attempts: list[int] = []

    async def always_unavailable(*args, **kwargs):
        attempts.append(1)
        raise ServiceUnavailableError("сеть моргнула")

    monkeypatch.setattr(tasks_service, "run_chat_loop", always_unavailable)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    emitter = FakeEmitter()

    await _service(store, link, emitter)._fire_one(_chat_row())

    assert len(attempts) >= 2
    assert emitter.results()[0]["ok"] is False


async def test_fire_chat_loop_does_not_retry_deterministic_bad_request(store, monkeypatch):
    monkeypatch.setattr(tasks_service, "FIRE_GRACE_S", 5.0)
    monkeypatch.setattr(tasks_service, "FIRE_RETRY_INTERVAL_S", 0.01)
    attempts: list[int] = []

    async def bad_request(*args, **kwargs):
        attempts.append(1)
        raise ProtoError(ERR_BAD_REQUEST, "плохой запрос")

    monkeypatch.setattr(tasks_service, "run_chat_loop", bad_request)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    emitter = FakeEmitter()

    await _service(store, link, emitter)._fire_one(_chat_row())

    assert len(attempts) == 1
    assert emitter.results()[0]["ok"] is False


async def test_fire_chat_loop_retries_on_unavailable_proto_error_code(store, monkeypatch):
    monkeypatch.setattr(tasks_service, "FIRE_RETRY_INTERVAL_S", 0.01)
    attempts: list[int] = []

    async def flaky_chat_loop(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise ProtoError(ERR_UNAVAILABLE, "служба спит")
        return "ок"

    monkeypatch.setattr(tasks_service, "run_chat_loop", flaky_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    emitter = FakeEmitter()

    await _service(store, link, emitter)._fire_one(_chat_row())

    assert len(attempts) == 2
    assert emitter.results()[0]["ok"] is True


async def test_fire_chat_loop_defaults_reason_high_when_task_args_bare(store, monkeypatch):
    captured: dict = {}

    async def fake_chat_loop(node_link, dst, timeout, messages, tool_ctx, reason, **kwargs):
        captured["reason"] = reason
        return "готово"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    svc = _service(store, link, FakeEmitter())

    row = _chat_row()
    await svc._fire_one(row)

    assert captured["reason"] == "high"


async def test_fire_now_on_unknown_task_reports_not_fired(store):
    svc = _service(store, FakeNodeLink(OWN_STATE), FakeEmitter())
    result = await svc.run_command(protocol.ACTION_FIRE_NOW, {"task_id": 999})
    assert result == {"fired": False, "reason": "нет такой задачи или уже сработала"}


async def test_fire_now_is_idempotent_on_already_fired_task(store, monkeypatch):
    async def fake_chat_loop(*args, **kwargs):
        return "ответ"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    svc = _service(store, link, FakeEmitter())

    task_id = await store.create_task(
        "winpc",
        "llm",
        protocol.ACTION_CHAT_LOOP,
        {"messages": [{"role": "user", "content": "блины"}]},
        60.0,
        META,
        datetime.now(tz=UTC) + timedelta(hours=1),
        datetime.now(tz=UTC),
    )

    first = await svc.run_command(protocol.ACTION_FIRE_NOW, {"task_id": task_id})
    for _ in range(10):
        await asyncio.sleep(0)
    second = await svc.run_command(protocol.ACTION_FIRE_NOW, {"task_id": task_id})

    assert first == {"fired": True}
    assert second == {"fired": False, "reason": "нет такой задачи или уже сработала"}


async def test_fire_now_rejects_non_int_task_id(store):
    from sa_home_bot.proto.messages import ProtoError

    svc = _service(store, FakeNodeLink(OWN_STATE), FakeEmitter())
    try:
        await svc.run_command(protocol.ACTION_FIRE_NOW, {"task_id": "not-a-number"})
    except ProtoError as exc:
        assert "task_id" in exc.message
    else:
        raise AssertionError("ожидался ProtoError")


async def test_create_with_await_event_registers_waiter(store):
    svc = _service(store, FakeNodeLink(OWN_STATE), FakeEmitter())
    due = (datetime.now(tz=UTC) + timedelta(minutes=30)).isoformat()
    result = await svc.run_command(
        protocol.ACTION_CREATE,
        {
            "due_at": due,
            "dst_node": "winpc",
            "dst_service": "llm",
            "action": protocol.ACTION_CHAT_LOOP,
            "await_event": {"node": "arch-t480", "event_type": "restart_applied"},
        },
    )
    task_id = result["task_id"]
    assert await store.pop_event_waiter_for("arch-t480", "restart_applied") == task_id


async def test_create_without_await_event_registers_no_waiter(store):
    svc = _service(store, FakeNodeLink(OWN_STATE), FakeEmitter())
    due = (datetime.now(tz=UTC) + timedelta(minutes=30)).isoformat()
    await svc.run_command(
        protocol.ACTION_CREATE,
        {
            "due_at": due,
            "dst_node": "winpc",
            "dst_service": "llm",
            "action": protocol.ACTION_CHAT_LOOP,
        },
    )
    assert await store.pop_event_waiter_for("arch-t480", "restart_applied") is None


async def test_create_rejects_malformed_await_event(store):
    svc = _service(store, FakeNodeLink(OWN_STATE), FakeEmitter())
    due = (datetime.now(tz=UTC) + timedelta(minutes=30)).isoformat()
    try:
        await svc.run_command(
            protocol.ACTION_CREATE,
            {
                "due_at": due,
                "dst_node": "winpc",
                "dst_service": "llm",
                "action": protocol.ACTION_CHAT_LOOP,
                "await_event": {"node": "arch-t480"},
            },
        )
    except ProtoError as exc:
        assert "await_event" in exc.message
    else:
        raise AssertionError("ожидался ProtoError")


async def test_match_event_fires_matching_waiter(store, monkeypatch):
    async def fake_chat_loop(*args, **kwargs):
        return "продолжаю по плану"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    emitter = FakeEmitter()
    svc = _service(store, link, emitter)

    due = (datetime.now(tz=UTC) + timedelta(minutes=30)).isoformat()
    created = await svc.run_command(
        protocol.ACTION_CREATE,
        {
            "due_at": due,
            "dst_node": "winpc",
            "dst_service": "llm",
            "action": protocol.ACTION_CHAT_LOOP,
            "args": {"messages": [{"role": "user", "content": "продолжай"}]},
            "meta": META,
            "await_event": {"node": "arch-t480", "event_type": "update_finished"},
        },
    )

    result = await svc.run_command(
        protocol.ACTION_MATCH_EVENT, {"node": "arch-t480", "event_type": "update_finished"}
    )
    assert result == {"matched": True, "task_id": created["task_id"]}
    for _ in range(10):
        await asyncio.sleep(0)
    assert emitter.results() == [
        {
            "task_id": created["task_id"],
            "meta": META,
            "ok": True,
            "result": {"response": "продолжаю по плану"},
        }
    ]
    second = await svc.run_command(
        protocol.ACTION_MATCH_EVENT, {"node": "arch-t480", "event_type": "update_finished"}
    )
    assert second == {"matched": False}


async def test_match_event_no_waiter_is_a_noop(store):
    svc = _service(store, FakeNodeLink(OWN_STATE), FakeEmitter())
    result = await svc.run_command(
        protocol.ACTION_MATCH_EVENT, {"node": "arch-t480", "event_type": "update_finished"}
    )
    assert result == {"matched": False}


async def test_match_event_wrong_node_does_not_fire(store):
    svc = _service(store, FakeNodeLink(OWN_STATE), FakeEmitter())
    due = (datetime.now(tz=UTC) + timedelta(minutes=30)).isoformat()
    await svc.run_command(
        protocol.ACTION_CREATE,
        {
            "due_at": due,
            "dst_node": "winpc",
            "dst_service": "llm",
            "action": protocol.ACTION_CHAT_LOOP,
            "await_event": {"node": "arch-t480", "event_type": "update_finished"},
        },
    )
    result = await svc.run_command(
        protocol.ACTION_MATCH_EVENT, {"node": "mycraft", "event_type": "update_finished"}
    )
    assert result == {"matched": False}
    assert await store.pop_event_waiter_for("arch-t480", "update_finished") is not None


async def test_match_event_rejects_missing_args(store):
    svc = _service(store, FakeNodeLink(OWN_STATE), FakeEmitter())
    try:
        await svc.run_command(protocol.ACTION_MATCH_EVENT, {"node": "arch-t480"})
    except ProtoError as exc:
        assert "node" in exc.message or "event_type" in exc.message
    else:
        raise AssertionError("ожидался ProtoError")


async def test_fire_due_cleans_up_leftover_waiter_on_timeout(store, monkeypatch):
    async def fake_chat_loop(*args, **kwargs):
        return "не подтвердилось, но всё равно продолжаю"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    svc = _service(store, link, FakeEmitter())

    now = datetime.now(tz=UTC)
    task_id = await store.create_task(
        "winpc",
        "llm",
        protocol.ACTION_CHAT_LOOP,
        {"messages": [{"role": "user", "content": "x"}]},
        60.0,
        META,
        now - timedelta(seconds=1),
        now,
    )
    await store.add_event_waiter(task_id, "arch-t480", "update_finished", now)

    await svc._fire_due()
    for _ in range(10):
        await asyncio.sleep(0)

    assert await store.pop_event_waiter_for("arch-t480", "update_finished") is None


async def test_timer_fires_result_without_touching_dst(store):
    link = FakeNodeLink(OWN_STATE)
    emitter = FakeEmitter()
    svc = _service(store, link, emitter)
    meta = {"kind": protocol.TASK_KIND_PENDING_ACTION_EXPIRE, "pending_action_id": 5}
    now = datetime.now(tz=UTC)
    task_id = await store.create_task(
        protocol.NODE_ID, protocol.SERVICE_NAME, protocol.ACTION_TIMER, {}, 60.0, meta, now, now
    )

    await svc._prewake_due()
    await svc._fire_due()
    for _ in range(10):
        await asyncio.sleep(0)

    assert emitter.results() == [{"task_id": task_id, "meta": meta, "ok": True, "result": {}}]
    assert emitter.statuses() == []
    assert link.commands == []
