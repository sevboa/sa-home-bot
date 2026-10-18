from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sa_home_bot.bot import tools
from sa_home_bot.bot.tools import remind
from sa_home_bot.config import (
    LlmConfig,
    Settings,
)
from sa_home_bot.llm import prompt
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.tasks import protocol as task_protocol
from tests.unit.tools_support import (
    CHAT_ID,
    FakeNodeLink,
)
from tests.unit.tools_support import (
    make_ctx as _ctx,
)

# --- _close_pending_tool_calls: снимок для remind из середины раунда ---


def test_close_pending_tool_calls_returns_unchanged_when_last_is_not_tool_calls():
    history = [{"role": "user", "content": "привет"}]
    assert remind._close_pending_tool_calls(history) == history


def test_close_pending_tool_calls_fills_generic_placeholder_without_self_result():
    history = [
        {"role": "user", "content": "обнови ноды"},
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "node_manage", "arguments": {}}}],
        },
    ]
    snapshot = remind._close_pending_tool_calls(history)
    assert snapshot[-1] == {
        "role": "tool",
        "content": "(результат не сохранён)",
        "name": "node_manage",
    }
    assert history[-1].get("role") != "tool"  # живой history не тронут


def test_close_pending_tool_calls_uses_self_result_for_pending_call():
    # Живой баг 2026-08-05 (часть 2): node_manage зовёт remind изнутри
    # своего же handler'а — его СОБСТВЕННЫЙ результат должен попасть в
    # снимок, а не заглушка, иначе разбуженная модель не знает, что update
    # реально удался.
    history = [
        {"role": "user", "content": "обнови ноды"},
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "node_manage", "arguments": {}}}],
        },
    ]
    snapshot = remind._close_pending_tool_calls(history, self_result="Обновление поставлено")
    assert snapshot[-1] == {
        "role": "tool",
        "content": "Обновление поставлено",
        "name": "node_manage",
    }


def test_close_pending_tool_calls_self_result_only_for_first_of_several():
    history = [
        {"role": "user", "content": "обнови ноды"},
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "node_manage", "arguments": {"node": "arch-t480"}}},
                {"function": {"name": "node_manage", "arguments": {"node": "jeeves"}}},
            ],
        },
    ]
    snapshot = remind._close_pending_tool_calls(history, self_result="готово для arch-t480")
    tail = snapshot[-2:]
    assert tail[0] == {"role": "tool", "content": "готово для arch-t480", "name": "node_manage"}
    assert tail[1] == {"role": "tool", "content": "(результат не сохранён)", "name": "node_manage"}


def test_close_pending_tool_calls_skips_already_recorded_pending_calls():
    # Первый вызов раунда УЖЕ получил ответ — self_result относится ко
    # ВТОРОМУ (текущему обрабатываемому), а не переписывает первый задним
    # числом.
    history = [
        {"role": "user", "content": "обнови ноды"},
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "node_manage", "arguments": {"node": "arch-t480"}}},
                {"function": {"name": "node_manage", "arguments": {"node": "jeeves"}}},
            ],
        },
        {"role": "tool", "content": "уже записанный результат arch-t480", "name": "node_manage"},
    ]
    snapshot = remind._close_pending_tool_calls(history, self_result="готово для jeeves")
    assert snapshot[-1] == {"role": "tool", "content": "готово для jeeves", "name": "node_manage"}
    assert snapshot[-2] == {
        "role": "tool",
        "content": "уже записанный результат arch-t480",
        "name": "node_manage",
    }


# --- remind ---


async def test_remind_creates_task_for_future_time(store):
    link = FakeNodeLink()
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(
        _ctx(store, node_link=link), {"when": when, "text": "полить цветы"}
    )
    assert "задача поставлена" in result
    assert len(link.calls) == 1
    action, args, dst = link.calls[0]
    assert action == task_protocol.ACTION_CREATE
    assert dst.node == task_protocol.NODE_ID
    assert dst.service == task_protocol.SERVICE_NAME
    assert args["dst_node"] == tools.LLM_NODE
    assert args["dst_service"] == tools.LLM_SERVICE
    assert args["action"] == task_protocol.ACTION_CHAT_LOOP
    assert args["meta"]["kind"] == task_protocol.TASK_KIND_LLM_CHAT
    assert args["meta"]["chat_id"] == CHAT_ID
    assert "полить цветы" in args["args"]["messages"][-1]["content"]


