from __future__ import annotations

from types import SimpleNamespace

from aiogram.types import BufferedInputFile

from sa_home_bot.bot.notifier import MEDIA_GROUP_MAX, Notifier


class AlbumBot:
    def __init__(self) -> None:
        self.groups: list[dict] = []
        self.photos: list[dict] = []

    async def send_media_group(self, chat_id, media, message_thread_id=None):
        self.groups.append({"chat_id": chat_id, "media": media, "thread": message_thread_id})
        return [SimpleNamespace(message_id=100 + index) for index in range(len(media))]

    async def send_photo(self, chat_id, photo, *, caption=None, message_thread_id=None):
        self.photos.append(
            {"chat_id": chat_id, "photo": photo, "caption": caption, "thread": message_thread_id}
        )
        return SimpleNamespace(message_id=77)


async def test_media_group_sends_one_call_with_caption_only_on_first_photo():
    bot = AlbumBot()
    result = await Notifier(bot).send_media_group(
        5, [b"a", b"b", b"c", b"d"], caption="<b>отчёт</b>", message_thread_id=3
    )

    assert result == [100, 101, 102, 103]
    assert len(bot.groups) == 1 and bot.photos == []
    call = bot.groups[0]
    assert call["chat_id"] == 5 and call["thread"] == 3
    media = call["media"]
    assert [item.caption for item in media] == ["<b>отчёт</b>", None, None, None]
    assert all(item.parse_mode == "HTML" for item in media)
    assert all(isinstance(item.media, BufferedInputFile) for item in media)
    assert [item.media.filename for item in media] == [
        "photo1.png", "photo2.png", "photo3.png", "photo4.png"
    ]


async def test_media_group_with_single_photo_falls_back_to_send_photo():
    bot = AlbumBot()
    result = await Notifier(bot).send_media_group(
        5, [b"a"], caption="подпись", message_thread_id=2
    )
    assert result == [77]
    assert bot.groups == []
    assert bot.photos[0]["caption"] == "подпись" and bot.photos[0]["thread"] == 2


async def test_media_group_with_no_photos_returns_none_without_calls():
    bot = AlbumBot()
    assert await Notifier(bot).send_media_group(5, []) is None
    assert bot.groups == [] and bot.photos == []


async def test_media_group_sends_at_most_ten_photos():
    bot = AlbumBot()
    result = await Notifier(bot).send_media_group(5, [b"x"] * (MEDIA_GROUP_MAX + 2))

    assert len(bot.groups[0]["media"]) == MEDIA_GROUP_MAX
    assert result == [100 + index for index in range(MEDIA_GROUP_MAX)]
