from sa_home_bot.bot import tools
from sa_home_bot.bot.node_events import build_node_event_handler
from sa_home_bot.config import SubscriptionConfig
from sa_home_bot.proto.messages import Address, make_event
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.tasks import protocol as task_protocol
from tests.unit.support.fakes import FakeNodeLink


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
        **kwargs,
    ):
        self.sent.append((chat_id, text))
        return 99


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
    link = FakeNodeLink(result={"task_id": 1})
    messages = [{"role": "user", "content": "A сказал(а), что ты его друг — подтверждаешь?"}]
    other_chat_id = 222

    await tools.schedule_agent_dialogue(link, other_chat_id, messages, "high", 60.0)

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
    assert sent_id == 99
    assert dialogue_id == sent_id
    assert role == "assistant"