async def test_remind_carries_message_thread_id_in_meta(store):
    # Живой баг 2026-08-05: без этого ответ tasks (bot/node_events.py)
    # уезжал в общий топик личного чата, а не в тот, откуда пришёл запрос.
    link = FakeNodeLink()
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await tools.tool_remind(
        _ctx(store, node_link=link, message_thread_id=42),
        {"when": when, "text": "полить цветы"},
    )
    _action, args, _dst = link.calls[0]
    assert args["meta"]["message_thread_id"] == 42


async def test_remind_includes_history_snapshot_in_directive(store):
    link = FakeNodeLink()
    history = [{"role": "user", "content": "привет"}, {"role": "assistant", "content": "привет!"}]
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await tools.tool_remind(
        _ctx(store, node_link=link, history=history), {"when": when, "text": "напомни"}
    )
    messages = link.calls[0][1]["args"]["messages"]
    assert messages[:2] == history


async def test_remind_closes_dangling_tool_calls_in_snapshot(store):
    # Живой сбой 2026-08-04: remind() читает ctx.history из СЕРЕДИНЫ раунда
    # tool-calling (llm_chat.run_chat_loop дописывает assistant/tool_calls
    # раньше, чем допишет ответное "tool" за сам вызов remind) — снимок с
    # висящим tool_calls без ответа ломал повторный /api/chat к Ollama
    # (HTTP 400) в момент срабатывания задачи. tool_remind обязан закрыть
    # такой хвост синтетическим "tool"-ответом, не трогая сам ctx.history
    # (остальные вызовы того же раунда мутируют ту же ссылку).
    link = FakeNodeLink()
    history = [
        {"role": "user", "content": "напомни и глянь погоду"},
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "remind", "arguments": {}}},
                {"function": {"name": "get_weather", "arguments": {}}},
            ],
        },
    ]
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await tools.tool_remind(
        _ctx(store, node_link=link, history=history), {"when": when, "text": "напомни"}
    )
    messages = link.calls[0][1]["args"]["messages"]
    assert messages[2] == {"role": "tool", "content": "(результат не сохранён)", "name": "remind"}
    assert messages[3] == {
        "role": "tool",
        "content": "(результат не сохранён)",
        "name": "get_weather",
    }
    assert messages[4]["role"] == prompt._DIRECTIVE_ROLE
    assert messages[4]["content"].startswith(prompt._DIRECTIVE_MARKER)
    # Исходный (живой, мутируемый другими вызовами того же раунда) history
    # не тронут — правится только копия, уходящая в задачу.
    assert history == [
        {"role": "user", "content": "напомни и глянь погоду"},
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "remind", "arguments": {}}},
                {"function": {"name": "get_weather", "arguments": {}}},
            ],
        },
    ]


async def test_remind_rejects_past_time(store):
    link = FakeNodeLink()
    when = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(_ctx(store, node_link=link), {"when": when, "text": "поздно"})
    assert result.startswith("ошибка")
    assert link.calls == []


async def test_remind_rejects_invalid_iso(store):
    result = await tools.tool_remind(
        _ctx(store, node_link=FakeNodeLink()), {"when": "завтра", "text": "текст"}
    )
    assert result.startswith("ошибка")


async def test_remind_rejects_missing_text(store):
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(
        _ctx(store, node_link=FakeNodeLink()), {"when": when, "text": ""}
    )
    assert result.startswith("ошибка")


async def test_remind_without_chat_id(store):
    result = await tools.tool_remind(
        _ctx(store, chat_id=None, node_link=FakeNodeLink()), {"when": "x", "text": "x"}
    )
    assert "вне диалога" in result


async def test_remind_without_node_link(store):
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(_ctx(store, node_link=None), {"when": when, "text": "x"})
    assert "служба задач недоступна" in result


async def test_remind_reports_error_when_task_service_unreachable(store):
    link = FakeNodeLink(raises=ServiceUnavailableError("boom"))
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(_ctx(store, node_link=link), {"when": when, "text": "x"})
    assert result.startswith("внутренняя ошибка")


# --- remind(after_event=...): проснуться по событию, не по будильнику ---


