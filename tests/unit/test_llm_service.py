"""Служба llm (Альфред): describe, ask/chat/sleep, идле-таймер.

Ollama/WSL не трогаем (monkeypatch sa_home_bot.llm.service.ollama) — это
чистая loopback-обвязка, ей место в отдельном тесте llm/ollama.py, а не здесь.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.llm import service as llm_service
from sa_home_bot.llm.service import LlmService
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError


def _settings(**overrides) -> Settings:
    overrides.setdefault("idle_sleep_after_s", 1800.0)
    return Settings(llm=LlmConfig(model="qwen2.5:7b", **overrides))


class FakeEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


def test_describe_declares_ask_chat_sleep():
    desc = LlmService(_settings()).describe()
    assert desc.info.service == "llm"
    assert desc.capabilities == ("qwen2.5:7b",)
    assert [a.id for a in desc.actions] == ["ask", "chat", "sleep"]
    assert desc.find_action("ask").params[0].name == "prompt"
    assert desc.find_action("chat").params[0].name == "messages"
    assert desc.find_action("sleep").params == ()


async def test_ask_calls_ollama_generate_with_system_prompt(monkeypatch):
    calls = []

    async def fake_generate(cfg, prompt, system):
        calls.append((cfg.model, prompt, system))
        return {"response": "Здравствуйте, сэ"}

    monkeypatch.setattr(llm_service.ollama, "generate", fake_generate)
    svc = LlmService(_settings())
    result = await svc.run_command("ask", {"prompt": "Как погода?"})

    assert result == {"response": "Здравствуйте, сэ", "model": "qwen2.5:7b"}
    assert calls[0][0] == "qwen2.5:7b"
    assert calls[0][1] == "Как погода?"
    assert "р" in calls[0][2].lower()  # системный промпт реально ушёл


async def test_ask_rejects_missing_prompt():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command("ask", {})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_chat_calls_ollama_chat_and_extracts_message(monkeypatch):
    async def fake_chat(cfg, messages, system):
        assert messages == [{"role": "user", "content": "привет"}]
        return {"message": {"role": "assistant", "content": "Добгый день"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    svc = LlmService(_settings())
    result = await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "привет"}]}
    )
    assert result == {"response": "Добгый день", "model": "qwen2.5:7b"}


async def test_chat_rejects_non_list_messages():
    svc = LlmService(_settings())
    with pytest.raises(ProtoError):
        await svc.run_command("chat", {"messages": "не список"})
    with pytest.raises(ProtoError):
        await svc.run_command("chat", {"messages": []})


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


async def test_idle_check_sleeps_after_threshold(monkeypatch):
    stopped = []

    async def fake_stop(cfg):
        stopped.append(True)

    monkeypatch.setattr(llm_service.ollama, "stop", fake_stop)
    svc = LlmService(_settings(idle_sleep_after_s=60.0))
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
    async def fake_chat(cfg, messages, system):
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

    assert emitter.events == [("llm_idle_sleep", {"chat_ids": [1]})]


async def test_active_chat_ids_reset_after_emit(monkeypatch):
    async def fake_chat(cfg, messages, system):
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
    async def fake_chat(cfg, messages, system):
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
    async def fake_chat(cfg, messages, system):
        return {"message": {"content": "ответ"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    monkeypatch.setattr(llm_service.ollama, "WslKeepalive", FakeKeepalive)
    svc = LlmService(_settings())

    await svc.run_command("chat", {"messages": [{"role": "user", "content": "1"}]})
    await svc.run_command("chat", {"messages": [{"role": "user", "content": "2"}]})

    assert svc._keepalive.start_calls == 1  # второй раз уже жив — не перезапускаем


async def test_keepalive_stopped_only_when_service_actually_sleeps(monkeypatch):
    async def fake_chat(cfg, messages, system):
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
