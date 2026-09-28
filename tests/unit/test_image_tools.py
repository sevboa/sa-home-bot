"""bot/image_tools.py: тулы /ai generate_image и find_image (Этап 48).

Store — настоящий (sqlite во временном каталоге), node_link и notifier —
фейки: нода llm «рисует» заранее заготовленный PNG, notifier записывает, что
и как ушло в Telegram, и отдаёт выдуманный file_id."""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace

import pytest
import pytest_asyncio
from PIL import Image

from sa_home_bot.bot import image_tools
from sa_home_bot.bot.service_link import ServiceTimeoutError, ServiceUnavailableError
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import ERR_INTERNAL, ProtoError

from .conftest import BASE_TIME

CHAT = 100
OTHER_CHAT = 200


def _png(size: int = 16, color=(200, 80, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, format="PNG")
    return buf.getvalue()


class FakeNodeLink:
    def __init__(self, png: bytes | None = None, error: Exception | None = None) -> None:
        self.png = png or _png()
        self.error = error
        self.calls: list[dict] = []

    async def command(self, action, args, *, dst, timeout):
        self.calls.append({"action": action, "args": args, "dst": dst, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return {
            "png_b64": base64.b64encode(self.png).decode(),
            "width": 16,
            "height": 16,
            "seconds": 12.3,
            "prompt": "red dragon, old castle, sunset",
        }


class FakeNotifier:
    """``results`` — очередь ответов send_photo_ex; кончилась — успешная отправка."""

    def __init__(self, results: list | None = None) -> None:
        self.results = list(results or [])
        self.sent: list[dict] = []
        self._next_msg = 500

    async def send_photo_ex(self, chat_id, photo, *, caption=None, message_thread_id=None,
                            reply_to_message_id=None, **_):
        self.sent.append({
            "chat_id": chat_id, "photo": photo, "caption": caption,
            "thread": message_thread_id, "reply_to": reply_to_message_id,
        })
        if self.results:
            return self.results.pop(0)
        self._next_msg += 1
        return self._next_msg, f"file-{self._next_msg}"


class Remember:
    def __init__(self) -> None:
        self.episodes: list[str] = []

    async def __call__(self, text: str) -> None:
        self.episodes.append(text)


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


def _ctx(store, *, notifier=None, node_link=None, chat_id=CHAT, **llm):
    llm.setdefault("imagegen_display_px", 64)
    return SimpleNamespace(
        chat_id=chat_id,
        settings=Settings(llm=LlmConfig(**llm)),
        node_link=node_link if node_link is not None else FakeNodeLink(),
        notifier=notifier if notifier is not None else FakeNotifier(),
        store=store,
        author="Сева",
        message_thread_id=7,
        trigger_message_id=42,
    )


GEN_ARGS = {
    "description": "огромный красный дракон кружит над старым замком на закате",
    "prompt_ru": "дракон над замком",
    "caption": "Красный дракон",
}


async def _add(store, *, chat_id=CHAT, caption="Красный дракон", prompt_ru="дракон над замком",
               png=None, file_id=None) -> int:
    image_id = await store.add_image(
        chat_id=chat_id, author="Сева", prompt_ru=prompt_ru, prompt_en="a dragon",
        caption=caption, width=16, height=16, colors=0, png=png or _png(), now=BASE_TIME,
    )
    if file_id is not None:
        await store.set_image_sent(image_id, file_id, 1)
    return image_id


# --- generate ---


async def test_generate_saves_sends_stores_file_id_and_remembers(store):
    link, notifier, remember = FakeNodeLink(), FakeNotifier(), Remember()
    ctx = _ctx(store, node_link=link, notifier=notifier)

    result = await image_tools.generate(ctx, dict(GEN_ARGS, negative_en="text"), remember)

    assert len(link.calls) == 1
    call = link.calls[0]
    assert call["action"] == "generate_image"
    assert call["args"] == {
        "description": GEN_ARGS["description"], "negative": "text", "chat_id": CHAT,
    }
    assert (call["dst"].node, call["dst"].service) == ("mycraft", "llm")

    [sent] = notifier.sent
    assert sent["chat_id"] == CHAT
    assert sent["caption"] == "Красный дракон"
    assert (sent["thread"], sent["reply_to"]) == (7, 42)
    # в чат уходит увеличенная копия (16 → 64 при display_px=64), в БД — маленькая
    assert Image.open(io.BytesIO(sent["photo"])).size == (64, 64)

    [row] = await store.recent_images(CHAT)
    image = await store.get_image(CHAT, row["id"])
    assert image["png"] == link.png
    assert image["prompt_ru"] == "дракон над замком"
    # в БД — промпт, который реально ушёл в модель (после промптера)
    assert image["prompt_en"] == "red dragon, old castle, sunset"
    assert image["author"] == "Сева"
    assert image["telegram_file_id"] == "file-501"
    assert image["message_id"] == 501

    assert len(remember.episodes) == 1
    assert f"#{row['id']}" in remember.episodes[0] and "Красный дракон" in remember.episodes[0]
    assert f"#{row['id']}" in result and "отправлена" in result


async def test_generate_without_description_is_error_and_no_call(store):
    link = FakeNodeLink()
    result = await image_tools.generate(
        _ctx(store, node_link=link), {"prompt_ru": "кот", "caption": "кот"}, Remember()
    )
    assert result.startswith("ошибка")
    assert link.calls == []


async def test_generate_caption_falls_back_to_prompt_ru(store):
    notifier = FakeNotifier()
    args = {"description": "рыжий кот", "prompt_ru": "рыжий кот"}
    await image_tools.generate(_ctx(store, notifier=notifier), args, Remember())
    assert notifier.sent[0]["caption"] == "рыжий кот"


@pytest.mark.parametrize(
    "error",
    [
        ServiceUnavailableError("нет связи с mycraft"),
        ServiceTimeoutError("не дождались"),
        ProtoError(ERR_INTERNAL, "не удалось нарисовать картинку"),
        TimeoutError(),
    ],
)
async def test_generate_node_error_is_reported_and_nothing_saved(store, error):
    notifier, remember = FakeNotifier(), Remember()
    ctx = _ctx(store, node_link=FakeNodeLink(error=error), notifier=notifier)
    result = await image_tools.generate(ctx, GEN_ARGS, remember)
    assert result.startswith("не получилось нарисовать")
    assert notifier.sent == []
    assert remember.episodes == []
    assert await store.recent_images(CHAT) == []


async def test_generate_without_notifier_refuses_before_calling_node(store):
    link = FakeNodeLink()
    ctx = _ctx(store, node_link=link)
    ctx.notifier = None
    result = await image_tools.generate(ctx, GEN_ARGS, Remember())
    assert result.startswith("недоступно")
    assert link.calls == []


async def test_generate_without_chat_refuses(store):
    link = FakeNodeLink()
    ctx = _ctx(store, node_link=link)
    ctx.chat_id = None
    assert (await image_tools.generate(ctx, GEN_ARGS, Remember())).startswith("недоступно")
    assert link.calls == []


async def test_generate_without_node_link_is_error(store):
    ctx = _ctx(store)
    ctx.node_link = None
    assert (await image_tools.generate(ctx, GEN_ARGS, Remember())).startswith("ошибка")


async def test_generate_daily_limit_blocks_and_is_per_chat(store):
    link = FakeNodeLink()
    ctx = _ctx(store, node_link=link, imagegen_daily_limit=2)
    for _ in range(2):
        assert "отправлена" in await image_tools.generate(ctx, GEN_ARGS, Remember())
    result = await image_tools.generate(ctx, GEN_ARGS, Remember())
    assert result.startswith("отказ") and "2" in result
    assert len(link.calls) == 2

    other = _ctx(store, node_link=link, chat_id=OTHER_CHAT, imagegen_daily_limit=2)
    assert "отправлена" in await image_tools.generate(other, GEN_ARGS, Remember())


async def test_generate_zero_daily_limit_means_unlimited(store):
    link = FakeNodeLink()
    ctx = _ctx(store, node_link=link, imagegen_daily_limit=0)
    for _ in range(3):
        assert "отправлена" in await image_tools.generate(ctx, GEN_ARGS, Remember())
    assert len(link.calls) == 3


async def test_generate_telegram_refused_keeps_image_without_file_id(store):
    remember = Remember()
    ctx = _ctx(store, notifier=FakeNotifier(results=[None]))
    result = await image_tools.generate(ctx, GEN_ARGS, remember)
    assert "не ушла" in result
    [row] = await store.recent_images(CHAT)
    assert row["telegram_file_id"] is None
    assert remember.episodes == []


# --- find ---


async def test_find_by_id_resends_by_file_id_without_bytes(store):
    image_id = await _add(store, file_id="tg-file-abc")
    notifier = FakeNotifier()
    result = await image_tools.find(_ctx(store, notifier=notifier), {"image_id": image_id})
    [sent] = notifier.sent
    assert sent["photo"] == "tg-file-abc"
    assert sent["caption"] == "Красный дракон"
    assert (sent["thread"], sent["reply_to"]) == (7, 42)
    assert f"#{image_id}" in result and "ещё раз" in result


async def test_find_falls_back_to_bytes_when_file_id_rejected(store):
    image_id = await _add(store, file_id="stale-file-id")
    notifier = FakeNotifier(results=[None, (900, "fresh-file-id")])
    result = await image_tools.find(_ctx(store, notifier=notifier), {"image_id": image_id})
    assert [type(s["photo"]) for s in notifier.sent] == [str, bytes]
    assert Image.open(io.BytesIO(notifier.sent[1]["photo"])).size == (64, 64)
    image = await store.get_image(CHAT, image_id)
    assert image["telegram_file_id"] == "fresh-file-id"
    assert image["message_id"] == 900
    assert "ещё раз" in result


async def test_find_without_file_id_sends_bytes_and_saves_file_id(store):
    image_id = await _add(store)
    notifier = FakeNotifier()
    await image_tools.find(_ctx(store, notifier=notifier), {"image_id": image_id})
    [sent] = notifier.sent
    assert isinstance(sent["photo"], bytes)
    assert (await store.get_image(CHAT, image_id))["telegram_file_id"] == "file-501"


async def test_find_both_sends_failed(store):
    image_id = await _add(store, file_id="stale")
    notifier = FakeNotifier(results=[None, None])
    result = await image_tools.find(_ctx(store, notifier=notifier), {"image_id": image_id})
    assert "не принял" in result
    assert (await store.get_image(CHAT, image_id))["telegram_file_id"] == "stale"


async def test_find_by_query_with_russian_ending(store):
    await _add(store, caption="Рыжий кот", prompt_ru="рыжий кот в шляпе")
    dragon = await _add(store, file_id="dragon-file")
    notifier = FakeNotifier()
    result = await image_tools.find(_ctx(store, notifier=notifier), {"query": "покажи дракона"})
    assert notifier.sent[0]["photo"] == "dragon-file"
    assert f"#{dragon}" in result


async def test_find_foreign_image_id_is_not_found(store):
    foreign = await _add(store, chat_id=OTHER_CHAT, file_id="secret")
    notifier = FakeNotifier()
    result = await image_tools.find(_ctx(store, notifier=notifier), {"image_id": foreign})
    assert "нет" in result
    assert notifier.sent == []


async def test_find_foreign_image_by_query_is_not_found(store):
    await _add(store, chat_id=OTHER_CHAT, file_id="secret")
    notifier = FakeNotifier()
    result = await image_tools.find(_ctx(store, notifier=notifier), {"query": "дракона"})
    assert result == "в этом чате я ещё ничего не рисовал"
    assert notifier.sent == []


async def test_find_in_empty_chat(store):
    result = await image_tools.find(_ctx(store), {})
    assert result == "в этом чате я ещё ничего не рисовал"


async def test_find_no_match_lists_recent_candidates(store):
    ids = [await _add(store, caption=f"Кот {i}", prompt_ru="кот") for i in range(7)]
    notifier = FakeNotifier()
    result = await image_tools.find(_ctx(store, notifier=notifier), {"query": "паровоз"})
    assert notifier.sent == []
    assert "ничего не нашлось" in result
    newest_five = list(reversed(ids))[:5]
    for image_id in newest_five:
        assert f"#{image_id}" in result
    assert f"#{ids[0]} " not in result  # самые старые за пределами списка
    assert result.index(f"#{ids[-1]}") < result.index(f"#{ids[-2]}")


async def test_find_bool_image_id_is_not_treated_as_id(store):
    await _add(store, caption="Кот", prompt_ru="кот")
    notifier = FakeNotifier()
    result = await image_tools.find(_ctx(store, notifier=notifier), {"image_id": True})
    assert notifier.sent == []
    assert "Последние картинки" in result


async def test_find_without_notifier_refuses(store):
    await _add(store)
    ctx = _ctx(store)
    ctx.notifier = None
    assert (await image_tools.find(ctx, {"query": "дракон"})).startswith("недоступно")


async def test_generate_accepts_legacy_prompt_en(store):
    link = FakeNodeLink()
    args = {"prompt_en": "a ginger cat", "prompt_ru": "рыжий кот", "caption": "Кот"}
    await image_tools.generate(_ctx(store, node_link=link), args, Remember())
    assert link.calls[0]["args"]["description"] == "a ginger cat"