async def test_remind_after_event_creates_task_without_when(store):
    link = FakeNodeLink()
    result = await tools.tool_remind(
        _ctx(store, node_link=link),
        {
            "after_event": {"node": "arch-t480", "event": "restart_applied"},
            "text": "продолжи обновление роя",
        },
    )
    assert "жду событие" in result
    assert "arch-t480" in result
    action, args, dst = link.calls[0]
    assert action == task_protocol.ACTION_CREATE
    # due_at — страховка, не пусто, и не «прямо сейчас».
    due_at = datetime.fromisoformat(args["due_at"])
    assert due_at > datetime.now(UTC)
    # Живой баг 2026-08-05: конкретное время дедлайна в директиве
    # заставляло маленькую модель (Gemma) звать get_time по кругу, чтобы
    # его "проверить" — решение системы, а не модели, время из текста убрано.
    directive = args["args"]["messages"][-1]["content"]
    assert due_at.strftime("%H:%M") not in directive
    assert "продолжи обновление роя" in args["args"]["messages"][-1]["content"]


async def test_remind_after_event_puts_await_event_in_create_rpc(store):
    # Решение пользователя 2026-08-05, живой инцидент: ожидание теперь
    # ставит сама служба tasks (await_event в том же ACTION_CREATE), а не
    # БД бота — remind, вызванный из уже сработавшего хода (там нет
    # ctx.store), тоже должен уметь его поставить.
    link = FakeNodeLink()
    await tools.tool_remind(
        _ctx(store, node_link=link),
        {
            "after_event": {"node": "arch-t480", "event": "restart_applied"},
            "text": "продолжи",
        },
    )
    _action, args, _dst = link.calls[0]
    assert args["await_event"] == {"node": "arch-t480", "event_type": "restart_applied"}


async def test_remind_after_event_stores_awaited_pair_in_meta(store):
    # Восстанавливается в ToolContext.woken_by на срабатывании
    # (tasks/service.py::_fire_chat_loop) — иначе некому проверять
    # запрет на повторный remind того же события.
    link = FakeNodeLink()
    await tools.tool_remind(
        _ctx(store, node_link=link),
        {"after_event": {"node": "arch-t480", "event": "restart_applied"}, "text": "продолжи"},
    )
    _action, args, _dst = link.calls[0]
    assert args["meta"]["awaited_node"] == "arch-t480"
    assert args["meta"]["awaited_event"] == "restart_applied"


async def test_remind_after_event_rejects_unknown_event_name(store):
    result = await tools.tool_remind(
        _ctx(store, node_link=FakeNodeLink()),
        {"after_event": {"node": "arch-t480", "event": "something_else"}, "text": "x"},
    )
    assert result.startswith("ошибка")


async def test_remind_after_event_rejects_missing_node(store):
    result = await tools.tool_remind(
        _ctx(store, node_link=FakeNodeLink()),
        {"after_event": {"event": "restart_applied"}, "text": "x"},
    )
    assert result.startswith("ошибка")


async def test_remind_after_event_rejects_same_event_that_woke_this_run(store):
    # Живой инцидент 2026-08-05: модель, разбуженная по (node, event_type),
    # систематически игнорировала словесный запрет и заново ставила remind
    # на ТО ЖЕ САМОЕ событие — бесконечный цикл самопереноса. Жёсткий отказ
    # вместо тихого согласия.
    link = FakeNodeLink()
    result = await tools.tool_remind(
        _ctx(store, node_link=link, woken_by=("jeeves", "update_finished")),
        {"after_event": {"node": "jeeves", "event": "update_finished"}, "text": "x"},
    )
    assert result.startswith("ошибка")
    assert "уже наступило" in result
    assert link.calls == []  # задача не создана — не просто отказ на словах


async def test_remind_after_event_allows_different_node_than_woke_this_run(store):
    link = FakeNodeLink()
    result = await tools.tool_remind(
        _ctx(store, node_link=link, woken_by=("jeeves", "update_finished")),
        {"after_event": {"node": "arch-t480", "event": "update_finished"}, "text": "x"},
    )
    assert "жду событие" in result
    assert len(link.calls) == 1


async def test_remind_after_event_allows_different_event_than_woke_this_run(store):
    link = FakeNodeLink()
    result = await tools.tool_remind(
        _ctx(store, node_link=link, woken_by=("jeeves", "update_finished")),
        {"after_event": {"node": "jeeves", "event": "restart_applied"}, "text": "x"},
    )
    assert "жду событие" in result
    assert len(link.calls) == 1


