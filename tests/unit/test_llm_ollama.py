"""llm/ollama.py: прогрев WSL/контейнера, generate/chat, стоп.

`ollama_version`/`_post_json_sync` — реальные HTTP-вызовы к loopback, здесь
не выполняются; monkeypatch подменяет их на уровне модуля, как _run_systemctl
в test_apps_service.py."""

from __future__ import annotations

import io
import urllib.error

import pytest

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm import ollama
from sa_home_bot.proto.messages import ERR_INTERNAL, ProtoError


def _cfg(**overrides) -> LlmConfig:
    return LlmConfig(**overrides)


@pytest.fixture(autouse=True)
def fast_warmup(monkeypatch):
    # Не ждать реальные секунды между попытками прогрева/ретрая.
    monkeypatch.setattr(ollama, "_WARMUP_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(ollama, "_WARMUP_TIMEOUT_S", 0.03)
    monkeypatch.setattr(ollama, "_POST_RETRY_DELAY_S", 0.01)


# --- _post_json_sync: тело HTTP-ошибки ---


def test_post_json_sync_includes_json_error_body_in_http_error(monkeypatch):
    # Живой сбой 2026-08-04: str(HTTPError) по умолчанию — голое "HTTP Error
    # 400: Bad Request", а тело ответа Ollama (реальная причина отказа)
    # молча терялось — разбор сводился к логам трёх машин. Тело должно
    # попасть в сообщение исключения.
    body = b'{"error": "messages: assistant tool_calls without a tool response"}'

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:11434/api/chat", 400, "Bad Request", None, io.BytesIO(body)
        )

    monkeypatch.setattr(ollama.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        ollama._post_json_sync("http://127.0.0.1:11434/api/chat", {"messages": []}, 5.0)

    assert "messages: assistant tool_calls without a tool response" in str(excinfo.value)


def test_post_json_sync_includes_plain_text_error_body_in_http_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:11434/api/chat",
            500,
            "Internal Server Error",
            None,
            io.BytesIO(b"panic: nil pointer"),
        )

    monkeypatch.setattr(ollama.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        ollama._post_json_sync("http://127.0.0.1:11434/api/chat", {"messages": []}, 5.0)

    assert "panic: nil pointer" in str(excinfo.value)


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


async def test_chat_think_none_omits_key_from_payload(monkeypatch):
    # Живая находка 2026-07-25: раньше think=None подставлял cfg.think_chat
    # как дефолт — решение пользователя изменило семантику, потому что
    # принудительный think=false на новых моделях (qwen3.5/3.6, свой
    # renderer/parser) давал галлюцинации. Теперь think=None значит "не
    # слать поле think вообще" — пусть модель решает сама, cfg.think_chat
    # этот путь больше не подставляет (используется явно только в
    # bot/tools.py::tool_remind для отложенных задач).
    monkeypatch.setattr(ollama, "ensure_running", _noop)
    posted = {}

    def fake_post(url, payload, timeout):
        posted["payload"] = payload
        return {"message": {"content": "ответ"}}

    monkeypatch.setattr(ollama, "_post_json_sync", fake_post)
    await ollama.chat(_cfg(think_chat=True), [{"role": "user", "content": "1"}], "system")
    assert "think" not in posted["payload"]

    await ollama.chat(_cfg(think_chat=False), [{"role": "user", "content": "1"}], "system")
    assert "think" not in posted["payload"]


async def test_chat_explicit_think_overrides_config_default(monkeypatch):
    monkeypatch.setattr(ollama, "ensure_running", _noop)
    posted = {}

    def fake_post(url, payload, timeout):
        posted["payload"] = payload
        return {"message": {"content": "ответ"}}

    monkeypatch.setattr(ollama, "_post_json_sync", fake_post)
    await ollama.chat(
        _cfg(think_chat=True), [{"role": "user", "content": "1"}], "system", think=False
    )
    assert posted["payload"]["think"] is False

    await ollama.chat(
        _cfg(think_chat=False), [{"role": "user", "content": "1"}], "system", think=True
    )
    assert posted["payload"]["think"] is True


async def test_generate_wraps_http_error(monkeypatch):
    monkeypatch.setattr(ollama, "ensure_running", _noop)

    def fake_post(url, payload, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(ollama, "_post_json_sync", fake_post)
    with pytest.raises(ProtoError) as excinfo:
        await ollama.generate(_cfg(), "привет", "system")
    assert excinfo.value.code == ERR_INTERNAL


async def test_chat_retries_once_after_transient_connection_error(monkeypatch):
    # Живая находка: сразу после холодного старта контейнера первый POST
    # иногда обрывается, хотя /api/version уже отвечал — один ретрай.
    monkeypatch.setattr(ollama, "ensure_running", _noop)
    calls = {"n": 0}

    def fake_post(url, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("Remote end closed connection without response")
        return {"message": {"content": "Добгый день"}}

    monkeypatch.setattr(ollama, "_post_json_sync", fake_post)
    result = await ollama.chat(_cfg(), [{"role": "user", "content": "привет"}], "system")

    assert result == {"message": {"content": "Добгый день"}}
    assert calls["n"] == 2


async def test_chat_gives_up_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(ollama, "ensure_running", _noop)
    calls = {"n": 0}

    def fake_post(url, payload, timeout):
        calls["n"] += 1
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(ollama, "_post_json_sync", fake_post)
    with pytest.raises(ProtoError) as excinfo:
        await ollama.chat(_cfg(), [{"role": "user", "content": "привет"}], "system")

    assert excinfo.value.code == ERR_INTERNAL
    assert calls["n"] == ollama._POST_RETRY_ATTEMPTS  # не бесконечный ретрай


async def test_chat_does_not_retry_real_http_error_response(monkeypatch):
    # HTTPError — реальный ответ сервера (например, модель не найдена) —
    # повторный точно такой же запрос даст тот же результат, ретрай бессмыслен.
    monkeypatch.setattr(ollama, "ensure_running", _noop)
    calls = {"n": 0}

    def fake_post(url, payload, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 404, "model not found", None, None)

    monkeypatch.setattr(ollama, "_post_json_sync", fake_post)
    with pytest.raises(ProtoError) as excinfo:
        await ollama.chat(_cfg(), [{"role": "user", "content": "привет"}], "system")

    assert excinfo.value.code == ERR_INTERNAL
    assert calls["n"] == 1  # ни одного ретрая


class FakeKeepaliveProc:
    def __init__(self) -> None:
        self.terminated = False
        self.waited = False
        self.returncode = None  # None = ещё выполняется, как настоящий Process

    def terminate(self) -> None:
        self.terminated = True

    async def wait(self) -> int:
        self.waited = True
        self.returncode = 0
        return 0


async def test_wsl_keepalive_start_spawns_long_sleep(monkeypatch):
    fake_proc = FakeKeepaliveProc()
    spawn_calls = []

    async def fake_exec(*args, **kwargs):
        spawn_calls.append(args)
        return fake_proc

    monkeypatch.setattr(ollama.asyncio, "create_subprocess_exec", fake_exec)
    keepalive = ollama.WslKeepalive(_cfg(wsl_distro="Docker"), duration_s=1860.0)

    assert keepalive.alive is False
    await keepalive.start()

    assert spawn_calls[0][:6] == ("wsl", "-d", "Docker", "-u", "root", "--")
    assert spawn_calls[0][6:8] == ("sleep", "1860")
    assert keepalive.alive is True


async def test_wsl_keepalive_stop_terminates_process(monkeypatch):
    fake_proc = FakeKeepaliveProc()

    async def fake_exec(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(ollama.asyncio, "create_subprocess_exec", fake_exec)
    keepalive = ollama.WslKeepalive(_cfg(), duration_s=60.0)
    await keepalive.start()

    await keepalive.stop()

    assert fake_proc.terminated is True
    assert fake_proc.waited is True
    assert keepalive.alive is False


async def test_wsl_keepalive_spawn_failure_leaves_it_not_alive(monkeypatch):
    # Не удалось поднять keepalive (например, самого wsl.exe нет) — не
    # повод валить вызывающий код, просто остаётся "не живым".
    async def fake_exec(*args, **kwargs):
        raise OSError("wsl.exe not found")

    monkeypatch.setattr(ollama.asyncio, "create_subprocess_exec", fake_exec)
    keepalive = ollama.WslKeepalive(_cfg(), duration_s=60.0)

    await keepalive.start()  # не бросает

    assert keepalive.alive is False


async def test_wsl_keepalive_stop_without_start_is_noop():
    keepalive = ollama.WslKeepalive(_cfg(), duration_s=60.0)
    await keepalive.stop()  # не бросает, даже если start() не звали


async def _noop(cfg):
    pass
