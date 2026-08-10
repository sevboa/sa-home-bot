"""Приём и распознавание голосовых на стороне alfred (bot/voice_stt.py).

wake_core.fetch_state/ensure_service_ready замокан целиком — тот сценарий
уже подробно проверен в test_wake_core.py, здесь важна только реакция
voice_stt на его исходы (READY/UNREACHABLE/WARMUP_FAILED), на содержимое
транскрипта и на то, куда идут статусы: message.answer (plain) vs
push_status/finalize_status общей RichStreamSession (rich, thinking-блок —
переиспользует персонажные константы ai_flow.STEPS_TEXT/ALBERT_*, см.
докстринг voice_stt.py)."""

from __future__ import annotations

import io

import pytest

from sa_home_bot import wake_core
from sa_home_bot.bot import ai_flow, voice_stt
from sa_home_bot.config import LlmConfig, Settings


def _settings(**overrides) -> Settings:
    overrides.setdefault("stt_max_duration_s", 600.0)
    return Settings(llm=LlmConfig(**overrides))


class FakeVoice:
    def __init__(self, duration: int = 5) -> None:
        self.duration = duration


class FakeChat:
    def __init__(self, chat_id: int = 1) -> None:
        self.id = chat_id


class FakeBot:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.download_calls = 0
        self.typing_actions: list[int] = []

    async def download(self, voice: FakeVoice) -> io.BytesIO:
        self.download_calls += 1
        return io.BytesIO(self.payload)

    async def send_chat_action(self, chat_id, action, message_thread_id=None) -> None:
        self.typing_actions.append(chat_id)


class FakeMessage:
    def __init__(self, voice: FakeVoice | None, payload: bytes = b"raw-audio-bytes") -> None:
        self.voice = voice
        self.chat = FakeChat()
        self.bot = FakeBot(payload)
        self.message_thread_id = None
        self.sent: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.sent.append(text)


class FakeRichSession:
    """Записывает вызовы push_status/finalize_status — реальная
    RichStreamSession (bot/rich_stream.py) требует настоящий aiogram.Bot,
    тут важна только последовательность/содержимое статусов."""

    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.finalized: list[str] = []

    async def push_status(self, text: str) -> None:
        self.statuses.append(text)

    async def finalize_status(self, markdown: str) -> None:
        self.finalized.append(markdown)


class FakeNodeLink:
    def __init__(self, transcript: str = "привет альфред") -> None:
        self.transcript = transcript
        self.commands: list[tuple[str, dict]] = []

    async def command(self, action, args=None, dst=None, timeout=None):
        args = args or {}
        self.commands.append((action, args))
        if action == voice_stt.ACTION_STT_UPLOAD_CHUNK:
            return {"received": len(args.get("data_b64", ""))}
        if action == voice_stt.ACTION_TRANSCRIBE_VOICE:
            return {"transcript": self.transcript}
        raise AssertionError(f"неожиданное действие {action}")


class RaisingNodeLink(FakeNodeLink):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    async def command(self, action, args=None, dst=None, timeout=None):
        raise self._exc


def _patch_wake(monkeypatch, *, fetch_state_result, ensure_outcome):
    async def fake_fetch_state(node_link, dst, *, timeout_s=None):
        return fetch_state_result

    async def fake_ensure_service_ready(node_link, store, node_id, service, **kwargs):
        return ensure_outcome

    monkeypatch.setattr(wake_core, "fetch_state", fake_fetch_state)
    monkeypatch.setattr(wake_core, "ensure_service_ready", fake_ensure_service_ready)


async def test_too_long_voice_rejected_without_download(monkeypatch):
    message = FakeMessage(FakeVoice(duration=1000))
    config = _settings(stt_max_duration_s=600.0)
    link = FakeNodeLink()

    result = await voice_stt.transcribe_voice_message(message, link, store=None, config=config)

    assert result is None
    assert message.sent == [voice_stt.VOICE_TOO_LONG_TEXT]
    assert message.bot.download_calls == 0
    assert link.commands == []


