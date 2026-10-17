from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sa_home_bot.proto.errors import ServiceUnavailableError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FAKE_PNG = b"\x89PNG-fake"
FAKE_JPEG = b"\xff\xd8\xff-fake-jpeg-bytes"


class FakeNotifier:
    def __init__(
        self,
        *,
        fail: bool = False,
        send_direct_result: int | None | object = ...,
        send_voice_result: int | None = 42424,
        first_message_id: int = 1000,
    ) -> None:
        self.sent: list[tuple[int, str]] = []
        self.markups: list[object] = []
        self.reply_tos: list[int | None] = []
        self.threads: list[int | None] = []
        self.photos: list[tuple[int, object, str | None]] = []
        self.documents: list[tuple[int, object, str | None]] = []
        self.voices: list[tuple[int, bytes]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.markup_edits: list[tuple[int, int]] = []
        self.deleted: list[tuple[int, int]] = []
        self._fail = fail
        self._send_direct_result = send_direct_result
        self._send_voice_result = send_voice_result
        self._first_message_id = first_message_id

    @property
    def sent_with_markup(self) -> list[tuple[int, str, object]]:
        pairs = zip(self.sent, self.markups, strict=True)
        return [(chat, text, markup) for (chat, text), markup in pairs]

    def texts(self, chat_id: int) -> list[str]:
        return [text for chat, text in self.sent if chat == chat_id]

    async def send_direct(
        self,
        chat_id,
        text,
        reply_to_message_id=None,
        reply_markup=None,
        message_thread_id=None,
        **_options,
    ):
        if self._fail:
            return None
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)
        self.reply_tos.append(reply_to_message_id)
        self.threads.append(message_thread_id)
        if self._send_direct_result is not ...:
            return self._send_direct_result
        return self._first_message_id + len(self.sent)

    async def send_photo_ex(self, chat_id, photo, *, caption=None, **_options):
        self.photos.append((chat_id, photo, caption))
        return 500 + len(self.photos), f"file-{len(self.photos)}"

    async def send_photo(self, chat_id, photo, *, caption=None, **_options):
        self.photos.append((chat_id, photo, caption))
        return 500 + len(self.photos)

    async def send_document(
        self, chat_id, document, *, filename=None, caption=None, **_options
    ):
        self.documents.append((chat_id, document, caption))
        return 600 + len(self.documents)

    async def send_voice(self, chat_id, voice, **_options):
        self.voices.append((chat_id, voice))
        return self._send_voice_result

    async def edit_text(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((chat_id, message_id, text))
        return True

    async def edit_markup(self, chat_id, message_id, reply_markup):
        self.markup_edits.append((chat_id, message_id))
        return True

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True


class FakeNodeLink:
    display_name = "нода"
    connected = True

    def __init__(
        self,
        own_state: dict | None = None,
        *,
        routes: dict[str, dict] | None = None,
        result: Any = None,
        results: dict[str, Any] | None = None,
        error: Exception | None = None,
        description: Any = None,
        actions: list | None = None,
    ) -> None:
        self.own_state = own_state if own_state is not None else {}
        self.routes = dict(routes or {})
        self.result = result if result is not None else {}
        self.results = dict(results or {})
        self.error = error
        self.description = description
        self._actions = list(actions or [])
        self.calls: list[tuple[str, dict, object]] = []

    @property
    def commands(self) -> list[tuple[str, dict]]:
        return [(action, args) for action, args, _dst in self.calls]

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}, dst))
        if self.error is not None:
            raise self.error
        return self.results.get(action, self.result)

    async def get_state(self, dst=None):
        if dst is None:
            return self.own_state
        key = f"{dst.node}:{dst.service}"
        if key in self.routes:
            return self.routes[key]
        raise ServiceUnavailableError("нет связи")

    async def describe(self, dst=None):
        return self.description

    async def actions(self, dst=None):
        return self._actions


@dataclass
class FakeChat:
    id: int = 1
    type: str = "private"


@dataclass
class FakeUser:
    id: int = 7
    first_name: str = "Гость"
    last_name: str | None = None
    username: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}" if self.last_name else self.first_name


class FakeBot:
    def __init__(self, files: dict[str, bytes] | None = None, *, fail_download: bool = False):
        self.files = files or {}
        self.fail_download = fail_download
        self.typing_chats: list[int] = []
        self.typing_threads: list[int | None] = []
        self.downloads: list[object] = []
        self.sent: list[dict] = []
        self.deleted: list[tuple[int, int]] = []
        self.send_script: list[Exception | None] = []

    async def send_chat_action(self, chat_id, action, message_thread_id=None):
        self.typing_chats.append(chat_id)
        self.typing_threads.append(message_thread_id)

    async def download(self, obj, destination=None):
        self.downloads.append(obj)
        if self.fail_download:
            raise RuntimeError("не скачалось")
        data = self.files.get(getattr(obj, "file_id", obj), FAKE_JPEG)
        if destination is not None:
            destination.write_bytes(data)
            return None
        return io.BytesIO(data)

    async def send_message(
        self,
        chat_id,
        text,
        reply_parameters=None,
        reply_markup=None,
        message_thread_id=None,
        **options,
    ):
        if self.send_script:
            failure = self.send_script.pop(0)
            if failure is not None:
                raise failure
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "message_thread_id": message_thread_id,
                **options,
            }
        )
        return FakeSentMessage(message_id=len(self.sent))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


@dataclass
class FakeSentMessage:
    message_id: int = 1


@dataclass
class FakeMessage:
    chat_id: int = 1
    text: str | None = None
    chat_type: str = "private"
    user_id: int | None = None
    message_id: int = 42
    message_thread_id: int | None = None
    bot: FakeBot = field(default_factory=FakeBot)
    answers: list[str] = field(default_factory=list)
    answer_markups: list[object] = field(default_factory=list)
    edits: list[str] = field(default_factory=list)
    edit_keyboards: list[object] = field(default_factory=list)
    reply_markup_edits: list[object] = field(default_factory=list)

    @property
    def chat(self) -> FakeChat:
        return FakeChat(self.chat_id, self.chat_type)

    @property
    def from_user(self) -> FakeUser | None:
        return FakeUser(id=self.user_id) if self.user_id is not None else None

    async def answer(self, text, reply_markup=None, **_options):
        self.answers.append(text)
        self.answer_markups.append(reply_markup)
        return FakeSentMessage(message_id=len(self.answers))

    reply = answer

    async def edit_text(self, text, reply_markup=None, **_options):
        self.edits.append(text)
        self.edit_keyboards.append(reply_markup)

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup_edits.append(reply_markup)


class FakeCallback:
    def __init__(self, data: str, *, chat_id: int = 1, callback_id: str = "cb-1") -> None:
        self.data = data
        self.id = callback_id
        self.message = FakeMessage(chat_id=chat_id)
        self.answered: list[tuple[tuple, dict]] = []

    async def answer(self, *args, **kwargs):
        self.answered.append((args, kwargs))


class Clock:
    def __init__(self, start: datetime | float = 1000.0) -> None:
        self.now = start

    @property
    def t(self) -> float:
        return self.now

    def __call__(self):
        return self.now

    def advance(self, seconds: float = 0, **timedelta_options: float) -> None:
        if isinstance(self.now, datetime):
            self.now += timedelta(seconds=seconds, **timedelta_options)
        else:
            self.now += seconds + timedelta(**timedelta_options).total_seconds()
