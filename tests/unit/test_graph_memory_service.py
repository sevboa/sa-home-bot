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