async def test_unavailable_when_service_not_ready(monkeypatch):
    _patch_wake(monkeypatch, fetch_state_result=None, ensure_outcome=wake_core.UNREACHABLE)
    message = FakeMessage(FakeVoice())
    link = FakeNodeLink()

    result = await voice_stt.transcribe_voice_message(message, link, store=None, config=_settings())

    assert result is None
    # Недоступность мycraft — тот же персонаж/текст, что и у текстового /ai
    # (ai_flow.ALBERT_UNAVAILABLE), не отдельная голосовая копия.
    assert ai_flow.ALBERT_UNAVAILABLE in message.sent
    assert message.bot.download_calls == 0
    assert link.commands == []


async def test_waiting_text_shown_when_initially_asleep(monkeypatch):
    _patch_wake(monkeypatch, fetch_state_result=None, ensure_outcome=wake_core.READY)
    message = FakeMessage(FakeVoice())
    link = FakeNodeLink(transcript="разбудили и распознали")

    result = await voice_stt.transcribe_voice_message(message, link, store=None, config=_settings())

    assert result == "разбудили и распознали"
    # "Шаги" (ожидание пробуждения) + "слушаю" (сама транскрипция) — та же
    # реплика "шагов", что и в текстовом /ai (ai_flow.STEPS_TEXT).
    assert message.sent == [ai_flow.STEPS_TEXT, voice_stt.VOICE_LISTENING_TEXT]


async def test_success_returns_transcript_without_waiting_text_when_already_warm(monkeypatch):
    _patch_wake(
        monkeypatch, fetch_state_result={"asleep": False}, ensure_outcome=wake_core.READY
    )
    message = FakeMessage(FakeVoice())
    link = FakeNodeLink(transcript="привет альфред")

    result = await voice_stt.transcribe_voice_message(message, link, store=None, config=_settings())

    assert result == "привет альфред"
    # Нет "шагов" (mycraft уже не спала) — только статус "слушаю".
    assert message.sent == [voice_stt.VOICE_LISTENING_TEXT]
    action, args = link.commands[0]
    assert action == voice_stt.ACTION_TRANSCRIBE_VOICE
    assert "audio_b64" in args
    assert args["chat_id"] == 1


async def test_empty_transcript_sends_failed_text(monkeypatch):
    _patch_wake(
        monkeypatch, fetch_state_result={"asleep": False}, ensure_outcome=wake_core.READY
    )
    message = FakeMessage(FakeVoice())
    link = FakeNodeLink(transcript="   ")

    result = await voice_stt.transcribe_voice_message(message, link, store=None, config=_settings())

    assert result is None
    assert message.sent == [voice_stt.VOICE_LISTENING_TEXT, voice_stt.VOICE_RECOGNITION_FAILED_TEXT]


async def test_service_error_sends_hiccup_text(monkeypatch):
    from sa_home_bot.proto.messages import ERR_UNAVAILABLE, ProtoError

    _patch_wake(
        monkeypatch, fetch_state_result={"asleep": False}, ensure_outcome=wake_core.READY
    )
    message = FakeMessage(FakeVoice())
    link = RaisingNodeLink(ProtoError(ERR_UNAVAILABLE, "служба недоступна"))

    result = await voice_stt.transcribe_voice_message(message, link, store=None, config=_settings())

    assert result is None
    # Сбой уже ВО ВРЕМЯ распознавания (mycraft была доступна) — тот же
    # персонаж-текст, что и у сбоя генерации в текстовом /ai
    # (ai_flow.ALBERT_HICCUP), не ALBERT_UNAVAILABLE (та — только про сам
    # wake-сценарий).
    assert message.sent == [voice_stt.VOICE_LISTENING_TEXT, ai_flow.ALBERT_HICCUP]


async def test_large_voice_uses_chunked_upload_with_session(monkeypatch):
    _patch_wake(
        monkeypatch, fetch_state_result={"asleep": False}, ensure_outcome=wake_core.READY
    )
    # После base64 (раздувает на треть) должно уйти за _INLINE_VOICE_B64_BYTES.
    payload = b"x" * 1_500_000
    message = FakeMessage(FakeVoice(), payload=payload)
    link = FakeNodeLink(transcript="длинное голосовое")

    result = await voice_stt.transcribe_voice_message(message, link, store=None, config=_settings())

    assert result == "длинное голосовое"
    chunk_actions = [a for a, _ in link.commands if a == voice_stt.ACTION_STT_UPLOAD_CHUNK]
    assert len(chunk_actions) >= 2  # payload крупнее _CHUNK_BYTES
    final_action, final_args = link.commands[-1]
    assert final_action == voice_stt.ACTION_TRANSCRIBE_VOICE
    assert "audio_b64" not in final_args
    assert final_args["expected_size"] == len(payload)
    assert "session_id" in final_args
    assert "expected_sha256" in final_args


