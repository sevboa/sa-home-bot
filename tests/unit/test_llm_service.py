"""Служба llm (Альфред): describe, ask/chat/sleep, идле-таймер.

Ollama/WSL не трогаем (monkeypatch sa_home_bot.llm.service.ollama) — это
чистая loopback-обвязка, ей место в отдельном тесте llm/ollama.py, а не здесь.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.llm import service as llm_service
from sa_home_bot.llm.service import LlmService
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ERR_INTERNAL, ProtoError

PERSONA = "ТЕСТОВЫЙ ПЕРСОНАЖ (persona_prompt в тестовом конфиге)"


@pytest.fixture(autouse=True)
def _isolate_speech_therapy_state(tmp_path, monkeypatch):
    # speech_therapy_state_path — относительный путь по умолчанию (см.
    # config.py::LlmConfig) — без chdir тесты читали/писали бы реальный файл
    # состояния Логопеда в репозитории.
    monkeypatch.chdir(tmp_path)


def _settings(**overrides) -> Settings:
    overrides.setdefault("idle_sleep_after_s", 1800.0)
    overrides.setdefault("persona_prompt", PERSONA)
    return Settings(llm=LlmConfig(model="qwen2.5:7b", **overrides))


class FakeEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


def test_describe_declares_ask_chat_sleep_warmup():
    desc = LlmService(_settings()).describe()
    assert desc.info.service == "llm"
    assert desc.capabilities == ("qwen2.5:7b",)
    assert [a.id for a in desc.actions] == [
        "ask",
        "chat",
        "look_at_photo",
        "transcribe_voice",
        "stt_chunk",
        "synthesize_speech",
        "tts_chunk",
        "chat_progress",
        "sleep",
        "warmup",
    ]
    assert desc.find_action("ask").params[0].name == "prompt"
    assert desc.find_action("chat").params[0].name == "messages"
    quiet = desc.find_action("sleep").params[0]
    assert (quiet.name, quiet.type, quiet.required) == ("quiet", "bool", False)


async def test_get_state_includes_speech_therapy_snapshot():
    svc = LlmService(_settings())
    state = await svc.get_state()
    assert state["speech_therapy"] == {
        "error_probability": 1.0,
        "corrections_total": 0,
        "cured": False,
    }


async def test_ask_calls_ollama_generate_with_system_prompt(monkeypatch):
    calls = []

    async def fake_generate(cfg, prompt, system):
        calls.append((cfg.model, prompt, system))
        return {"response": "Здравствуйте, сэр"}

    monkeypatch.setattr(llm_service.ollama, "generate", fake_generate)
    # speech_rand=0.5: гарантированно ниже стартовой error_probability=1.0
    # (искажает), но выше вероятности визита логопеда 0.025 (без визита) —
    # иначе к ответу мог бы случайно (~5%) прилипнуть текст логопеда и
    # сломать точное сравнение ниже.
    svc = LlmService(_settings(), speech_rand=lambda: 0.5)
    result = await svc.run_command("ask", {"prompt": "Как погода?"})

    # Картавость — вероятностная механика «Логопед» (llm/speech_therapy.py),
    # не вывод модели как есть.
    assert result == {"response": "Здгавствуйте, сэг", "model": "qwen2.5:7b"}
    assert calls[0][0] == "qwen2.5:7b"
    assert calls[0][1] == "Как погода?"
    assert calls[0][2] == PERSONA  # системный промпт реально ушёл


async def test_ask_falls_back_to_default_persona_when_unconfigured(monkeypatch):
    # Живая находка 2026-07-25: текст персонажа убран из репозитория в
    # settings.llm.persona_prompt (локальный config.toml) — если он не
    # заполнен (свежий чекаут, CI), служба не должна слать Ollama пустую
    # строку системным промптом.
    calls = []

    async def fake_generate(cfg, prompt, system):
        calls.append(system)
        return {"response": "ответ"}

    monkeypatch.setattr(llm_service.ollama, "generate", fake_generate)
    svc = LlmService(_settings(persona_prompt=""))
    await svc.run_command("ask", {"prompt": "привет"})
    assert calls[0] == llm_service.DEFAULT_PERSONA_PROMPT


async def test_ask_rejects_missing_prompt():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command("ask", {})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_chat_calls_ollama_chat_and_extracts_message(monkeypatch):
    async def fake_chat(cfg, messages, system, tools=None, think=None):
        assert messages == [{"role": "user", "content": "привет"}]
        return {"message": {"role": "assistant", "content": "Добрый день"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    svc = LlmService(_settings(), speech_rand=lambda: 0.5)  # см. комментарий выше
    result = await svc.run_command("chat", {"messages": [{"role": "user", "content": "привет"}]})
    assert result == {"response": "Добгый день", "model": "qwen2.5:7b"}


async def test_chat_puts_speech_remark_in_own_field_not_in_response(monkeypatch):
    # Живой баг 2026-08-03: ремарка раньше дописывалась ПРЯМО в "response"
    # (см. llm/speech_therapy.py::process) — уезжала одним сообщением с
    # ответом и ломано отформатированной выше по стеку (bot/handlers/ai.py
    # экранирует ВЕСЬ текст персонажа как plain text). rand=0.0 —
    # наихудший случай, визит логопеда гарантирован.
    async def fake_chat(cfg, messages, system, tools=None, think=None):
        return {"message": {"role": "assistant", "content": "сэр"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    svc = LlmService(_settings(), speech_rand=lambda: 0.0)
    result = await svc.run_command("chat", {"messages": [{"role": "user", "content": "привет"}]})

    assert result["response"] == "сэг"  # без хвоста-ремарки
    assert result["speech_remark"] == "🗣 <b>Логопед:</b> <i>Не «сэг», а «сэр»!</i>"


async def test_chat_rejects_non_list_messages():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError):
        await svc.run_command("chat", {"messages": "не список"})
    with pytest.raises(ProtoError):
        await svc.run_command("chat", {"messages": []})


# --- think (вариативное рассуждение, LLM_INTEGRATION_PLAN.md §7 —
# bot/ai_flow.py теперь передаёт think явно на каждый вызов) ---


async def test_chat_passes_explicit_think_through_to_ollama(monkeypatch):
    seen = {}

    async def fake_chat(cfg, messages, system, tools=None, think=None):
        seen["think"] = think
        return {"message": {"content": "ответ"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    svc = LlmService(_settings())
    await svc.run_command("chat", {"messages": [{"role": "user", "content": "1"}], "think": True})
    assert seen["think"] is True

    await svc.run_command("chat", {"messages": [{"role": "user", "content": "1"}], "think": False})
    assert seen["think"] is False


async def test_chat_without_reason_defaults_to_off(monkeypatch):
    seen = {}

    async def fake_chat(cfg, messages, system, tools=None, think=None):
        seen["think"] = think
        return {"message": {"content": "ответ"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    svc = LlmService(_settings())  # qwen2.5:7b → профиль _default (think_control=flag)
    await svc.run_command("chat", {"messages": [{"role": "user", "content": "1"}]})
    assert seen["think"] is False  # нет намерения → быстрый проход


async def test_chat_reason_level_translated_via_profile(monkeypatch):
    seen = {}

    async def fake_chat(cfg, messages, system, tools=None, think=None):
        seen["think"] = think
        return {"message": {"content": "ответ"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    svc = LlmService(_settings())  # _default: think_control=flag → любой уровень >0 = True
    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "1"}], "reason": "medium"}
    )
    assert seen["think"] is True
    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "1"}], "reason": "off"}
    )
    assert seen["think"] is False


async def test_describe_carries_model_profile_summary():
    svc = LlmService(_settings())
    desc = svc.describe()
    assert desc.model_profile is not None
    assert desc.model_profile["router"] is True
    assert "num_ctx" in desc.model_profile


async def test_chat_rejects_non_bool_think():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(
            "chat", {"messages": [{"role": "user", "content": "1"}], "think": "да"}
        )
    assert excinfo.value.code == ERR_BAD_REQUEST


# --- role (живая находка 2026-07-25: триаж "думать ли"/"звать ли тул"
# вынесен в отдельный вызов с маленьким промптом без персонажа — см.
# llm/prompt.py::ROUTER_SYSTEM_PROMPT — чтобы не конкурировать за внимание
# модели с 12 правилами персонажа Альфреда) ---


async def test_chat_role_router_uses_router_prompt_not_persona(monkeypatch):
    seen = {}

    async def fake_chat(cfg, messages, system, tools=None, think=None):
        seen["system"] = system
        seen["messages"] = messages
        return {"message": {"content": "OK"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    svc = LlmService(_settings())
    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "1"}], "role": "router"}
    )
    # Общий с персонажным проходом префикс (2026-08-10): system[0] у роутера
    # ТОТ ЖЕ, что у персонажа, — иначе KV-кэш одного прохода не годится
    # другому. Сама инструкция триажа уезжает последним системным сообщением.
    assert seen["system"] == PERSONA
    assert seen["messages"][-1] == {
        "role": "system",
        "content": llm_service.ROUTER_SYSTEM_PROMPT,
    }
    assert seen["messages"][0] == {"role": "user", "content": "1"}


async def test_chat_role_absent_or_persona_uses_persona_prompt(monkeypatch):
    seen = []

    async def fake_chat(cfg, messages, system, tools=None, think=None):
        seen.append(system)
        return {"message": {"content": "ответ"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    svc = LlmService(_settings())
    await svc.run_command("chat", {"messages": [{"role": "user", "content": "1"}]})
    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "1"}], "role": "persona"}
    )
    assert seen == [PERSONA, PERSONA]


async def test_chat_rejects_unknown_role():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(
            "chat", {"messages": [{"role": "user", "content": "1"}], "role": "admin"}
        )
    assert excinfo.value.code == ERR_BAD_REQUEST


# --- фото (мультимодальный /ai, 2026-08-10 — ресайз/хранение на стороне
# этой службы, alfred шлёт raw_image как есть, см. llm/vision.py) ---


def _tiny_jpeg_b64() -> str:
    import base64
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (200, 30, 30)).save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


async def test_chat_with_raw_image_resizes_and_stores(monkeypatch):
    seen = {}

    async def fake_chat(cfg, messages, system, tools=None, think=None):
        seen["messages"] = messages
        return {"message": {"content": "вижу картинку"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    svc = LlmService(_settings(), speech_rand=lambda: 1.0)
    result = await svc.run_command(
        "chat",
        {
            "messages": [
                {"role": "user", "content": "что тут?", "raw_image": _tiny_jpeg_b64()}
            ],
            "photo_key": "chat1_msg1",
        },
    )
    assert result["response"] == "вижу картинку"
    sent = seen["messages"][-1]
    assert "raw_image" not in sent
    assert len(sent["images"]) == 1
    stored = svc._cfg.photos_dir / "chat1_msg1.jpg"
    assert stored.is_file()


async def test_chat_raw_image_requires_photo_key():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(
            "chat",
            {"messages": [{"role": "user", "content": "?", "raw_image": _tiny_jpeg_b64()}]},
        )
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_chat_raw_image_bad_base64_returns_apology_without_calling_ollama(monkeypatch):
    async def fake_chat(cfg, messages, system, tools=None, think=None):
        raise AssertionError("сломанное фото не должно доходить до ollama.chat")

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    svc = LlmService(_settings())
    result = await svc.run_command(
        "chat",
        {
            "messages": [
                {"role": "user", "content": "?", "raw_image": "не-base64!!!"}
            ],
            "photo_key": "chat1_msg2",
        },
    )
    assert result["response"] == llm_service.PHOTO_PROCESS_FAILED_TEXT


async def test_look_at_photo_loads_stored_and_returns_response(monkeypatch):
    seen = {}

    async def fake_chat(cfg, messages, system, tools=None, think=None):
        seen["messages"] = messages
        seen["tools"] = tools
        seen["think"] = think
        return {"message": {"content": "на фото круг"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    svc = LlmService(_settings(), speech_rand=lambda: 1.0)
    await svc.run_command(
        "chat",
        {
            "messages": [
                {"role": "user", "content": "что тут?", "raw_image": _tiny_jpeg_b64()}
            ],
            "photo_key": "chat1_msg1",
        },
    )

    result = await svc.run_command(
        "look_at_photo", {"photo_key": "chat1_msg1", "question": "какого цвета?"}
    )
    assert result["response"] == "на фото круг"
    assert seen["tools"] is None
    assert seen["think"] is False
    assert seen["messages"] == [
        {"role": "user", "content": "какого цвета?", "images": seen["messages"][0]["images"]}
    ]


async def test_look_at_photo_missing_file_returns_apology():
    svc = LlmService(_settings())
    result = await svc.run_command(
        "look_at_photo", {"photo_key": "нет-такого", "question": "что там?"}
    )
    assert result["response"] == llm_service.PHOTO_NOT_FOUND_TEXT


async def test_look_at_photo_rejects_missing_args():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError):
        await svc.run_command("look_at_photo", {"question": "что там?"})
    with pytest.raises(ProtoError):
        await svc.run_command("look_at_photo", {"photo_key": "x"})


# --- голосовые сообщения /ai (распознавание — faster-whisper на CPU,
# см. llm/stt.py; здесь stt.transcribe_voice замокан целиком — своя логика
# проверяется отдельно в test_llm_stt.py, здесь только обвязка протокола) ---


def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode()


def _sha256_hex(raw: bytes) -> str:
    import hashlib

    return hashlib.sha256(raw).hexdigest()


async def test_transcribe_voice_inline_calls_stt_and_returns_transcript(monkeypatch):
    seen = {}

    async def fake_transcribe(raw_bytes, cfg):
        seen["raw_bytes"] = raw_bytes
        return "привет альфред"

    monkeypatch.setattr(llm_service.stt, "transcribe_voice", fake_transcribe)
    svc = LlmService(_settings())
    result = await svc.run_command(
        "transcribe_voice", {"audio_b64": _b64(b"raw-ogg-bytes"), "chat_id": 1}
    )
    assert result == {"transcript": "привет альфред"}
    assert seen["raw_bytes"] == b"raw-ogg-bytes"


async def test_transcribe_voice_requires_audio_or_session():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command("transcribe_voice", {})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_transcribe_voice_rejects_bad_base64():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command("transcribe_voice", {"audio_b64": "не-base64!!!"})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_transcribe_voice_rejects_empty_audio():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command("transcribe_voice", {"audio_b64": _b64(b"")})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_transcribe_voice_swallows_stt_exception_returns_empty_transcript(monkeypatch):
    async def fake_transcribe(raw_bytes, cfg):
        raise RuntimeError("модель упала")

    monkeypatch.setattr(llm_service.stt, "transcribe_voice", fake_transcribe)
    svc = LlmService(_settings())
    result = await svc.run_command("transcribe_voice", {"audio_b64": _b64(b"x")})
    assert result == {"transcript": ""}


async def test_stt_upload_chunk_appends_and_returns_received_length():
    svc = LlmService(_settings())
    result = await svc.run_command(
        "stt_chunk", {"session_id": "s1", "offset": 0, "data_b64": _b64(b"part1")}
    )
    assert result == {"received": 5}
    result = await svc.run_command(
        "stt_chunk", {"session_id": "s1", "offset": 5, "data_b64": _b64(b"part2")}
    )
    assert result == {"received": 10}
    assert bytes(svc._stt_uploads["s1"]) == b"part1part2"


async def test_stt_upload_chunk_rejects_wrong_offset():
    svc = LlmService(_settings())
    await svc.run_command(
        "stt_chunk", {"session_id": "s1", "offset": 0, "data_b64": _b64(b"part1")}
    )
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(
            "stt_chunk", {"session_id": "s1", "offset": 999, "data_b64": _b64(b"x")}
        )
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_stt_upload_chunk_rejects_bad_base64():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError):
        await svc.run_command(
            "stt_chunk", {"session_id": "s1", "offset": 0, "data_b64": "не-base64!!!"}
        )


async def test_transcribe_voice_from_chunked_session_verifies_integrity(monkeypatch):
    seen = {}

    async def fake_transcribe(raw_bytes, cfg):
        seen["raw_bytes"] = raw_bytes
        return "длинное голосовое"

    monkeypatch.setattr(llm_service.stt, "transcribe_voice", fake_transcribe)
    svc = LlmService(_settings())
    raw = b"chast1chast2"
    await svc.run_command(
        "stt_chunk", {"session_id": "s1", "offset": 0, "data_b64": _b64(b"chast1")}
    )
    await svc.run_command(
        "stt_chunk", {"session_id": "s1", "offset": len(b"chast1"), "data_b64": _b64(b"chast2")}
    )
    result = await svc.run_command(
        "transcribe_voice",
        {
            "session_id": "s1",
            "expected_size": len(raw),
            "expected_sha256": _sha256_hex(raw),
        },
    )
    assert result == {"transcript": "длинное голосовое"}
    assert seen["raw_bytes"] == raw
    # Сессия одноразовая — использована и убрана из буфера.
    assert "s1" not in svc._stt_uploads


async def test_transcribe_voice_session_size_mismatch_rejected():
    svc = LlmService(_settings())
    await svc.run_command("stt_chunk", {"session_id": "s1", "offset": 0, "data_b64": _b64(b"abc")})
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(
            "transcribe_voice", {"session_id": "s1", "expected_size": 999}
        )
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_transcribe_voice_session_sha256_mismatch_rejected():
    svc = LlmService(_settings())
    await svc.run_command("stt_chunk", {"session_id": "s1", "offset": 0, "data_b64": _b64(b"abc")})
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(
            "transcribe_voice", {"session_id": "s1", "expected_sha256": "0" * 64}
        )
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_transcribe_voice_unknown_session_rejected():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command("transcribe_voice", {"session_id": "нет-такой"})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_sweep_stt_uploads_evicts_only_stale_sessions():
    svc = LlmService(_settings())
    now = datetime.now(tz=UTC)
    svc._stt_uploads["stale"] = bytearray(b"old")
    svc._stt_upload_touched["stale"] = now - timedelta(
        seconds=llm_service._STT_UPLOAD_TTL_S + 1
    )
    svc._stt_uploads["fresh"] = bytearray(b"new")
    svc._stt_upload_touched["fresh"] = now - timedelta(seconds=1)

    svc._sweep_stt_uploads()

    assert set(svc._stt_uploads) == {"fresh"}
    assert set(svc._stt_upload_touched) == {"fresh"}


# --- голосовые ОТВЕТЫ /ai (синтез — Coqui XTTS v2 на CPU, см. llm/tts.py;
# здесь tts.synthesize_speech замокан целиком — своя логика проверяется
# отдельно в test_llm_tts.py, здесь только обвязка протокола) ---


async def test_synthesize_speech_inline_calls_tts_and_returns_audio(monkeypatch):
    seen = {}

    async def fake_synthesize(text, cfg):
        seen["text"] = text
        return b"fake-ogg-bytes"

    monkeypatch.setattr(llm_service.tts, "synthesize_speech", fake_synthesize)
    svc = LlmService(_settings())
    result = await svc.run_command("synthesize_speech", {"text": "привет, сэр", "chat_id": 1})
    assert result == {"audio_b64": _b64(b"fake-ogg-bytes"), "format": "ogg"}
    assert seen["text"] == "привет, сэр"


async def test_synthesize_speech_rejects_empty_text():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command("synthesize_speech", {"text": "   "})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_synthesize_speech_truncates_long_text(monkeypatch):
    seen = {}

    async def fake_synthesize(text, cfg):
        seen["text"] = text
        return b"x"

    monkeypatch.setattr(llm_service.tts, "synthesize_speech", fake_synthesize)
    svc = LlmService(_settings(tts_max_text_chars=5))
    await svc.run_command("synthesize_speech", {"text": "0123456789"})
    assert seen["text"] == "01234"


async def test_synthesize_speech_wraps_exception_as_internal_proto_error(monkeypatch):
    async def fake_synthesize(text, cfg):
        raise RuntimeError("модель упала")

    monkeypatch.setattr(llm_service.tts, "synthesize_speech", fake_synthesize)
    svc = LlmService(_settings())
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command("synthesize_speech", {"text": "привет"})
    assert excinfo.value.code == ERR_INTERNAL


async def test_synthesize_speech_large_payload_returns_session(monkeypatch):
    payload = b"x" * (llm_service._INLINE_TTS_B64_BYTES + 1000)

    async def fake_synthesize(text, cfg):
        return payload

    monkeypatch.setattr(llm_service.tts, "synthesize_speech", fake_synthesize)
    svc = LlmService(_settings())
    result = await svc.run_command("synthesize_speech", {"text": "длинный ответ"})
    assert "audio_b64" not in result
    assert result["size"] == len(payload)
    assert result["sha256"] == _sha256_hex(payload)
    session_id = result["session_id"]
    assert svc._tts_sessions[session_id] == payload


async def test_tts_download_chunk_returns_data_and_eof(monkeypatch):
    async def fake_synthesize(text, cfg):
        return b"x" * (llm_service._INLINE_TTS_B64_BYTES + 1000)

    monkeypatch.setattr(llm_service.tts, "synthesize_speech", fake_synthesize)
    svc = LlmService(_settings())
    started = await svc.run_command("synthesize_speech", {"text": "текст"})
    session_id = started["session_id"]

    first = await svc.run_command(
        "tts_chunk", {"session_id": session_id, "offset": 0, "length": 500}
    )
    assert first["eof"] is False
    assert len(base64.b64decode(first["data_b64"])) == 500

    # Дочитываем остаток одним куском большего размера, чем осталось данных.
    rest = await svc.run_command(
        "tts_chunk", {"session_id": session_id, "offset": 500, "length": 10_000_000}
    )
    assert rest["eof"] is True
    # Сессия одноразовая — вычищена после eof.
    assert session_id not in svc._tts_sessions


async def test_tts_download_chunk_unknown_session_rejected():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command("tts_chunk", {"session_id": "нет-такой", "offset": 0})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_tts_download_chunk_rejects_negative_offset():
    svc = LlmService(_settings())
    svc._tts_sessions["s1"] = b"data"
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command("tts_chunk", {"session_id": "s1", "offset": -1})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_sweep_tts_sessions_evicts_only_stale_sessions():
    svc = LlmService(_settings())
    now = datetime.now(tz=UTC)
    svc._tts_sessions["stale"] = b"old"
    svc._tts_session_touched["stale"] = now - timedelta(
        seconds=llm_service._TTS_SESSION_TTL_S + 1
    )
    svc._tts_sessions["fresh"] = b"new"
    svc._tts_session_touched["fresh"] = now - timedelta(seconds=1)

    svc._sweep_tts_sessions()

    assert set(svc._tts_sessions) == {"fresh"}
    assert set(svc._tts_session_touched) == {"fresh"}


# --- request_id / chat_progress (этап 34, Фаза 2 — Rich-стрим ответов) ---


async def test_chat_with_request_id_goes_through_chat_stream_not_chat(monkeypatch):
    async def fake_chat(cfg, messages, system, tools=None, think=None):
        raise AssertionError("без request_id ожидался chat_stream, а не chat")

    async def fake_chat_stream(cfg, messages, system, on_chunk, tools=None, think=None):
        on_chunk("При")
        on_chunk("Привет")
        return {"message": {"content": "Привет"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    monkeypatch.setattr(llm_service.ollama, "chat_stream", fake_chat_stream)
    svc = LlmService(_settings(), speech_rand=lambda: 0.5)
    result = await svc.run_command(
        "chat",
        {"messages": [{"role": "user", "content": "1"}], "request_id": "req-1"},
    )
    assert result == {"response": "Пгивет", "model": "qwen2.5:7b"}


async def test_chat_stream_fills_streaming_buffer_and_marks_done(monkeypatch):
    seen_partials = []

    async def fake_chat_stream(cfg, messages, system, on_chunk, tools=None, think=None):
        on_chunk("Доб")
        seen_partials.append(svc._streaming["req-2"]["partial"])
        on_chunk("Добрый день")
        seen_partials.append(svc._streaming["req-2"]["partial"])
        assert svc._streaming["req-2"]["done"] is False  # ещё не завершили
        return {"message": {"content": "Добрый день"}}

    monkeypatch.setattr(llm_service.ollama, "chat_stream", fake_chat_stream)
    svc = LlmService(_settings())
    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "1"}], "request_id": "req-2"}
    )
    assert seen_partials == ["Доб", "Добрый день"]
    # После завершения запись остаётся (для последнего опроса бота), но
    # помечена done — см. также тест на подметание ниже.
    assert svc._streaming["req-2"]["done"] is True
    assert svc._streaming["req-2"]["done_at"] is not None


async def test_chat_stream_marks_done_even_on_ollama_failure(monkeypatch):
    # Опрашивающий бот не должен крутиться до собственного таймаута, если
    # Ollama упала посреди генерации — finally в _chat_streamed закрывает
    # запись независимо от исхода.
    async def fake_chat_stream(cfg, messages, system, on_chunk, tools=None, think=None):
        on_chunk("часть")
        raise RuntimeError("Ollama оборвалась")

    monkeypatch.setattr(llm_service.ollama, "chat_stream", fake_chat_stream)
    svc = LlmService(_settings())
    with pytest.raises(RuntimeError):
        await svc.run_command(
            "chat", {"messages": [{"role": "user", "content": "1"}], "request_id": "req-3"}
        )
    assert svc._streaming["req-3"] == {
        "partial": "часть",
        "done": True,
        "done_at": svc._streaming["req-3"]["done_at"],
    }
    assert svc._streaming["req-3"]["done_at"] is not None


async def test_chat_progress_reflects_current_buffer(monkeypatch):
    async def fake_chat_stream(cfg, messages, system, on_chunk, tools=None, think=None):
        on_chunk("текст")
        progress = await svc.run_command("chat_progress", {"request_id": "req-4"})
        assert progress == {"partial": "текст", "done": False}
        return {"message": {"content": "текст"}}

    monkeypatch.setattr(llm_service.ollama, "chat_stream", fake_chat_stream)
    svc = LlmService(_settings())
    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "1"}], "request_id": "req-4"}
    )
    final = await svc.run_command("chat_progress", {"request_id": "req-4"})
    assert final == {"partial": "текст", "done": True}


async def test_chat_progress_unknown_request_id_is_not_an_error():
    # Гонка первого опроса: бот уже спрашивает, а запись ещё не заведена
    # (или уже подметена, см. sweep-тест ниже) — не bad_request, просто пусто.
    svc = LlmService(_settings())
    result = await svc.run_command("chat_progress", {"request_id": "никогда-не-было"})
    assert result == {"partial": "", "done": False}


async def test_chat_progress_requires_request_id():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command("chat_progress", {})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_chat_rejects_non_string_request_id():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(
            "chat", {"messages": [{"role": "user", "content": "1"}], "request_id": 123}
        )
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_sweep_streaming_evicts_only_stale_done_entries():
    svc = LlmService(_settings())
    now = datetime.now(tz=UTC)
    svc._streaming["stale"] = {
        "partial": "старое",
        "done": True,
        "done_at": now - timedelta(seconds=llm_service._STREAMING_ENTRY_TTL_S + 1),
    }
    svc._streaming["fresh_done"] = {
        "partial": "недавнее",
        "done": True,
        "done_at": now - timedelta(seconds=1),
    }
    svc._streaming["still_running"] = {"partial": "идёт", "done": False, "done_at": None}

    svc._sweep_streaming()

    assert set(svc._streaming) == {"fresh_done", "still_running"}


async def test_sleep_action_stops_ollama_and_marks_asleep(monkeypatch):
    calls = []

    async def _stop(cfg):
        calls.append(cfg.model)

    monkeypatch.setattr(llm_service.ollama, "stop", _stop)
    svc = LlmService(_settings())
    result = await svc.run_command("sleep", {})
    assert result == {"asleep": True}
    assert calls == ["qwen2.5:7b"]
    assert (await svc.get_state())["asleep"] is True


async def test_ask_after_sleep_wakes_up_again(monkeypatch):
    async def fake_stop(cfg):
        pass

    async def fake_generate(cfg, prompt, system):
        return {"response": "ответ"}

    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    monkeypatch.setattr(llm_service.ollama, "generate", fake_generate)
    svc = LlmService(_settings())
    await svc.run_command("sleep", {})
    assert (await svc.get_state())["asleep"] is True

    await svc.run_command("ask", {"prompt": "привет"})
    assert (await svc.get_state())["asleep"] is False


async def test_warmup_also_preloads_model(monkeypatch):
    # Живая находка 2026-07-27: раньше прогрев только поднимал WSL/контейнер
    # (ensure_running), а модель оставалась выгруженной — "прогретая" служба
    # всё равно платила за её загрузку на первом реальном запросе, и
    # отложенная задача опаздывала на десятки секунд. Теперь прогрев тянет и
    # саму модель в память (ollama.preload).
    calls = []

    async def fake_ensure_running(cfg):
        calls.append(("ensure_running", cfg.model))

    async def fake_preload(cfg):
        calls.append(("preload", cfg.model))

    monkeypatch.setattr(llm_service.ollama, "ensure_running", fake_ensure_running)
    monkeypatch.setattr(llm_service.ollama, "preload", fake_preload)
    svc = LlmService(_settings())
    result = await svc.run_command("warmup", {})
    assert result == {"asleep": False}
    assert calls == [("ensure_running", "qwen2.5:7b"), ("preload", "qwen2.5:7b")]
    assert (await svc.get_state())["asleep"] is False


async def test_warmup_does_not_add_chat_id_to_active_chats(monkeypatch):
    # Живая находка (см. llm/service.py::run_command): прогрев — не реальный
    # чат, не должен раздувать список для EVENT_IDLE_SLEEP.
    async def fake_ensure_running(cfg):
        pass

    async def fake_preload(cfg):
        pass

    monkeypatch.setattr(llm_service.ollama, "ensure_running", fake_ensure_running)
    monkeypatch.setattr(llm_service.ollama, "preload", fake_preload)

    async def fake_stop(cfg):
        pass

    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    emitter = FakeEmitter()
    svc = LlmService(_settings(), emit=emitter)
    await svc.run_command("warmup", {})
    await svc.run_command("sleep", {})
    assert emitter.events == []  # ни одного llm_idle_sleep — не было chat_id


async def test_idle_check_sleeps_after_threshold(monkeypatch):
    stopped = []

    async def fake_stop(cfg):
        stopped.append(True)

    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    svc = LlmService(_settings(idle_sleep_after_s=60.0))
    # Живая находка при аудите механизма пробуждения (2026-08-26):
    # get_state()["asleep"] теперь учитывает и _warmup_confirmed (см.
    # LlmService.__init__/_touch) — тест эмулирует службу, которая реально
    # была активна недавно (а не свежесозданный, ни разу не тронутый
    # процесс), поэтому явно подтверждаем прогрев, как это сделал бы
    # настоящий _touch().
    svc._warmup_confirmed = True
    svc._last_activity = datetime.now(tz=UTC) - timedelta(seconds=61)

    await svc._maybe_sleep_idle()

    assert stopped == [True]
    assert (await svc.get_state())["asleep"] is True


async def test_idle_check_no_sleep_before_threshold(monkeypatch):
    stopped = []

    async def fake_stop(cfg):
        stopped.append(True)

    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    svc = LlmService(_settings(idle_sleep_after_s=60.0))
    # См. комментарий в test_idle_check_sleeps_after_threshold выше про
    # _warmup_confirmed.
    svc._warmup_confirmed = True
    svc._last_activity = datetime.now(tz=UTC) - timedelta(seconds=5)

    await svc._maybe_sleep_idle()

    assert stopped == []
    assert (await svc.get_state())["asleep"] is False


async def test_idle_check_is_noop_once_already_asleep(monkeypatch):
    calls = []

    async def fake_stop(cfg):
        calls.append(True)

    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    svc = LlmService(_settings(idle_sleep_after_s=60.0))
    svc._asleep = True
    svc._last_activity = datetime.now(tz=UTC) - timedelta(seconds=1000)

    await svc._maybe_sleep_idle()

    assert calls == []  # уже спит — второй docker stop не нужен


# --- chat_id tracking + llm_idle_sleep (живая находка 2026-07-23: закрытие
# диалога должно быть событийным — один раз на сон контейнера, только в
# реально спрашивавшие чаты — а не сканом БД по каждому диалогу отдельно) ---


async def test_chat_tracks_chat_id_for_idle_sleep_event(monkeypatch):
    async def fake_chat(cfg, messages, system, tools=None, think=None):
        return {"message": {"content": "ответ"}}

    async def fake_stop(cfg):
        pass

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    emitter = FakeEmitter()
    svc = LlmService(_settings(), emit=emitter)

    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "привет"}], "chat_id": 42}
    )
    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "снова"}], "chat_id": 7}
    )
    await svc.run_command("sleep", {})

    assert emitter.events == [("llm_idle_sleep", {"chat_ids": [7, 42]})]


async def test_quiet_sleep_says_nothing_and_forgets_chats(monkeypatch):
    """Штатный роспуск («ты свободен»): прощание уже сказано ботом, и
    llm_idle_sleep («не дождался обращения») противоречил бы ему. Список
    чатов при этом чистится — иначе останов процесса при выключении машины
    доложит тем же чатам llm_service_restart."""

    async def fake_chat(cfg, messages, system, tools=None, think=None):
        return {"message": {"content": "ответ"}}

    async def fake_stop(cfg):
        pass

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    emitter = FakeEmitter()
    svc = LlmService(_settings(), emit=emitter)

    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "спасибо, свободен"}], "chat_id": 42}
    )
    result = await svc.run_command("sleep", {"quiet": True})

    assert result == {"asleep": True}
    assert (await svc.get_state())["asleep"] is True
    assert emitter.events == []
    await svc.notify_restart()
    assert emitter.events == []  # и останов процесса следом — тоже молча


async def test_sleep_without_active_chats_emits_nothing(monkeypatch):
    async def fake_stop(cfg):
        pass

    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    emitter = FakeEmitter()
    svc = LlmService(_settings(), emit=emitter)

    await svc.run_command("sleep", {})

    assert emitter.events == []


async def test_idle_triggered_sleep_also_emits(monkeypatch):
    async def fake_generate(cfg, prompt, system):
        return {"response": "ответ"}

    async def fake_stop(cfg):
        pass

    monkeypatch.setattr(llm_service.ollama, "generate", fake_generate)
    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    emitter = FakeEmitter()
    svc = LlmService(_settings(idle_sleep_after_s=60.0), emit=emitter)

    await svc.run_command("ask", {"prompt": "привет", "chat_id": 1})
    svc._last_activity = datetime.now(tz=UTC) - timedelta(seconds=61)

    await svc._maybe_sleep_idle()

    assert emitter.events == [
        ("llm_idle_sleep", {"chat_ids": [1]}),
        ("llm_went_idle", {}),
    ]


async def test_idle_triggered_sleep_emits_went_idle_even_without_chats(monkeypatch):
    """Живая находка 2026-08-03 (обкатка автовыключения mycraft): warmup без
    единого реального обращения не рождает llm_idle_sleep вовсе (адресован
    чатам — см. test_warmup_does_not_add_chat_id_to_active_chats), но
    node/service.py::maybe_auto_poweroff_idle всё равно должен узнать, что
    простой наступил — иначе автовыключение никогда не сработало бы на
    машине, с которой Alfred просто ни разу не заговорили."""

    async def fake_stop(cfg):
        pass

    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    emitter = FakeEmitter()
    svc = LlmService(_settings(idle_sleep_after_s=60.0), emit=emitter)
    svc._last_activity = datetime.now(tz=UTC) - timedelta(seconds=61)

    await svc._maybe_sleep_idle()

    assert emitter.events == [("llm_went_idle", {})]


async def test_manual_sleep_does_not_emit_went_idle(monkeypatch):
    """`llm_went_idle` — только естественный тайм-аут (_maybe_sleep_idle),
    не ручной вызов действия sleep (роспуск через ai_flow.py или nodectl call)."""

    async def fake_stop(cfg):
        pass

    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    emitter = FakeEmitter()
    svc = LlmService(_settings(), emit=emitter)

    await svc.run_command("sleep", {})

    assert emitter.events == []


async def test_active_chat_ids_reset_after_emit(monkeypatch):
    async def fake_chat(cfg, messages, system, tools=None, think=None):
        return {"message": {"content": "ответ"}}

    async def fake_stop(cfg):
        pass

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    emitter = FakeEmitter()
    svc = LlmService(_settings(), emit=emitter)

    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "привет"}], "chat_id": 1}
    )
    await svc.run_command("sleep", {})
    await svc.run_command("sleep", {})  # второй сон подряд — новых чатов не было

    assert emitter.events == [("llm_idle_sleep", {"chat_ids": [1]})]


async def test_emit_failure_does_not_break_sleep(monkeypatch):
    async def fake_chat(cfg, messages, system, tools=None, think=None):
        return {"message": {"content": "ответ"}}

    async def fake_stop(cfg):
        pass

    async def broken_emit(event_type, data):
        raise RuntimeError("сеть моргнула")

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    svc = LlmService(_settings(), emit=broken_emit)

    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "привет"}], "chat_id": 1}
    )
    await svc.run_command("sleep", {})  # не должно бросить исключение

    assert (await svc.get_state())["asleep"] is True


# --- WSL keepalive живёт весь тёплый период, не один запрос (живая
# находка 2026-07-23: раньше держался только на время одного вызова в
# llm/ollama.py, и WSL гасла уже через секунды после ответа — задолго до
# idle_sleep_after_s) ---


class FakeKeepalive:
    def __init__(self, cfg, duration_s) -> None:
        self.duration_s = duration_s
        self._alive = False
        self.start_calls = 0
        self.stop_calls = 0

    @property
    def alive(self) -> bool:
        return self._alive

    async def start(self) -> None:
        self.start_calls += 1
        self._alive = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self._alive = False


def test_keepalive_duration_covers_idle_window(monkeypatch):
    monkeypatch.setattr(llm_service.ollama, "WslKeepalive", FakeKeepalive)
    svc = LlmService(_settings(idle_sleep_after_s=1800.0))
    assert svc._keepalive.duration_s == 1800.0 + 60.0  # запас поверх idle-порога


async def test_keepalive_started_on_first_activity_and_not_restarted(monkeypatch):
    async def fake_chat(cfg, messages, system, tools=None, think=None):
        return {"message": {"content": "ответ"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    monkeypatch.setattr(llm_service.ollama, "WslKeepalive", FakeKeepalive)
    svc = LlmService(_settings())

    await svc.run_command("chat", {"messages": [{"role": "user", "content": "1"}]})
    await svc.run_command("chat", {"messages": [{"role": "user", "content": "2"}]})

    assert svc._keepalive.start_calls == 1  # второй раз уже жив — не перезапускаем


async def test_keepalive_stopped_only_when_service_actually_sleeps(monkeypatch):
    async def fake_chat(cfg, messages, system, tools=None, think=None):
        return {"message": {"content": "ответ"}}

    async def fake_stop(cfg):
        pass

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    monkeypatch.setattr(llm_service.ollama, "WslKeepalive", FakeKeepalive)
    svc = LlmService(_settings())

    await svc.run_command("chat", {"messages": [{"role": "user", "content": "1"}]})
    assert svc._keepalive.alive is True

    await svc.run_command("sleep", {})

    assert svc._keepalive.alive is False
    assert svc._keepalive.stop_calls == 1


# --- notify_restart (перед остановом процесса, llm/app.py — известить
# активные чаты, что служба перезапускается, а не просто зависла) ---


async def test_notify_restart_emits_for_active_chats(monkeypatch):
    async def fake_chat(cfg, messages, system, tools=None, think=None):
        return {"message": {"content": "ответ"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    emitter = FakeEmitter()
    svc = LlmService(_settings(), emit=emitter)

    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "привет"}], "chat_id": 42}
    )
    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "снова"}], "chat_id": 7}
    )
    await svc.notify_restart()

    assert emitter.events == [("llm_service_restart", {"chat_ids": [7, 42]})]


async def test_notify_restart_without_active_chats_emits_nothing():
    emitter = FakeEmitter()
    svc = LlmService(_settings(), emit=emitter)

    await svc.notify_restart()

    assert emitter.events == []


async def test_notify_restart_failure_is_swallowed(monkeypatch):
    async def fake_chat(cfg, messages, system, tools=None, think=None):
        return {"message": {"content": "ответ"}}

    async def failing_emit(event_type, data):
        raise RuntimeError("boom")

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    svc = LlmService(_settings(), emit=failing_emit)

    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "привет"}], "chat_id": 1}
    )
    await svc.notify_restart()  # не должно бросить исключение наружу
