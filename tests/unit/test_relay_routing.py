import dataclasses

import pytest

from sa_home_bot.node.peers import NodeRouter, PeerLink
from sa_home_bot.proto.messages import (
    ERR_UNAVAILABLE,
    MSG_COMMAND,
    MSG_GET_STATE,
    Address,
    ProtoError,
    ServiceInfo,
    make_request,
    make_response,
)


class FakeClient:
    def __init__(self, name: str, *, broken: bool = False) -> None:
        self.name = name
        self.broken = broken
        self.seen: list = []

    async def forward(self, env):
        self.seen.append(env)
        if self.broken:
            raise ConnectionError("обрыв")
        return make_response(env, {"answered_by": self.name})


def make_link(name: str, *, rtt: float | None = None, misses: int = 0, alive: bool = True):
    link = PeerLink(name, "tcp://127.0.0.1:1")
    if alive:
        link._client = FakeClient(name)
    if rtt is not None:
        for _ in range(12 - misses):
            link.note_probe(rtt)
    for _ in range(misses):
        link.note_probe(None)
    return link


def learn(link: PeerLink, **links) -> None:
    link._learn_links(ServiceInfo(node=link.name, service="node", version="0", links=links))


def router_for(*links: PeerLink) -> NodeRouter:
    return NodeRouter("alfred", peers={link.name: link for link in links})


def request_to(node: str, type_: str = MSG_GET_STATE):
    return make_request(type_, dst=Address(node=node, service="node"))


async def test_healthy_direct_link_is_used():
    wooster = make_link("wooster", rtt=190)
    jeeves = make_link("jeeves", rtt=95)
    learn(jeeves, wooster={"rtt_ms": 85, "loss": 0, "alive": True})
    response = await router_for(wooster, jeeves).route(request_to("wooster"))
    assert response.payload == {"answered_by": "wooster"}


async def test_degraded_direct_link_goes_through_faster_relay():
    wooster = make_link("wooster", rtt=600, misses=2)
    jeeves = make_link("jeeves", rtt=95)
    learn(jeeves, wooster={"rtt_ms": 85, "loss": 0, "alive": True})
    router = router_for(wooster, jeeves)
    response = await router.route(request_to("wooster"))
    assert response.payload == {"answered_by": "jeeves"}
    assert jeeves._client.seen[0].hops == 1
    state = {p["id"]: p for p in router.peers_state()}
    assert state["wooster"]["via"] == "jeeves"
    assert state["wooster"]["via_rtt_ms"] == 180.0
    assert state["jeeves"]["via"] is None


async def test_small_gain_keeps_direct_path():
    wooster = make_link("wooster", rtt=200)
    jeeves = make_link("jeeves", rtt=95)
    learn(jeeves, wooster={"rtt_ms": 85, "loss": 0, "alive": True})
    response = await router_for(wooster, jeeves).route(request_to("wooster"))
    assert response.payload == {"answered_by": "wooster"}


async def test_relay_path_is_kept_until_direct_is_clearly_better():
    wooster = make_link("wooster", rtt=600)
    jeeves = make_link("jeeves", rtt=95)
    learn(jeeves, wooster={"rtt_ms": 85, "loss": 0, "alive": True})
    router = router_for(wooster, jeeves)
    await router.route(request_to("wooster"))
    for _ in range(30):
        wooster.note_probe(170)
    response = await router.route(request_to("wooster"))
    assert response.payload == {"answered_by": "jeeves"}
    for _ in range(30):
        wooster.note_probe(100)
    response = await router.route(request_to("wooster"))
    assert response.payload == {"answered_by": "wooster"}


async def test_relayed_request_is_forwarded_only_directly():
    wooster = make_link("wooster", rtt=600)
    jeeves = make_link("jeeves", rtt=95)
    learn(jeeves, wooster={"rtt_ms": 85, "loss": 0, "alive": True})
    relayed = dataclasses.replace(request_to("wooster"), hops=1)
    response = await router_for(wooster, jeeves).route(relayed)
    assert response.payload == {"answered_by": "wooster"}


async def test_down_direct_link_goes_through_relay_that_sees_target():
    wooster = make_link("wooster", alive=False)
    jeeves = make_link("jeeves", rtt=95)
    learn(jeeves, wooster={"rtt_ms": 85, "loss": 0, "alive": True})
    response = await router_for(wooster, jeeves).route(request_to("wooster", MSG_COMMAND))
    assert response.payload == {"answered_by": "jeeves"}


async def test_relay_that_lost_target_is_not_used():
    wooster = make_link("wooster", alive=False)
    jeeves = make_link("jeeves", rtt=95)
    learn(jeeves, wooster={"rtt_ms": None, "loss": 1.0, "alive": False})
    with pytest.raises(ProtoError) as exc:
        await router_for(wooster, jeeves).route(request_to("wooster"))
    assert exc.value.code == ERR_UNAVAILABLE


async def test_peer_without_links_table_is_never_a_relay():
    wooster = make_link("wooster", rtt=600, misses=3)
    old_node = make_link("jeeves", rtt=95)
    learn(old_node)
    response = await router_for(wooster, old_node).route(request_to("wooster"))
    assert response.payload == {"answered_by": "wooster"}


@pytest.mark.parametrize(
    ("type_", "retried"),
    [(MSG_GET_STATE, True), (MSG_COMMAND, False)],
)
async def test_only_read_requests_are_retried_through_relay(type_, retried):
    wooster = make_link("wooster", rtt=190)
    wooster._client.broken = True
    jeeves = make_link("jeeves", rtt=95)
    learn(jeeves, wooster={"rtt_ms": 85, "loss": 0, "alive": True})
    router = router_for(wooster, jeeves)
    wooster.reconnect_now = lambda reason: None
    if retried:
        response = await router.route(request_to("wooster", type_))
        assert response.payload == {"answered_by": "jeeves"}
    else:
        with pytest.raises(ProtoError):
            await router.route(request_to("wooster", type_))
        assert jeeves._client.seen == []


def test_links_table_reports_quality_of_every_peer():
    wooster = make_link("wooster", rtt=200, misses=3)
    sleeping = make_link("mycraft", alive=False)
    assert router_for(wooster, sleeping).links_table() == {
        "wooster": {"rtt_ms": 200.0, "loss": 0.25, "alive": True},
        "mycraft": {"rtt_ms": None, "loss": 0.0, "alive": False},
    }


def test_hello_links_survive_the_wire_and_stay_absent_for_other_services():
    info = ServiceInfo(
        node="jeeves",
        service="node",
        version="0",
        links={"wooster": {"rtt_ms": 85, "loss": 0, "alive": True}},
    )
    assert ServiceInfo.from_payload(info.to_payload()).links == {
        "wooster": {"rtt_ms": 85.0, "loss": 0.0, "alive": True}
    }
    plain = ServiceInfo(node="alfred", service="monitor", version="0")
    assert "links" not in plain.to_payload()
    assert ServiceInfo.from_payload(plain.to_payload()).links is None
