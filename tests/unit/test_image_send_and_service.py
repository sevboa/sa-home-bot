"""Этап 48, обвязка вокруг картинок: Notifier.send_photo_ex (байты или
file_id → (message_id, file_id)) и ветка generate_image службы llm
(сам генератор замокан — diffusers в dev-окружении нет)."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile

from sa_home_bot.bot import notifier as notifier_module
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.llm import service as llm_service
from sa_home_bot.llm.service import LlmService
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ERR_INTERNAL, ProtoError


@pytest.fixture(autouse=True)
def _fast_and_isolated(tmp_path, monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(notifier_module.asyncio, "sleep", _no_sleep)
    # см. test_llm_service.py: относительный путь состояния Логопеда
    monkeypatch.chdir(tmp_path)


# --- Notifier.send_photo_ex ---


class PhotoBot:
    def __init__(self, *, photo_sizes=("small", "big"), fail: Exception | None = None,
                 fail_times: int = 0) -> None:
        self.photo_sizes = photo_sizes
        self.fail = fail
        self.fail_times = fail_times
        self.calls: list[dict] = []

    async def send_photo(self, chat_id, photo, *, caption=None, message_thread_id=None,
                         reply_parameters=None, has_spoiler=False):
        self.calls.append({
            "chat_id": chat_id, "photo": photo, "caption": caption,
            "thread": message_thread_id, "reply": reply_parameters,
        })
        if self.fail is not None and self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail
        return SimpleNamespace(
            message_id=77,
            photo=[SimpleNamespace(file_id=f) for f in self.photo_sizes],
        )


async def test_send_photo_ex_bytes_returns_message_id_and_largest_file_id():
    bot = PhotoBot()
    result = await Notifier(bot).send_photo_ex(
        5, b"png-bytes", caption="Кот", message_thread_id=3, reply_to_message_id=9
    )
    assert result == (77, "big")
    [call] = bot.calls
    assert isinstance(call["photo"], BufferedInputFile)
    assert call["caption"] == "Кот" and call["thread"] == 3
    assert call["reply"].message_id == 9
    assert call["reply"].allow_sending_without_reply is True


async def test_send_photo_ex_file_id_is_passed_as_is_without_reply():
    bot = PhotoBot()
    assert await Notifier(bot).send_photo_ex(5, "tg-file") == (77, "big")
    assert bot.calls[0]["photo"] == "tg-file"
    assert bot.calls[0]["reply"] is None


async def test_send_photo_ex_retries_transient_error():
    bot = PhotoBot(fail=ConnectionError("proxy timed out"), fail_times=1)
    assert await Notifier(bot).send_photo_ex(5, b"x") == (77, "big")
    assert len(bot.calls) == 2


async def test_send_photo_ex_permanent_error_is_none():
    bot = PhotoBot(
        fail=TelegramBadRequest(method=None, message="Bad Request: wrong file identifier"),
        fail_times=99,
    )
    assert await Notifier(bot).send_photo_ex(5, "stale-file-id") is None
    assert len(bot.calls) == 1


async def test_send_photo_ex_message_without_photo_is_none():
    assert await Notifier(PhotoBot(photo_sizes=())).send_photo_ex(5, b"x") is None


# --- служба llm: generate_image ---


def _svc(**llm) -> LlmService:
    llm.setdefault("idle_sleep_after_s", 1800.0)
    return LlmService(Settings(llm=LlmConfig(model="qwen2.5:7b", **llm)))


async def test_generate_image_disabled_is_bad_request(monkeypatch):
    called = []

    async def fake_generate(prompt, negative, cfg):
        called.append(prompt)
        return {}

    monkeypatch.setattr(llm_service.imagegen, "generate_image", fake_generate)
    with pytest.raises(ProtoError) as excinfo:
        await _svc().run_command("generate_image", {"prompt": "a cat"})
    assert excinfo.value.code == ERR_BAD_REQUEST
    assert called == []


async def test_generate_image_returns_png_b64(monkeypatch):
    seen = {}

    async def fake_generate(prompt, negative, cfg):
        seen.update(prompt=prompt, negative=negative)
        return {"png": b"\x89PNG-fake", "width": 128, "height": 96, "seconds": 12.345}

    monkeypatch.setattr(llm_service.imagegen, "generate_image", fake_generate)
    result = await _svc(imagegen_enabled=True).run_command(
        "generate_image", {"prompt": "  a cat  ", "negative": "text", "chat_id": 1}
    )
    assert result == {
        "png_b64": base64.b64encode(b"\x89PNG-fake").decode(),
        "width": 128,
        "height": 96,
        "seconds": 12.3,
        "prompt": "a cat",
        "prompt_seconds": 0.0,
    }
    assert seen == {"prompt": "a cat", "negative": "text"}


async def test_generate_image_empty_negative_uses_config_default(monkeypatch):
    seen = {}

    async def fake_generate(prompt, negative, cfg):
        seen["negative"] = negative
        return {"png": b"x", "width": 1, "height": 1, "seconds": 0.0}

    monkeypatch.setattr(llm_service.imagegen, "generate_image", fake_generate)
    svc = _svc(imagegen_enabled=True, imagegen_negative="blurry")
    await svc.run_command("generate_image", {"prompt": "a cat", "negative": " "})
    assert seen["negative"] == "blurry"


async def test_generate_image_empty_prompt_is_bad_request():
    with pytest.raises(ProtoError) as excinfo:
        await _svc(imagegen_enabled=True).run_command("generate_image", {"prompt": "  "})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_generate_image_generator_failure_is_internal(monkeypatch):
    async def boom(prompt, negative, cfg):
        raise RuntimeError("OOM")

    monkeypatch.setattr(llm_service.imagegen, "generate_image", boom)
    with pytest.raises(ProtoError) as excinfo:
        await _svc(imagegen_enabled=True).run_command("generate_image", {"prompt": "a cat"})
    assert excinfo.value.code == ERR_INTERNAL


def test_generate_sync_passes_guidance_from_config():
    from sa_home_bot.llm import imagegen

    seen = {}

    class _Pipe:
        def __call__(self, prompt, **kw):
            seen.update(kw)

            class _R:
                images = ["img"]

            return _R()

    cfg = LlmConfig(model="qwen2.5:7b", imagegen_guidance=1.5, imagegen_steps=6)
    assert imagegen._generate_sync(_Pipe(), "a cat", "", cfg) == "img"
    assert seen["guidance_scale"] == 1.5
    assert seen["num_inference_steps"] == 6
    assert seen["negative_prompt"] is None


async def test_generate_image_description_goes_through_prompt_agent(monkeypatch):
    seen = {}

    async def fake_compose(description, cfg, think=None):
        seen["description"] = description
        return "red dragon, old castle", "people"

    async def fake_generate(prompt, negative, cfg):
        seen.update(prompt=prompt, negative=negative)
        return {"png": b"x", "width": 1, "height": 1, "seconds": 1.0, "prompt": prompt}

    monkeypatch.setattr(llm_service.image_prompt, "compose", fake_compose)
    monkeypatch.setattr(llm_service.imagegen, "generate_image", fake_generate)
    result = await _svc(imagegen_enabled=True).run_command(
        "generate_image", {"description": " дракон над замком ", "chat_id": 1}
    )
    assert seen == {
        "description": "дракон над замком",
        "prompt": "red dragon, old castle",
        "negative": "people",
    }
    assert result["prompt"] == "red dragon, old castle"


async def test_generate_image_prompt_agent_off_strips_style(monkeypatch):
    seen = {}

    async def fake_generate(prompt, negative, cfg):
        seen["prompt"] = prompt
        return {"png": b"x", "width": 1, "height": 1, "seconds": 1.0}

    monkeypatch.setattr(llm_service.imagegen, "generate_image", fake_generate)
    svc = _svc(imagegen_enabled=True, imagegen_prompt_agent=False)
    await svc.run_command(
        "generate_image", {"description": "a red dragon, castle, cinematic lighting, 8k"}
    )
    assert seen["prompt"] == "a red dragon, castle"
