"""Служба tasks: прогрев заранее и срабатывание отложенной задачи.

Живая находка 2026-07-27 (инцидент 19:34-19:39, напоминание не доставлено):
тестов на эту службу не было вовсе, и три дефекта дожили до прода — WoL из
неё не уходил никогда, на сроке она сдавалась за 6 секунд без единой попытки
разбудить цель, а прогрев и срабатывание одной задачи гонялись наперегонки.
Здесь зафиксировано поведение после разбора.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest_asyncio

from sa_home_bot import wake_core
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import Settings
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

# Своя нода видит спящую winpc: связи нет, но реквизиты WoL известны с её
# последнего hello (node/peers.py::PeerLink.wake_info).
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
    # Прежний дефолт 60с не покрывал даже одного прохода chat_loop.
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
    # Главный сценарий инцидента: на сроке цель спит. Раньше это давало
    # мгновенное «пропущено», теперь служба сама будит её и доставляет.
    async def fake_chat_loop(*args, **kwargs):
        return "Блины ждут, сэг"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, wake_reveals={"winpc:llm": {"asleep": True}})
    emitter = FakeEmitter()

    await _service(store, link, emitter)._fire_one(_chat_row())

    assert "send_wol" in link.commands  # magic packet реально ушёл
    assert emitter.results() == [
        {
            "task_id": 1,
            "meta": META,
            "ok": True,
            "result": {"response": "Блины ждут, сэг"},
        }
    ]


async def test_fire_gives_up_after_grace(store, monkeypatch):
    # Цель не поднялась за отведённое время — честное «пропущено», но лишь
    # после повторных попыток, а не с первой presence-проверки.
    monkeypatch.setattr(tasks_service, "FIRE_GRACE_S", 0.05)
    monkeypatch.setattr(tasks_service, "FIRE_RETRY_INTERVAL_S", 0.01)
    monkeypatch.setattr(wake_core, "WAKE_POLL_TIMEOUT_S", 0.0)
    link = FakeNodeLink(OWN_STATE)  # WoL уходит, но служба так и не появляется
    emitter = FakeEmitter()

    await _service(store, link, emitter)._fire_one(_chat_row())

    assert link.commands.count("send_wol") >= 2  # именно повторяли, а не сдались сразу
    assert emitter.results()[0]["ok"] is False
    assert emitter.statuses() == ["waking"]  # «шаги» показаны ровно один раз


async def test_fire_waits_for_inflight_prewake(store, monkeypatch):
    # Задача со сроком ближе PREWAKE_LEAD_S получает прогрев и срабатывание
    # почти одновременно: fire обязан дождаться прогрева, а не читать
    # presence посреди него.
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
    await asyncio.sleep(0)  # дать прогреву стартовать
    await svc._fire_one(row)
    await job

    assert order == ["prewake-начался", "prewake-кончился", "chat"]


async def test_prewake_failure_is_not_reported_as_final(store, monkeypatch):
    # Прогрев — лишь оптимизация: его неудача не должна выглядеть для
    # пользователя как проигранная задача, впереди ещё попытки на сроке.
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

    assert emitter.events == []  # ни «шагов», ни WoL — показывать нечего
    assert link.commands == []


# --- fire_now: разбудить задачу по событию, а не по due_at (remind after_event) ---


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
        await asyncio.sleep(0)  # дать фоновой задаче (asyncio.create_task) выполниться

    assert emitter.results() == [
        {
            "task_id": task_id,
            "meta": META,
            "ok": True,
            "result": {"response": "разбужен по событию"},
        }
    ]
    # due_at не наступил — обычный опрос очереди не должен найти её снова.
    assert await store.due_tasks(datetime.now(tz=UTC)) == []


async def test_fire_chat_loop_passes_think_none_through_json_roundtrip(store, monkeypatch):
    # Живой баг 2026-08-05: bool(task_args.get("think", True)) давил
    # think=None (mode="single_call", модель без thinking) в явный False —
    # Ollama падала 400 "does not support thinking" на любом присутствии
    # ключа, не только на true. think должен пройти JSON-круг как None.
    captured: dict = {}

    async def fake_chat_loop(*args, think=None, **kwargs):
        captured["think"] = think
        return "готово"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    svc = _service(store, link, FakeEmitter())

    row = _chat_row()
    row["args_json"] = json.dumps(
        {"messages": [{"role": "user", "content": "блины"}], "think": None}
    )
    await svc._fire_one(row)

    assert captured["think"] is None


async def test_fire_chat_loop_retries_transient_unavailable_then_succeeds(store, monkeypatch):
    # Решение пользователя 2026-08-05: сбой "модель недоступна прямо сейчас"
    # не должен сразу сдаваться "не нашли Альфреда" — есть время до
    # deadline, стоит повторить.
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

    assert len(attempts) >= 2  # именно повторяли, а не сдались с первого раза
    assert emitter.results()[0]["ok"] is False


async def test_fire_chat_loop_does_not_retry_deterministic_bad_request(store, monkeypatch):
    # Ошибка в самом запросе (например, наш собственный баг с think) —
    # повтор её не лечит, только жжёт бюджет: должен упасть с первой попытки.
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
    # ERR_UNAVAILABLE от службы llm классифицируется как переходный сбой
    # ровно как и ServiceUnavailableError (bot/ai_flow.py::_is_unavailable —
    # та же классификация, дублирована в tasks/service.py).
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


async def test_fire_chat_loop_coerces_truthy_think_to_bool(store, monkeypatch):
    captured: dict = {}

    async def fake_chat_loop(*args, think=None, **kwargs):
        captured["think"] = think
        return "готово"

    monkeypatch.setattr(tasks_service, "run_chat_loop", fake_chat_loop)
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    svc = _service(store, link, FakeEmitter())

    row = _chat_row()  # args_json без "think" — дефолт True
    await svc._fire_one(row)

    assert captured["think"] is True


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
