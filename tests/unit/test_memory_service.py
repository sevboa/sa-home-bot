"""Служба memory: запись/поиск/забывание фактов, границы видимости между
чатами, общее знание дома и пометка личного."""

from __future__ import annotations

import pytest
import pytest_asyncio

from sa_home_bot.config import MemoryConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.memory import service as memory_service
from sa_home_bot.memory.service import MemoryService
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError

FAMILY = 1
PRIVATE = 2


@pytest_asyncio.fixture
async def svc(tmp_path):
    db = Database(tmp_path / "memory.sqlite")
    await db.open()
    await apply_migrations(db)
    yield MemoryService(Settings(memory=MemoryConfig(db_path=tmp_path / "memory.sqlite")), db)
    await db.close()


async def test_remember_then_recall_by_meaningful_words(svc):
    await svc.run_command(
        "remember", {"text": "Качаем сериалы в /mnt/data/pr, озвучка LostFilm", "chat_id": FAMILY}
    )
    result = await svc.run_command("recall", {"query": "а какая у нас озвучка?", "chat_id": FAMILY})
    assert result["count"] == 1
    assert "LostFilm" in result["facts"][0]["text"]
    assert result["facts"][0]["scope"] == "chat"


async def test_recall_survives_a_different_word_ending(svc):
    """Живая находка: записали «в озвучкЕ», спросили «какая озвучкА» — и
    триграммный индекс не нашёл ничего, потому что ищет подстроку целиком.
    Окончание у слов запроса отрезается — это не морфология, а грубое
    приближение, но именно оно и нужно."""
    await svc.run_command("remember", {"text": "Смотрим в озвучке LostFilm", "chat_id": FAMILY})
    for query in ("озвучк", "а какая у нас озвучка?", "в какой озвучкой смотрим"):
        found = await svc.run_command("recall", {"query": query, "chat_id": FAMILY})
        assert found["count"] == 1, query


async def test_memory_of_one_chat_is_invisible_to_another(svc):
    await svc.run_command("remember", {"text": "Секрет из личного чата", "chat_id": PRIVATE})
    assert (await svc.run_command("recall", {"query": "секрет", "chat_id": FAMILY}))["facts"] == []
    assert (await svc.run_command("recall", {"query": "секрет", "chat_id": PRIVATE}))["count"] == 1


async def test_common_knowledge_is_visible_from_every_chat(svc):
    """Справочник (кто такой Альфред, как выглядит) — не чья-то тайна: его
    заводят один раз и видно отовсюду."""
    await svc.run_command(
        "remember",
        {"text": "Альфред носит фрак и седые бакенбарды", "chat_id": FAMILY, "scope": "common"},
    )
    for chat_id in (FAMILY, PRIVATE):
        found = await svc.run_command("recall", {"query": "как выглядит фрак", "chat_id": chat_id})
        assert found["count"] == 1
        assert found["facts"][0]["scope"] == "common"


async def test_sensitive_fact_can_never_be_common(svc):
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(
            "remember",
            {"text": "Код от домофона 1234", "chat_id": FAMILY, "scope": "common",
             "sensitive": True},
        )
    assert excinfo.value.code == ERR_BAD_REQUEST
    assert "не может быть общим" in excinfo.value.message


async def test_sensitive_fact_is_marked_in_recall(svc):
    await svc.run_command(
        "remember", {"text": "Код от домофона 1234", "chat_id": FAMILY, "sensitive": True}
    )
    fact = (await svc.run_command("recall", {"query": "домофон", "chat_id": FAMILY}))["facts"][0]
    assert fact["sensitive"] is True
    assert fact["scope"] == "chat"


async def test_unknown_scope_is_refused(svc):
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command("remember", {"text": "x", "chat_id": FAMILY, "scope": "весь мир"})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_forget_only_touches_own_chat(svc):
    added = await svc.run_command("remember", {"text": "Забывчивый факт", "chat_id": PRIVATE})
    # Чужой чат знает номер, но стереть не может.
    alien = await svc.run_command("forget", {"id": added["id"], "chat_id": FAMILY})
    assert alien["forgotten"] is False
    own = await svc.run_command("forget", {"id": added["id"], "chat_id": PRIVATE})
    assert own["forgotten"] is True
    left = await svc.run_command("recall", {"query": "забывчивый", "chat_id": PRIVATE})
    assert left["count"] == 0


async def test_common_knowledge_is_not_erased_from_a_random_chat(svc):
    """Общее знание заводит человек руками — пусть руками и убирает."""
    added = await svc.run_command(
        "remember", {"text": "Рой состоит из alfred, winpc и jeeves", "chat_id": FAMILY,
                     "scope": "common"}
    )
    result = await svc.run_command("forget", {"id": added["id"], "chat_id": FAMILY})
    assert result["forgotten"] is False


async def test_too_long_fact_is_refused(svc):
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(
            "remember", {"text": "и" * (memory_service.MAX_FACT_CHARS + 1), "chat_id": FAMILY}
        )
    assert excinfo.value.code == ERR_BAD_REQUEST
    assert "одну мысль" in excinfo.value.message


async def test_remember_without_chat_id_is_refused(svc):
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command("remember", {"text": "ничей факт"})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_recall_ignores_short_and_punctuation_only_queries(svc):
    """Живое сообщение приходит как есть: пунктуация и служебные слова FTS5
    («AND», «*») не должны ломать разбор запроса."""
    await svc.run_command("remember", {"text": "Кот Барсик спит на роутере", "chat_id": FAMILY})
    for query in ("а он?", "* AND *", "!!!", ""):
        assert (await svc.run_command("recall", {"query": query, "chat_id": FAMILY}))["count"] == 0
    found = await svc.run_command("recall", {"query": "кто такой барсик", "chat_id": FAMILY})
    assert found["count"] == 1


async def test_list_shows_own_and_common_facts(svc):
    await svc.run_command("remember", {"text": "Свой факт", "chat_id": FAMILY})
    await svc.run_command("remember", {"text": "Общий факт", "chat_id": PRIVATE, "scope": "common"})
    await svc.run_command("remember", {"text": "Чужой факт", "chat_id": PRIVATE})
    texts = [f["text"] for f in (await svc.run_command("list", {"chat_id": FAMILY}))["facts"]]
    assert set(texts) == {"Свой факт", "Общий факт"}


async def test_state_counts_facts(svc):
    await svc.run_command("remember", {"text": "Раз", "chat_id": FAMILY})
    await svc.run_command("remember", {"text": "Два", "chat_id": PRIVATE})
    assert (await svc.get_state())["facts"] == 2
