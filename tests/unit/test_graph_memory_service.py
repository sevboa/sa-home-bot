from __future__ import annotations

import sys
import types

import pytest
import pytest_asyncio
from pydantic import BaseModel, ValidationError

from sa_home_bot.config import GraphMemoryConfig, LlmConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.graph_memory import service as service_module
from sa_home_bot.graph_memory.service import (
    COMPANION_ASLEEP_CODE,
    NO_THINKING_BODY,
    OLLAMA_API_KEY,
    OLLAMA_OPENAI_URL,
    GraphMemoryService,
    extraction_endpoint,
)
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError

CHAT_A = 1
CHAT_B = 2


@pytest_asyncio.fixture
async def svc(tmp_path):
    db = Database(tmp_path / "graph_memory.sqlite")
    await db.open()
    await apply_migrations(db)
    settings = Settings(
        graph_memory=GraphMemoryConfig(db_path=tmp_path / "graph_memory.sqlite"),
        llm=LlmConfig(model="hf.co/unsloth/gemma-4-26B-A4B-it-GGUF:UD-IQ4_XS"),
    )
    yield GraphMemoryService(settings, db)
    await db.close()


def test_extraction_model_defaults_to_the_persona_chat_model(svc):
    assert svc._extraction_model() == "hf.co/unsloth/gemma-4-26B-A4B-it-GGUF:UD-IQ4_XS"


async def test_extraction_model_override_wins_over_persona_model(tmp_path):
    db = Database(tmp_path / "graph_memory.sqlite")
    await db.open()
    await apply_migrations(db)
    settings = Settings(
        graph_memory=GraphMemoryConfig(
            db_path=tmp_path / "graph_memory.sqlite", extraction_model="qwen3:8b"
        ),
        llm=LlmConfig(model="hf.co/unsloth/gemma-4-26B-A4B-it-GGUF:UD-IQ4_XS"),
    )
    svc = GraphMemoryService(settings, db)
    assert svc._extraction_model() == "qwen3:8b"
    await db.close()


def test_extraction_endpoint_stays_on_ollama_for_ollama_backend():
    assert extraction_endpoint(LlmConfig()) == (OLLAMA_OPENAI_URL, OLLAMA_API_KEY, {})


def test_extraction_endpoint_follows_openai_backend_with_key_and_no_thinking(tmp_path):
    key_file = tmp_path / "key.txt"
    key_file.write_text("k-123\n", encoding="utf-8")
    llm = LlmConfig(
        backend="openai",
        openai_base_url="http://127.0.0.1:8090/v1",
        openai_api_key_file=key_file,
    )
    assert extraction_endpoint(llm) == ("http://127.0.0.1:8090/v1", "k-123", NO_THINKING_BODY)


def test_extraction_endpoint_openai_without_key_file_uses_placeholder_key():
    llm = LlmConfig(backend="openai", openai_base_url="http://127.0.0.1:8090/v1")
    assert extraction_endpoint(llm)[1] == OLLAMA_API_KEY


async def test_add_episode_queues_and_reports_in_queue_status(svc):
    result = await svc.run_command(
        "add_episode", {"text": "Наташа — жена Алексея", "chat_id": CHAT_A}
    )
    assert result["queued"] is True
    state = await svc.get_state()
    assert state["queue_pending"] == 1
    assert state["queue_done"] == 0


async def test_add_episode_requires_chat_id(svc):
    with pytest.raises(ProtoError) as exc_info:
        await svc.run_command("add_episode", {"text": "факт"})
    assert exc_info.value.code == ERR_BAD_REQUEST


async def test_add_episode_rejects_empty_text(svc):
    with pytest.raises(ProtoError) as exc_info:
        await svc.run_command("add_episode", {"text": "   ", "chat_id": CHAT_A})
    assert exc_info.value.code == ERR_BAD_REQUEST


async def test_add_episode_rejects_text_over_the_limit(svc):
    with pytest.raises(ProtoError) as exc_info:
        await svc.run_command("add_episode", {"text": "a" * 401, "chat_id": CHAT_A})
    assert exc_info.value.code == ERR_BAD_REQUEST


async def test_search_requires_chat_id(svc):
    with pytest.raises(ProtoError) as exc_info:
        await svc.run_command("search", {"query": "кто?"})
    assert exc_info.value.code == ERR_BAD_REQUEST


async def test_reset_stuck_processing_on_restart_recovers_orphaned_episode(svc):
    await svc.run_command("add_episode", {"text": "факт", "chat_id": CHAT_A})
    await svc._db.conn.execute("UPDATE graph_episodes SET status = 'processing' WHERE id = 1")
    await svc._db.conn.commit()

    await svc._reset_stuck_processing()

    row = await svc._next_pending()
    assert row is not None
    assert row["id"] == 1


class _FakeResult:
    def __init__(self, records):
        self.records = records


class _FakeDriver:
    def __init__(self):
        self.claims: list[dict] = []

    async def execute_query(self, cypher, params):
        assert params["group_id"] == "people"
        if cypher.startswith("MERGE"):
            key = ("subject_id", "field", "value", "by_id")
            self.claims = [c for c in self.claims if any(c[k] != params[k] for k in key)]
            self.claims.append({k: v for k, v in params.items() if k != "group_id"})
            return _FakeResult([])
        return _FakeResult([c for c in self.claims if c["subject_id"] in params["ids"]])


class _FakeGraphiti:
    def __init__(self):
        self.driver = _FakeDriver()


@pytest_asyncio.fixture
async def people_svc(svc):
    svc._graphiti = _FakeGraphiti()
    return svc