@pytest.mark.parametrize("outcome", [wake_core.UNREACHABLE, wake_core.WARMUP_FAILED])
async def test_any_non_ready_outcome_is_treated_as_unavailable(monkeypatch, outcome):
    _patch_wake(monkeypatch, fetch_state_result={"asleep": False}, ensure_outcome=outcome)
    message = FakeMessage(FakeVoice())
    link = FakeNodeLink()

    result = await voice_stt.transcribe_voice_message(message, link, store=None, config=_settings())

    assert result is None
    assert message.sent == [ai_flow.ALBERT_UNAVAILABLE]


# --- rich-режим: статусы идут через push_status/finalize_status общей
# сессии, а не message.answer (см. bot/ai_flow.py::_announce_steps/
# _announce_albert про тот же выбор для текстового /ai) ---


async def test_rich_session_gets_listening_status_not_message_answer(monkeypatch):
    _patch_wake(
        monkeypatch, fetch_state_result={"asleep": False}, ensure_outcome=wake_core.READY
    )
    message = FakeMessage(FakeVoice())
    link = FakeNodeLink(transcript="привет альфред")
    rich_session = FakeRichSession()

    result = await voice_stt.transcribe_voice_message(
        message, link, store=None, config=_settings(), rich_session=rich_session
    )

    assert result == "привет альфред"
    assert message.sent == []  # ничего голым message.answer
    assert rich_session.statuses == [voice_stt.VOICE_LISTENING_TEXT_PLAIN]
    # Успех: сессия НЕ финализируется здесь — черновик остаётся активным для
    # финального ответа Альфреда (см. докстринг transcribe_voice_message).
    assert rich_session.finalized == []


async def test_rich_session_wake_wait_pushes_steps_status(monkeypatch):
    _patch_wake(monkeypatch, fetch_state_result=None, ensure_outcome=wake_core.READY)
    message = FakeMessage(FakeVoice())
    link = FakeNodeLink(transcript="разбудили")
    rich_session = FakeRichSession()

    result = await voice_stt.transcribe_voice_message(
        message, link, store=None, config=_settings(), rich_session=rich_session
    )

    assert result == "разбудили"
    assert rich_session.statuses == [ai_flow.STEPS_TEXT_PLAIN, voice_stt.VOICE_LISTENING_TEXT_PLAIN]


async def test_rich_session_unavailable_finalizes_status_not_message_answer(monkeypatch):
    _patch_wake(monkeypatch, fetch_state_result=None, ensure_outcome=wake_core.UNREACHABLE)
    message = FakeMessage(FakeVoice())
    link = FakeNodeLink()
    rich_session = FakeRichSession()

    result = await voice_stt.transcribe_voice_message(
        message, link, store=None, config=_settings(), rich_session=rich_session
    )

    assert result is None
    assert message.sent == []
    assert rich_session.finalized == [ai_flow.ALBERT_UNAVAILABLE_MD]


async def test_rich_session_empty_transcript_finalizes_status(monkeypatch):
    _patch_wake(
        monkeypatch, fetch_state_result={"asleep": False}, ensure_outcome=wake_core.READY
    )
    message = FakeMessage(FakeVoice())
    link = FakeNodeLink(transcript="")
    rich_session = FakeRichSession()

    result = await voice_stt.transcribe_voice_message(
        message, link, store=None, config=_settings(), rich_session=rich_session
    )

    assert result is None
    assert rich_session.finalized == [voice_stt.VOICE_RECOGNITION_FAILED_TEXT_MD]


async def test_rich_session_too_long_finalizes_status_without_wake_probe(monkeypatch):
    message = FakeMessage(FakeVoice(duration=1000))
    link = FakeNodeLink()
    rich_session = FakeRichSession()

    result = await voice_stt.transcribe_voice_message(
        message, link, store=None, config=_settings(stt_max_duration_s=600.0),
        rich_session=rich_session,
    )

    assert result is None
    assert rich_session.finalized == [voice_stt.VOICE_TOO_LONG_TEXT_MD]
    assert rich_session.statuses == []
    assert message.bot.download_calls == 0