async def test_remind_after_event_works_without_store():
    # Живой инцидент 2026-08-05: тул remind, вызванный ИЗ УЖЕ СРАБОТАВШЕГО
    # хода (служба tasks, ctx.store там нет вовсе), должен уметь поставить
    # after_event ровно как из живого чата.
    link = FakeNodeLink()
    result = await tools.tool_remind(
        _ctx(None, node_link=link),
        {"after_event": {"node": "arch-t480", "event": "restart_applied"}, "text": "x"},
    )
    assert "жду событие" in result
    _action, args, _dst = link.calls[0]
    assert args["await_event"] == {"node": "arch-t480", "event_type": "restart_applied"}


async def test_remind_sends_reason_not_think(store):
    # С v0.97.0 напоминание кладёт в задачу намерение-уровень reason
    # (перевод в параметр Ollama — профиль модели на стороне службы llm),
    # а не булев think — отсюда и ушли прежние баги с 400 на gemma.
    link = FakeNodeLink()
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    settings = Settings(llm=LlmConfig(think_chat=True))
    await tools.tool_remind(
        _ctx(store, settings=settings, node_link=link), {"when": when, "text": "x"}
    )
    args = link.calls[0][1]["args"]
    assert args["reason"] == "high"
    assert "think" not in args


async def test_remind_reason_off_when_think_chat_disabled(store):
    link = FakeNodeLink()
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    settings = Settings(llm=LlmConfig(think_chat=False))
    await tools.tool_remind(
        _ctx(store, settings=settings, node_link=link), {"when": when, "text": "x"}
    )
    assert link.calls[0][1]["args"]["reason"] == "off"


async def test_remind_reason_off_for_single_call_think_false(store):
    link = FakeNodeLink()
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    settings = Settings(llm=LlmConfig(mode="single_call", single_call_think=False))
    await tools.tool_remind(
        _ctx(store, settings=settings, node_link=link), {"when": when, "text": "x"}
    )
    assert link.calls[0][1]["args"]["reason"] == "off"


async def test_remind_when_still_works_without_after_event(store):
    # Обратная совместимость: обычный remind по времени не задет.
    link = FakeNodeLink()
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(
        _ctx(store, node_link=link), {"when": when, "text": "полить цветы"}
    )
    assert "задача поставлена" in result
    assert "await_event" not in link.calls[0][1]


# --- schedule_agent_dialogue: проактивный chat_loop без готового треда
# (Этап 44.1, задел под агента установки связи 42.6) ---


async def test_schedule_agent_dialogue_creates_chat_loop_task_without_dialogue_id(store):
    link = FakeNodeLink()
    messages = [{"role": "user", "content": "скажи гостю B привет от A"}]
    other_chat_id = 222
    result = await tools.schedule_agent_dialogue(link, other_chat_id, messages, "high", 60.0)
    assert result == {"task_id": 1}
    assert len(link.calls) == 1
    action, args, dst = link.calls[0]
    assert action == task_protocol.ACTION_CREATE
    assert dst.node == task_protocol.NODE_ID
    assert dst.service == task_protocol.SERVICE_NAME
    assert args["dst_node"] == tools.LLM_NODE
    assert args["dst_service"] == tools.LLM_SERVICE
    assert args["action"] == task_protocol.ACTION_CHAT_LOOP
    assert args["args"] == {"messages": messages, "reason": "high", "chat_id": other_chat_id}
    assert args["timeout_s"] == 60.0
    # Ключевое отличие от tool_remind: никакого dialogue_id/trigger_message_id/
    # message_thread_id — их рождение отдано bot/node_events.py (Этап 44.2).
    assert args["meta"] == {
        "kind": task_protocol.TASK_KIND_LLM_CHAT,
        "chat_id": other_chat_id,
    }


async def test_schedule_agent_dialogue_due_at_is_now(store):
    link = FakeNodeLink()
    before = datetime.now(UTC)
    await tools.schedule_agent_dialogue(link, 222, [{"role": "user", "content": "x"}], "off", 30.0)
    after = datetime.now(UTC)
    due_at = datetime.fromisoformat(link.calls[0][1]["due_at"])
    assert before <= due_at <= after


async def test_schedule_agent_dialogue_propagates_service_errors(store):
    link = FakeNodeLink(raises=ServiceUnavailableError("служба задач недоступна"))
    with pytest.raises(ServiceUnavailableError):
        await tools.schedule_agent_dialogue(
            link, 222, [{"role": "user", "content": "x"}], "off", 30.0
        )
