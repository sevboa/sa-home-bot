"""Синтез голосового ответа на стороне alfred (bot/voice_tts.py).

wake_core.ensure_service_ready замокан целиком (см. test_voice_stt.py про
тот же приём) — здесь важна только реакция voice_tts на его исход, на
инлайн/чанкованный ответ службы и на сбои (откат на None, не исключение)."""

from __future__ import annotations

import base64
import hashlib

from sa_home_bot import wake_core
from sa_home_bot.bot import voice_tts
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.proto.messages import ERR_INTERNAL, ProtoError


def _settings(**overrides) -> Settings:
    return Settings(llm=LlmConfig(**overrides))


class FakeChat:
    def __init__(self, chat_id: int = 1) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(self, chat_id: int | None = 1) -> None:
        self.chat = FakeChat(chat_id) if chat_id is not None else None
        self.message_thread_id = None
        self.sent: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.sent.append(text)


class FakeRichSession:
    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.finalized: list[str] = []

    async def push_status(self, text: str) -> None:
        self.statuses.append(text)

    async def finalize_status(self, markdown: str) -> None:
        self.finalized.append(markdown)


class FakeNodeLink:
    def __init__(self, *, response: dict | None = None, chunks: list[bytes] | None = None) -> None:
        self._response = response
        self._chunks = chunks or []
        self.commands: list[tuple[str, dict]] = []

    async def command(self, action, args=None, dst=None, timeout=None):
        args = args or {}
        self.commands.append((action, args))
        if action == voice_tts.ACTION_SYNTHESIZE_SPEECH:
            return self._response
        if action == voice_tts.ACTION_TTS_DOWNLOAD_CHUNK:
            offset = args["offset"]
            length = args["length"]
            payload = b"".join(self._chunks)
            chunk = payload[offset : offset + length]
            eof = offset + len(chunk) >= len(payload)
            return {"data_b64": base64.b64encode(chunk).decode(), "offset": offset, "eof": eof}
        raise AssertionError(f"неожиданное действие {action}")


class RaisingNodeLink:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.commands: list[tuple[str, dict]] = []

    async def command(self, action, args=None, dst=None, timeout=None):
        self.commands.append((action, args or {}))
        raise self._exc


def _patch_ready(monkeypatch, outcome=wake_core.READY):
    async def fake_ensure_service_ready(node_link, store, node_id, service, **kwargs):
        return outcome

    monkeypatch.setattr(wake_core, "ensure_service_ready", fake_ensure_service_ready)


async def test_returns_none_when_service_not_ready(monkeypatch):
    _patch_ready(monkeypatch, outcome=wake_core.UNREACHABLE)
    message = FakeMessage()
    link = FakeNodeLink()

    result = await voice_tts.synthesize_voice_reply(
        message, link, store=None, config=_settings(), rich_session=None, text="привет"
    )

    assert result is None
    assert link.commands == []
    assert message.sent == []  # статус синтеза не показывается, если служба недоступна


async def test_inline_response_returns_bytes(monkeypatch):
    _patch_ready(monkeypatch)
    audio = b"fake-ogg-bytes"
    message = FakeMessage()
    link = FakeNodeLink(response={"audio_b64": base64.b64encode(audio).decode(), "format": "ogg"})

    result = await voice_tts.synthesize_voice_reply(
        message, link, store=None, config=_settings(), rich_session=None, text="привет"
    )

    assert result == audio
    action, args = link.commands[0]
    assert action == voice_tts.ACTION_SYNTHESIZE_SPEECH
    assert args["text"] == "привет"
    assert args["chat_id"] == 1
    assert message.sent == [voice_tts.VOICE_SYNTH_TEXT]


async def test_chunked_response_downloads_and_verifies_sha256(monkeypatch):
    _patch_ready(monkeypatch)
    payload = b"x" * 5000
    chunks = [payload[i : i + 2000] for i in range(0, len(payload), 2000)]
    message = FakeMessage()
    link = FakeNodeLink(
        response={
            "session_id": "sess-1",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "format": "ogg",
        },
        chunks=chunks,
    )
    monkeypatch.setattr(voice_tts, "_CHUNK_BYTES", 2000)

    result = await voice_tts.synthesize_voice_reply(
        message, link, store=None, config=_settings(), rich_session=None, text="длинный ответ"
    )

    assert result == payload
    chunk_calls = [a for a, _ in link.commands if a == voice_tts.ACTION_TTS_DOWNLOAD_CHUNK]
    assert len(chunk_calls) >= 2


async def test_chunked_response_sha256_mismatch_returns_none(monkeypatch):
    _patch_ready(monkeypatch)
    payload = b"real-data"
    message = FakeMessage()
    link = FakeNodeLink(
        response={"session_id": "sess-1", "size": len(payload), "sha256": "0" * 64},
        chunks=[payload],
    )

    result = await voice_tts.synthesize_voice_reply(
        message, link, store=None, config=_settings(), rich_session=None, text="текст"
    )

    assert result is None


async def test_proto_error_returns_none(monkeypatch):
    _patch_ready(monkeypatch)
    message = FakeMessage()
    link = RaisingNodeLink(ProtoError(ERR_INTERNAL, "не удалось синтезировать речь"))

    result = await voice_tts.synthesize_voice_reply(
        message, link, store=None, config=_settings(), rich_session=None, text="текст"
    )

    assert result is None


async def test_service_unavailable_returns_none(monkeypatch):
    _patch_ready(monkeypatch)
    message = FakeMessage()
    link = RaisingNodeLink(ServiceUnavailableError("нет связи"))

    result = await voice_tts.synthesize_voice_reply(
        message, link, store=None, config=_settings(), rich_session=None, text="текст"
    )

    assert result is None


async def test_no_chat_returns_none_without_command(monkeypatch):
    _patch_ready(monkeypatch)
    message = FakeMessage(chat_id=None)
    link = FakeNodeLink()

    result = await voice_tts.synthesize_voice_reply(
        message, link, store=None, config=_settings(), rich_session=None, text="текст"
    )

    assert result is None
    assert link.commands == []


async def test_rich_session_gets_synth_status_not_message_answer(monkeypatch):
    _patch_ready(monkeypatch)
    audio = b"bytes"
    message = FakeMessage()
    link = FakeNodeLink(response={"audio_b64": base64.b64encode(audio).decode()})
    rich_session = FakeRichSession()

    result = await voice_tts.synthesize_voice_reply(
        message, link, store=None, config=_settings(), rich_session=rich_session, text="текст"
    )

    assert result == audio
    assert message.sent == []
    assert rich_session.statuses == [voice_tts.VOICE_SYNTH_TEXT_PLAIN]
