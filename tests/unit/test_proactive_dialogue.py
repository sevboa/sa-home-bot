"""Стык 44.1 (bot/tools.py::schedule_agent_dialogue) ↔ 44.2
(bot/node_events.py::_handle_task_result): meta, которую реально кладёт
schedule_agent_dialogue в tasks.create, при доставке результата должна
рождать новый dialogue_id = message_id первого отправленного сообщения —
без переизобретения формы meta вручную (в отличие от test_tools.py/
test_node_events.py, которые проверяют каждую половину изолированно)."""

from sa_home_bot.bot import tools
from sa_home_bot.bot.node_events import build_node_event_handler
from sa_home_bot.config import SubscriptionConfig
from sa_home_bot.proto.messages import Address, make_event
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.tasks import protocol as task_protocol


class _FakeNodeLink:
    """Тот же двойник, что tests/unit/test_tools.py::_FakeNodeLink — не
    импортируется оттуда намеренно (в этом дереве тестовые модули не тянут
    друг друга, см. соседние файлы), только форма важна для стыка."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, object]] = []

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}, dst))
        return {"task_id": 1}


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_direct(
        self,
        chat_id,
        text,
        reply_to_message_id=None,
        reply_markup=None,
        message_thread_id=None,
    ):
        self.sent.append((chat_id, text))
        return 99  # message_id только что отправленного первого сообщения


class _FakeStore:
    def __init__(self) -> None:
        self.recorded_turns: list[tuple] = []

    async def record_ai_turn(self, *args, **kwargs):
        self.recorded_turns.append((args, kwargs))

    async def record_event(self, event_type, node, text, at):
        pass


def _book() -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="guest_b", chat_id=222, event_types=["*"])]
    )


async def test_schedule_agent_dialogue_meta_spawns_new_thread_end_to_end():
    link = _FakeNodeLink()
    messages = [{"role": "user", "content": "A сказал(а), что ты его друг — подтверждаешь?"}]
    other_chat_id = 222

    await tools.schedule_agent_dialogue(link, other_chat_id, messages, "high", 60.0)

    # meta, реально созданная schedule_agent_dialogue — не переписываем вручную.
    _action, create_args, _dst = link.calls[0]
    real_meta = create_args["meta"]
    assert "dialogue_id" not in real_meta

    notifier, store = _FakeNotifier(), _FakeStore()
    handler = build_node_event_handler(_book(), notifier, store)
    env = make_event(
        task_protocol.EVENT_TASK_RESULT,
        {
            "task_id": 1,
            "meta": real_meta,
            "ok": True,
            "result": {"response": "Здравствуй, я Альфред. Подтверждаешь дружбу с A?"},
        },
        src=Address(node="alfred", service="tasks"),
    )
    await handler(env)

    assert notifier.sent == [
        (other_chat_id, "<b>Альфред:</b> Здравствуй, я Альфред. Подтверждаешь дружбу с A?")
    ]
    assert len(store.recorded_turns) == 1
    args, _kwargs = store.recorded_turns[0]
    chat_id, sent_id, dialogue_id, role = args[:4]
    assert chat_id == other_chat_id
    assert sent_id == 99  # message_id, вернул _FakeNotifier
    assert dialogue_id == sent_id  # новый тред рождён из sent_id, не пуст
    assert role == "assistant"
