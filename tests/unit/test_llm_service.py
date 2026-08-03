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
    assert [a.id for a in desc.actions] == ["ask", "chat", "sleep", "warmup"]
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
    assert result["speech_remark"] == '🗣 <i>Логопед:</i> не «сэг», а «сэр»!'


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


async def test_chat_without_think_arg_defers_to_ollama_default(monkeypatch):
    seen = {}

    async def fake_chat(cfg, messages, system, tools=None, think=None):
        seen["think"] = think
        return {"message": {"content": "ответ"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    svc = LlmService(_settings())
    await svc.run_command("chat", {"messages": [{"role": "user", "content": "1"}]})
    assert seen["think"] is None  # ollama.chat сам подставит cfg.think_chat


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
        return {"message": {"content": "OK"}}

    monkeypatch.setattr(llm_service.ollama, "chat", fake_chat)
    svc = LlmService(_settings())
    await svc.run_command(
        "chat", {"messages": [{"role": "user", "content": "1"}], "role": "router"}
    )
    assert seen["system"] == llm_service.ROUTER_SYSTEM_PROMPT
    assert seen["system"] != PERSONA


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
