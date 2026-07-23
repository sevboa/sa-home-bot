"""llm/ollama.py: прогрев WSL/контейнера, generate/chat, стоп.

`ollama_version`/`_post_json_sync` — реальные HTTP-вызовы к loopback, здесь
не выполняются; monkeypatch подменяет их на уровне модуля, как _run_systemctl
в test_apps_service.py."""

from __future__ import annotations

import pytest

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm import ollama
from sa_home_bot.proto.messages import ERR_INTERNAL, ProtoError


def _cfg(**overrides) -> LlmConfig:
    return LlmConfig(**overrides)


@pytest.fixture(autouse=True)
def fast_warmup(monkeypatch):
    # Не ждать реальные секунды между попытками прогрева.
    monkeypatch.setattr(ollama, "_WARMUP_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(ollama, "_WARMUP_TIMEOUT_S", 0.03)


class FakeProc:
    def __init__(self, returncode: int, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr


async def test_wsl_exec_success(monkeypatch):
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return FakeProc(0)

    monkeypatch.setattr(ollama.asyncio, "create_subprocess_exec", fake_exec)
    ok = await ollama._wsl_exec(_cfg(wsl_distro="Docker"), "docker", "start", "ollama")
    assert ok is True
    assert calls[0] == ("wsl", "-d", "Docker", "-u", "root", "--", "docker", "start", "ollama")


async def test_wsl_exec_nonzero_returncode_is_false(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return FakeProc(1, b"error: distro not found")

    monkeypatch.setattr(ollama.asyncio, "create_subprocess_exec", fake_exec)
    assert await ollama._wsl_exec(_cfg(), "true") is False


async def test_wsl_exec_missing_binary_is_false(monkeypatch):
    async def fake_exec(*args, **kwargs):
        raise OSError("wsl.exe not found")

    monkeypatch.setattr(ollama.asyncio, "create_subprocess_exec", fake_exec)
    assert await ollama._wsl_exec(_cfg(), "true") is False


async def test_ensure_running_skips_warmup_when_already_up(monkeypatch):
    calls = []

    async def fake_version(cfg, timeout=None):
        return {"version": "0.32.1"}

    async def fake_wsl_exec(cfg, *args):
        calls.append(args)
        return True

    monkeypatch.setattr(ollama, "ollama_version", fake_version)
    monkeypatch.setattr(ollama, "_wsl_exec", fake_wsl_exec)
    await ollama.ensure_running(_cfg())
    assert calls == []  # уже отвечает — прогрев не нужен


async def test_ensure_running_warms_up_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    async def fake_version(cfg, timeout=None):
        attempts["n"] += 1
        return {"version": "0.32.1"} if attempts["n"] >= 3 else None

    wsl_calls = []

    async def fake_wsl_exec(cfg, *args):
        wsl_calls.append(args)
        return True

    monkeypatch.setattr(ollama, "ollama_version", fake_version)
    monkeypatch.setattr(ollama, "_wsl_exec", fake_wsl_exec)
    await ollama.ensure_running(_cfg(ollama_container="ollama"))

    assert ("true",) in wsl_calls
    assert ("docker", "start", "ollama") in wsl_calls
    assert attempts["n"] >= 3


async def test_ensure_running_gives_up_after_timeout(monkeypatch):
    async def fake_version(cfg, timeout=None):
        return None  # никогда не поднимается

    async def fake_wsl_exec(cfg, *args):
        return False

    monkeypatch.setattr(ollama, "ollama_version", fake_version)
    monkeypatch.setattr(ollama, "_wsl_exec", fake_wsl_exec)

    with pytest.raises(ProtoError) as excinfo:
        await ollama.ensure_running(_cfg())
    assert excinfo.value.code == ERR_INTERNAL


async def test_stop_calls_docker_stop(monkeypatch):
    calls = []

    async def fake_wsl_exec(cfg, *args):
        calls.append(args)
        return True

    monkeypatch.setattr(ollama, "_wsl_exec", fake_wsl_exec)
    await ollama.stop(_cfg(ollama_container="ollama"))
    assert calls == [("docker", "stop", "ollama")]


async def test_generate_warms_up_then_posts(monkeypatch):
    monkeypatch.setattr(ollama, "ensure_running", _noop)
    posted = {}

    def fake_post(url, payload, timeout):
        posted["url"] = url
        posted["payload"] = payload
        return {"response": "Здравствуйте"}

    monkeypatch.setattr(ollama, "_post_json_sync", fake_post)
    result = await ollama.generate(_cfg(model="qwen2.5:7b"), "привет", "system-prompt")

    assert result == {"response": "Здравствуйте"}
    assert posted["url"].endswith("/api/generate")
    assert posted["payload"]["model"] == "qwen2.5:7b"
    assert posted["payload"]["system"] == "system-prompt"
    assert posted["payload"]["think"] is False


async def test_chat_prepends_system_message(monkeypatch):
    monkeypatch.setattr(ollama, "ensure_running", _noop)
    posted = {}

    def fake_post(url, payload, timeout):
        posted["payload"] = payload
        return {"message": {"content": "Добгый день"}}

    monkeypatch.setattr(ollama, "_post_json_sync", fake_post)
    result = await ollama.chat(
        _cfg(), [{"role": "user", "content": "привет"}], "system-prompt"
    )

    assert result == {"message": {"content": "Добгый день"}}
    assert posted["payload"]["messages"][0] == {"role": "system", "content": "system-prompt"}
    assert posted["payload"]["messages"][1] == {"role": "user", "content": "привет"}


async def test_generate_wraps_http_error(monkeypatch):
    monkeypatch.setattr(ollama, "ensure_running", _noop)

    def fake_post(url, payload, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(ollama, "_post_json_sync", fake_post)
    with pytest.raises(ProtoError) as excinfo:
        await ollama.generate(_cfg(), "привет", "system")
    assert excinfo.value.code == ERR_INTERNAL


async def _noop(cfg):
    pass
