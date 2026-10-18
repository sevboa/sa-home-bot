from sa_home_bot.bot import wake_state
from sa_home_bot.bot.handlers.wake import _wake_swarm_node
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.proto.messages import ProtoError
from tests.unit.support.fakes import FakeMessage, FakeNodeLink

ALFRED_WAKE = {"mac": "7c:83:34:b4:59:ac", "ip": "192.168.0.100", "broadcast": "192.168.0.255"}
WINPC_WAKE = {"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.0.50", "broadcast": "192.168.0.255"}

OWN_STATE = {
    "node": "alfred",
    "version": "0.25.0",
    "services": [],
    "wake": ALFRED_WAKE,
    "peers": [
        {"id": "arch-t480", "endpoint": "tcp://x:8710", "alive": True},
        {"id": "winpc", "endpoint": "tcp://y:8710", "alive": False},
    ],
}

ARCH_STATE = {"node": "arch-t480", "version": "0.25.0", "services": [], "wake": None}


def _link(own=OWN_STATE, error=None) -> FakeNodeLink:
    return FakeNodeLink(
        own, routes={"arch-t480:node": ARCH_STATE}, result={"sent": True}, error=error
    )


async def test_wake_swarm_node_without_cache_asks_to_wait(store):
    message = FakeMessage()
    link = _link()
    await _wake_swarm_node(message, link, store, "winpc")
    assert "Нет данных о MAC" in message.answers[0]
    assert link.calls == []


async def test_wake_swarm_node_sends_via_matching_lan_peer(store):
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    message = FakeMessage()
    link = _link()

    await _wake_swarm_node(message, link, store, "winpc")

    assert [(a, args, dst.node) for a, args, dst in link.calls] == [
        ("send_wol", {"mac": WINPC_WAKE["mac"]}, "alfred")
    ]
    assert "через ноду «alfred»" in message.answers[0]


async def test_wake_swarm_node_no_matching_lan_peer(store):
    await wake_state.remember(store, "winpc", {**WINPC_WAKE, "broadcast": "10.0.0.255"})
    message = FakeMessage()
    link = _link({**OWN_STATE, "wake": None})

    await _wake_swarm_node(message, link, store, "winpc")

    assert "Некому отправить сигнал" in message.answers[0]
    assert link.calls == []


async def test_wake_swarm_node_waker_goes_unavailable_mid_flight(store):
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    message = FakeMessage()
    link = _link(error=ServiceUnavailableError("оборвалось"))

    await _wake_swarm_node(message, link, store, "winpc")

    assert "перестала отвечать" in message.answers[0]


async def test_wake_swarm_node_proto_error_from_waker(store):
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    message = FakeMessage()
    link = _link(error=ProtoError("bad_request", "битый MAC"))

    await _wake_swarm_node(message, link, store, "winpc")

    assert "битый MAC" in message.answers[0]
