"""Служба graph_memory: очередь эпизодов, границы chat_id, выбор модели
экстракции — без реального Graphiti/Neo4j (юниты для той части, что не
требует живого mycraft; сквозная проверка с реальным Graphiti — вручную,
см. историю Этапа 41)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from sa_home_bot.config import GraphMemoryConfig, LlmConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.graph_memory.service import GraphMemoryService
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
    """Решение пользователя 2026-09-22: пустой extraction_model берёт ту же
    модель, что уже держит в VRAM живой чат — не отдельную (см. докстринг
    GraphMemoryConfig в config.py и service.py::_extraction_model)."""
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
    """Живучесть очереди (Верификация, п.6 плана Этапа 41): эпизод, застрявший
    в 'processing' после аварийного завершения службы, должен вернуться в
    'pending' при следующем старте _ingest_loop, а не потеряться навсегда."""
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
    """Этап 54: карточки людей пишутся прямым Cypher — эмулируем MERGE по
    ключу (subject_id, field, value, by_id) в списке."""

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
    # nodectl call приводит «ids=7136623771» к int (живой баг 2026-10-08).
    assert GraphMemoryService._person_ids({"ids": 7136623771}) == [7136623771]