async def test_person_claim_derives_strength_from_ids(people_svc):
    own = await people_svc.run_command(
        "person_claim", {"subject_id": 101, "field": "gender", "value": "мужчина", "by_id": 101}
    )
    other = await people_svc.run_command(
        "person_claim", {"subject_id": 101, "field": "gender", "value": "f", "by_id": 202}
    )
    assert (own["value"], own["strength"]) == ("m", "self")
    assert other["strength"] == "acquaintance"
    cards = (await people_svc.run_command("person_cards", {"ids": "101, 999"}))["cards"]
    assert cards["101"]["gender"]["value"] == "m"
    assert "999" not in cards


async def test_person_claim_repeat_does_not_duplicate(people_svc):
    args = {"subject_id": 101, "field": "alias", "value": "Лиля", "by_id": 202}
    await people_svc.run_command("person_claim", args)
    await people_svc.run_command("person_claim", args)
    assert len(people_svc._graphiti.driver.claims) == 1


async def test_person_claim_rejects_unknown_field(people_svc):
    with pytest.raises(ProtoError) as exc_info:
        await people_svc.run_command(
            "person_claim", {"subject_id": 1, "field": "age", "value": "3", "by_id": 1}
        )
    assert exc_info.value.code == ERR_BAD_REQUEST


async def test_person_cards_empty_ids_skip_neo4j(svc):
    assert await svc.run_command("person_cards", {"ids": ""}) == {"cards": {}}


def test_person_ids_accept_single_int():
    assert GraphMemoryService._person_ids({"ids": 7136623771}) == [7136623771]


SWITCH_URL = "http://127.0.0.1:8090/v1/switch/status"


async def test_companion_awake_empty_url_true(svc):
    assert svc._cfg.wake_gate_url == ""
    assert await svc._companion_awake() is True


async def test_companion_awake_sleeping_companion_false(svc, monkeypatch):
    svc._cfg.wake_gate_url = SWITCH_URL
    calls = []

    async def fake_fetch(url):
        calls.append(url)
        return {"companion": {"running": False}}

    monkeypatch.setattr(service_module, "fetch_switch_status", fake_fetch)
    assert await svc._companion_awake() is False
    assert calls == [SWITCH_URL]


async def test_companion_awake_switch_unreachable_false(svc, monkeypatch):
    svc._cfg.wake_gate_url = SWITCH_URL

    async def fake_fetch(url):
        return None

    monkeypatch.setattr(service_module, "fetch_switch_status", fake_fetch)
    assert await svc._companion_awake() is False


async def test_companion_awake_running_companion_true(svc, monkeypatch):
    svc._cfg.wake_gate_url = SWITCH_URL
    async def fake_fetch(url):
        return {"companion": {"running": True}}

    monkeypatch.setattr(service_module, "fetch_switch_status", fake_fetch)
    assert await svc._companion_awake() is True


async def test_ingest_loop_waits_for_awake_companion(svc, monkeypatch):
    svc._cfg.wake_gate_url = SWITCH_URL
    async def fake_fetch(url):
        return {"companion": {"running": False}}

    monkeypatch.setattr(service_module, "fetch_switch_status", fake_fetch)
    await svc.run_command("add_episode", {"text": "факт", "chat_id": CHAT_A})

    def boom(_seconds):
        raise RuntimeError("stop-the-loop")

    monkeypatch.setattr(service_module.asyncio, "sleep", boom)
    with pytest.raises(RuntimeError):
        await svc._ingest_loop()
    row = await svc._next_pending()
    assert row is not None
    assert row["attempts"] == 0


class _Edges(BaseModel):
    edges: list[str]


def _asleep_error(body):
    exc = RuntimeError("503")
    exc.status_code = 503
    exc.body = body
    return exc


def _failing_graphiti(exc):
    class _Graphiti:
        async def add_episode(self, **kwargs):
            raise exc

    return _Graphiti()


async def _episode_after_failure(svc, monkeypatch, exc):
    sleeps = []
    nodes = types.SimpleNamespace(EpisodeType=types.SimpleNamespace(text="text"))
    monkeypatch.setitem(sys.modules, "graphiti_core", types.ModuleType("graphiti_core"))
    monkeypatch.setitem(sys.modules, "graphiti_core.nodes", nodes)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(service_module.asyncio, "sleep", fake_sleep)
    svc._graphiti = _failing_graphiti(exc)
    await svc.run_command("add_episode", {"text": "факт", "chat_id": CHAT_A})
    row = await svc._next_pending()
    await svc._process_one(row)
    cur = await svc._db.conn.execute("SELECT status, attempts, error FROM graph_episodes")
    return await cur.fetchone(), sleeps


async def test_process_one_keeps_pending_without_attempt_when_companion_asleep(svc, monkeypatch):
    row, sleeps = await _episode_after_failure(
        svc, monkeypatch, _asleep_error(COMPANION_ASLEEP_CODE)
    )
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["error"] is None
    assert sleeps == [service_module.IDLE_POLL_S]


async def test_process_one_counts_other_503_as_ordinary_failure(svc, monkeypatch):
    row, _ = await _episode_after_failure(svc, monkeypatch, _asleep_error("gpu_silence"))
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert "503" in row["error"]


async def test_process_one_fails_at_once_on_validation_error(svc, monkeypatch):
    with pytest.raises(ValidationError) as caught:
        _Edges.model_validate({})
    row, sleeps = await _episode_after_failure(svc, monkeypatch, caught.value)
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert "edges" in row["error"]
    assert sleeps == []
