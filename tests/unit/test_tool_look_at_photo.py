from __future__ import annotations

from datetime import UTC, datetime

from sa_home_bot.bot import tools
from sa_home_bot.proto.errors import ServiceUnavailableError
from tests.unit.tools_support import (
    CHAT_ID,
)
from tests.unit.tools_support import (
    make_ctx as _ctx,
)


class _FakeLookNodeLink:
    def __init__(self, *, response: str = "ответ", raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict, object]] = []
        self._response = response
        self._raises = raises

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}, dst))
        if self._raises is not None:
            raise self._raises
        return {"response": self._response}


async def test_look_at_photo_requires_question(store):
    result = await tools.tool_look_at_photo(_ctx(store, node_link=_FakeLookNodeLink()), {})
    assert "question" in result


async def test_look_at_photo_no_turn_found_is_honest_not_a_crash(store):
    result = await tools.tool_look_at_photo(
        _ctx(store, node_link=_FakeLookNodeLink()), {"question": "что там?"}
    )
    assert "не найдено" in result


async def test_look_at_photo_no_node_link_returns_error(store):
    await store.record_ai_turn(
        CHAT_ID, 1, 1, "user", "[фото]", datetime.now(tz=UTC), photo_path="key1"
    )
    result = await tools.tool_look_at_photo(_ctx(store, node_link=None), {"question": "что там?"})
    assert "ошибка" in result


async def test_look_at_photo_success_calls_service_with_stored_key(store):
    await store.record_ai_turn(
        CHAT_ID, 1, 1, "user", "[фото]", datetime.now(tz=UTC), photo_path="111_1"
    )
    link = _FakeLookNodeLink(response="на фото рыжий кот")

    result = await tools.tool_look_at_photo(
        _ctx(store, node_link=link), {"question": "какого цвета кот?"}
    )

    assert result == "на фото рыжий кот"
    assert len(link.calls) == 2
    action, args, dst = link.calls[0]
    assert action == tools.ACTION_LOOK_AT_PHOTO
    assert args == {"photo_key": "111_1", "question": "какого цвета кот?", "chat_id": CHAT_ID}
    assert dst.node == tools.LLM_NODE
    assert dst.service == tools.LLM_SERVICE


async def _alfred_picture(store, *, purpose="photo", message_id=500):
    image_id = await store.add_image(
        chat_id=CHAT_ID,
        author=None,
        prompt_ru="кабинет",
        prompt_en="study",
        caption="Мой кабинет",
        width=8,
        height=8,
        colors=32,
        png=_tiny_png(),
        now=datetime.now(tz=UTC),
        purpose=purpose,
    )
    await store.set_image_sent(image_id, "file-1", message_id)
    return image_id


def _tiny_png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 100, 50)).save(buf, format="PNG")
    return buf.getvalue()


async def test_look_at_photo_sees_alfreds_own_snapshot(store):
    image_id = await _alfred_picture(store)
    link = _FakeLookNodeLink(response="на снимке камин")
    result = await tools.tool_look_at_photo(
        _ctx(store, node_link=link), {"question": "что на снимке?", "whose": "mine"}
    )
    assert result == "на снимке камин"
    _, args, _ = link.calls[0]
    assert args["photo_key"] == f"img-{image_id}" and args["raw_image"]


async def test_look_at_photo_reply_to_alfreds_picture_takes_it(store):
    await store.record_ai_turn(
        CHAT_ID, 1, 1, "user", "[фото]", datetime.now(tz=UTC), photo_path="111_1"
    )
    image_id = await _alfred_picture(store, purpose="chat", message_id=777)
    ctx = _ctx(store, node_link=_FakeLookNodeLink())
    ctx.reply_to_message_id = 777
    await tools.tool_look_at_photo(ctx, {"question": "что там?"})
    _, args, _ = ctx.node_link.calls[0]
    assert args["photo_key"] == f"img-{image_id}"


async def test_look_at_photo_mine_without_pictures_is_honest(store):
    result = await tools.tool_look_at_photo(
        _ctx(store, node_link=_FakeLookNodeLink()), {"question": "что?", "whose": "mine"}
    )
    assert "ничего не присылал" in result


async def test_look_at_photo_piggybacks_a_graph_episode(store):
    await store.record_ai_turn(
        CHAT_ID, 1, 1, "user", "[фото]", datetime.now(tz=UTC), photo_path="111_1"
    )
    link = _FakeLookNodeLink(response="на фото рыжий кот")

    await tools.tool_look_at_photo(_ctx(store, node_link=link), {"question": "какого цвета кот?"})

    episode_action, episode_args, episode_dst = link.calls[1]
    assert episode_action == "add_episode"
    assert episode_dst.node == "mycraft"
    assert episode_dst.service == "graph_memory"
    assert episode_args["chat_id"] == CHAT_ID
    assert episode_args["source"] == "look_at_photo"
    assert "на фото рыжий кот" in episode_args["text"]


async def test_look_at_photo_survives_graph_memory_being_asleep(store):
    await store.record_ai_turn(
        CHAT_ID, 1, 1, "user", "[фото]", datetime.now(tz=UTC), photo_path="111_1"
    )

    class FlakyLink(_FakeLookNodeLink):
        async def command(self, action, args=None, dst=None, *, timeout=None):
            if action == "add_episode":
                raise ServiceUnavailableError("mycraft спит")
            return await super().command(action, args=args, dst=dst, timeout=timeout)

    link = FlakyLink(response="на фото рыжий кот")
    result = await tools.tool_look_at_photo(
        _ctx(store, node_link=link), {"question": "какого цвета кот?"}
    )
    assert result == "на фото рыжий кот"


async def test_look_at_photo_picks_latest_photo_turn_in_dialogue(store):
    await store.record_ai_turn(
        CHAT_ID, 1, 1, "user", "[фото]", datetime.now(tz=UTC), photo_path="111_1"
    )
    await store.record_ai_turn(
        CHAT_ID, 2, 1, "user", "[фото]", datetime.now(tz=UTC), photo_path="111_2"
    )
    link = _FakeLookNodeLink()

    await tools.tool_look_at_photo(_ctx(store, node_link=link), {"question": "?"})

    assert link.calls[0][1]["photo_key"] == "111_2"


async def test_look_at_photo_unavailable_returns_apology_not_exception(store):
    await store.record_ai_turn(
        CHAT_ID, 1, 1, "user", "[фото]", datetime.now(tz=UTC), photo_path="111_1"
    )
    link = _FakeLookNodeLink(raises=TimeoutError("узел не отвечает"))

    result = await tools.tool_look_at_photo(_ctx(store, node_link=link), {"question": "что там?"})
    assert "не получилось" in result
