"""Store: таблица картинок /ai (Этап 48) — images + FTS5 images_fts.

Главное: поиск ловит русские окончания («дракона» → «дракон») и картинки
одного чата не видны из другого — ни поиском, ни по номеру."""

from __future__ import annotations

from datetime import timedelta

import pytest_asyncio

from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store

from .conftest import BASE_TIME

CHAT = 100
OTHER_CHAT = 200


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


async def _add(store: Store, *, chat_id: int = CHAT, caption: str = "Рыжий кот",
               prompt_ru: str = "нарисуй рыжего кота", prompt_en: str = "a ginger cat",
               now=BASE_TIME, png: bytes = b"PNG") -> int:
    return await store.add_image(
        chat_id=chat_id, author="Сева", prompt_ru=prompt_ru, prompt_en=prompt_en,
        caption=caption, width=128, height=128, colors=0, png=png, now=now,
    )


async def test_add_then_get_returns_bytes_and_empty_file_id(store):
    image_id = await _add(store, png=b"\x89PNG-bytes")
    image = await store.get_image(CHAT, image_id)
    assert image is not None
    assert image["png"] == b"\x89PNG-bytes"
    assert image["caption"] == "Рыжий кот"
    assert image["telegram_file_id"] is None
    assert image["message_id"] is None


async def test_get_image_of_another_chat_is_none(store):
    image_id = await _add(store, chat_id=OTHER_CHAT)
    assert await store.get_image(CHAT, image_id) is None
    assert await store.get_image(OTHER_CHAT, image_id) is not None


async def test_set_image_sent_stores_file_id_and_keeps_message_id_on_none(store):
    image_id = await _add(store)
    await store.set_image_sent(image_id, "file-1", 55)
    await store.set_image_sent(image_id, "file-2", None)
    image = await store.get_image(CHAT, image_id)
    assert image["telegram_file_id"] == "file-2"
    assert image["message_id"] == 55  # COALESCE: None не затирает прежний


async def test_search_catches_russian_word_endings(store):
    dragon = await _add(store, caption="Красный дракон", prompt_ru="нарисуй дракон над замком",
                        prompt_en="a red dragon")
    await _add(store)  # кот — не должен найтись по дракону
    for query in ("дракона", "драконом", "покажи дракона ещё раз"):
        found = await store.search_images(CHAT, query)
        assert [r["id"] for r in found] == [dragon], query


async def test_search_finds_by_english_prompt_too(store):
    image_id = await _add(store, caption="Котик", prompt_ru="котик", prompt_en="a ginger cat")
    found = await store.search_images(CHAT, "ginger")
    assert [r["id"] for r in found] == [image_id]


async def test_search_result_has_no_png_bytes(store):
    await _add(store)
    found = await store.search_images(CHAT, "кота")
    assert found and "png" not in found[0]


async def test_search_does_not_see_other_chat(store):
    await _add(store, chat_id=OTHER_CHAT, caption="Красный дракон", prompt_ru="дракон")
    assert await store.search_images(CHAT, "дракона") == []
    assert len(await store.search_images(OTHER_CHAT, "дракона")) == 1


async def test_search_ignores_short_and_punctuation_only_queries(store):
    await _add(store)
    assert await store.search_images(CHAT, "") == []
    assert await store.search_images(CHAT, "а и ?!") == []


async def test_search_query_with_fts_syntax_does_not_crash(store):
    await _add(store)
    # кавычки/звёздочки/NEAR/OR — операторы FTS5; \w+ их вырезает
    found = await store.search_images(CHAT, 'кота" OR * NEAR( -"')
    assert len(found) == 1


async def test_recent_images_newest_first_and_limited(store):
    ids = [await _add(store, caption=f"картинка {i}") for i in range(4)]
    await _add(store, chat_id=OTHER_CHAT)
    recent = await store.recent_images(CHAT, limit=3)
    assert [r["id"] for r in recent] == list(reversed(ids))[:3]
    assert await store.recent_images(999) == []


async def test_count_images_since_counts_only_window_and_chat(store):
    await _add(store, now=BASE_TIME - timedelta(days=2))
    await _add(store, now=BASE_TIME - timedelta(hours=3))
    await _add(store, now=BASE_TIME)
    await _add(store, chat_id=OTHER_CHAT, now=BASE_TIME)
    assert await store.count_images_since(CHAT, BASE_TIME - timedelta(days=1)) == 2
    assert await store.count_images_since(OTHER_CHAT, BASE_TIME - timedelta(days=1)) == 1


# --- Этап 49: отладочный /draw — purpose='debug'/'ref' не видны разговору ---


async def _add_debug(store: Store, purpose: str = "debug") -> int:
    return await store.add_image(
        chat_id=CHAT, author="Сева", prompt_ru="рыжий кот", prompt_en="a ginger cat",
        caption="Рыжий кот", width=64, height=64, colors=32, png=b"PNG", now=BASE_TIME,
        purpose=purpose, params='{"mode": "item"}',
    )


async def test_debug_images_hidden_from_chat_lookups(store):
    debug_id = await _add_debug(store)
    ref_id = await _add_debug(store, "ref")
    assert await store.get_image(CHAT, debug_id) is None
    assert await store.search_images(CHAT, "кот") == []
    assert await store.recent_images(CHAT) == []
    assert await store.count_images_since(CHAT, BASE_TIME - timedelta(days=1)) == 0
    # а сам /draw видит любую — по номеру и по сообщению
    assert (await store.image_by_id(ref_id))["purpose"] == "ref"
    await store.set_image_sent(debug_id, "file-1", 77)
    assert (await store.image_by_message(CHAT, 77))["id"] == debug_id


async def test_clean_deletes_only_debug(store):
    chat_id = await _add(store)
    await _add_debug(store)
    keep_id = await _add_debug(store)
    assert await store.set_image_purpose(keep_id, "ref")
    assert await store.count_images_by_purpose("debug") == 1
    assert await store.delete_images_by_purpose("debug") == 1
    assert await store.count_images_by_purpose("debug") == 0
    assert await store.image_by_id(keep_id) is not None
    assert [r["id"] for r in await store.search_images(CHAT, "кот")] == [chat_id]
