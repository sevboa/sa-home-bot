"""bot/vpn_nodes.py: динамический поиск нод со службой ``vpn`` и выбор
адресата команды (этап 39, фаза 0 — серверов AmneziaWG может быть > 1)."""

from __future__ import annotations

from sa_home_bot.bot import vpn_nodes
from sa_home_bot.bot.service_link import ServiceUnavailableError


class FakeLink:
    """Двойник ServiceLink: get_state(dst) отдаёт состояние по ноде."""

    def __init__(self, own: dict, peer_states: dict[str, dict] | None = None) -> None:
        self._own = own
        self._peers = peer_states or {}

    async def get_state(self, dst=None):
        if dst is None:
            return self._own
        if dst.node in self._peers:
            return self._peers[dst.node]
        raise ServiceUnavailableError(f"нет связи с {dst.node}")


def _svc(name: str, status: str = "running") -> dict:
    return {"name": name, "service": name, "status": status}


def _node_state(node: str, services: list[dict], peers: list[dict] | None = None) -> dict:
    return {"node": node, "kind": "vps", "services": services, "peers": peers or []}


async def test_own_node_carries_vpn_is_first():
    link = FakeLink(_node_state("jeeves", [_svc("vpn"), _svc("monitor")]))
    assert await vpn_nodes.live_vpn_nodes(link) == ["jeeves"]


async def test_peers_with_vpn_are_collected_self_first():
    own = _node_state(
        "alfred",
        [_svc("monitor")],
        peers=[
            {"id": "jeeves", "alive": True, "kind": "vps"},
            {"id": "wooster", "alive": True, "kind": "vps"},
        ],
    )
    link = FakeLink(
        own,
        {
            "jeeves": _node_state("jeeves", [_svc("vpn")]),
            "wooster": _node_state("wooster", [_svc("vpn")]),
        },
    )
    assert await vpn_nodes.live_vpn_nodes(link) == ["jeeves", "wooster"]


async def test_node_without_running_vpn_is_skipped():
    own = _node_state(
        "alfred",
        [_svc("monitor")],
        peers=[
            {"id": "jeeves", "alive": True, "kind": "vps"},
            {"id": "wooster", "alive": True, "kind": "vps"},
        ],
    )
    link = FakeLink(
        own,
        {
            "jeeves": _node_state("jeeves", [_svc("vpn", status="stopped")]),
            "wooster": _node_state("wooster", [_svc("vpn")]),
        },
    )
    assert await vpn_nodes.live_vpn_nodes(link) == ["wooster"]


async def test_dead_peer_is_not_queried():
    own = _node_state(
        "alfred",
        [_svc("monitor")],
        peers=[{"id": "jeeves", "alive": False, "kind": "vps"}],
    )
    link = FakeLink(own, {})  # jeeves-состояния нет — но его и не спросят
    assert await vpn_nodes.live_vpn_nodes(link) == []


async def test_swarm_unreachable_returns_empty():
    class Dead:
        async def get_state(self, dst=None):
            raise ServiceUnavailableError("нет ноды")

    assert await vpn_nodes.live_vpn_nodes(Dead()) == []
    assert await vpn_nodes.resolve_vpn_dst(Dead()) is None


async def test_resolve_prefers_requested_server_when_alive():
    own = _node_state(
        "alfred",
        [_svc("monitor")],
        peers=[
            {"id": "jeeves", "alive": True, "kind": "vps"},
            {"id": "wooster", "alive": True, "kind": "vps"},
        ],
    )
    link = FakeLink(
        own,
        {
            "jeeves": _node_state("jeeves", [_svc("vpn")]),
            "wooster": _node_state("wooster", [_svc("vpn")]),
        },
    )
    dst = await vpn_nodes.resolve_vpn_dst(link, server="wooster")
    assert (dst.node, dst.service) == ("wooster", "vpn")


async def test_resolve_falls_back_to_first_live_when_server_gone():
    own = _node_state(
        "alfred",
        [_svc("monitor")],
        peers=[{"id": "wooster", "alive": True, "kind": "vps"}],
    )
    link = FakeLink(own, {"wooster": _node_state("wooster", [_svc("vpn")])})
    # Подключение заведено на jeeves, но jeeves сейчас VPN не держит.
    dst = await vpn_nodes.resolve_vpn_dst(link, server="jeeves")
    assert dst.node == "wooster"


async def test_resolve_none_when_no_vpn_anywhere():
    link = FakeLink(_node_state("alfred", [_svc("monitor")]))
    assert await vpn_nodes.resolve_vpn_dst(link) is None


class FanoutLink(FakeLink):
    """FakeLink + command(): отвечает по адресату, мёртвые ноды падают."""

    def __init__(self, own, peer_states=None, *, answers=None, dead=()) -> None:
        super().__init__(own, peer_states)
        self._answers = answers or {}
        self._dead = set(dead)
        self.seen: list[str] = []

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.seen.append(dst.node)
        if dst.node in self._dead:
            raise ServiceUnavailableError(f"нет связи с {dst.node}")
        return self._answers.get(dst.node, {})


def _two_vpn_nodes() -> dict:
    return _node_state(
        "alfred",
        [_svc("monitor")],
        peers=[
            {"id": "jeeves", "alive": True, "kind": "vps"},
            {"id": "wooster", "alive": True, "kind": "vps"},
        ],
    )


_VPN_PEERS = {
    "jeeves": _node_state("jeeves", [_svc("vpn")]),
    "wooster": _node_state("wooster", [_svc("vpn")]),
}


async def test_fanout_collects_answers_from_every_live_node():
    link = FanoutLink(
        _two_vpn_nodes(),
        _VPN_PEERS,
        answers={"jeeves": {"used_bytes": 1}, "wooster": {"used_bytes": 2}},
    )
    results = await vpn_nodes.fanout(link, "usage", {"chat_id": 7})
    assert [r["used_bytes"] for r in results] == [1, 2]
    assert link.seen == ["jeeves", "wooster"]


async def test_fanout_skips_node_that_died_after_discovery():
    link = FanoutLink(
        _two_vpn_nodes(), _VPN_PEERS, answers={"jeeves": {"used_bytes": 1}}, dead={"wooster"}
    )
    results = await vpn_nodes.fanout(link, "usage", {"chat_id": 7})
    assert results == [{"used_bytes": 1}]


async def test_fanout_empty_when_no_vpn_anywhere():
    link = FanoutLink(_node_state("alfred", [_svc("monitor")]))
    assert await vpn_nodes.fanout(link, "usage", {}) == []


class ServiceStateLink(FakeLink):
    """Различает опрос ноды (service="node") и опрос самой службы vpn —
    именно так и ходит live_vpn_servers."""

    def __init__(self, own, peer_states, vpn_states, *, dead=()) -> None:
        super().__init__(own, peer_states)
        self._vpn_states = vpn_states
        self._dead = set(dead)

    async def get_state(self, dst=None):
        if dst is not None and dst.service == "vpn":
            if dst.node in self._dead:
                raise ServiceUnavailableError(f"нет связи с {dst.node}")
            return self._vpn_states[dst.node]
        return await super().get_state(dst)


async def test_live_vpn_servers_reports_label_and_transports():
    link = ServiceStateLink(
        _two_vpn_nodes(),
        _VPN_PEERS,
        {
            "jeeves": {"label": "🇳🇱 Нидерланды", "transports": ["awg", "reality"]},
            "wooster": {"label": "", "transports": ["reality"]},
        },
    )
    assert await vpn_nodes.live_vpn_servers(link) == [
        {"node": "jeeves", "label": "🇳🇱 Нидерланды", "transports": ["awg", "reality"]},
        # label пуст — карточка подставит id ноды
        {"node": "wooster", "label": "", "transports": ["reality"]},
    ]


async def test_live_vpn_servers_skips_unreachable_service():
    link = ServiceStateLink(
        _two_vpn_nodes(),
        _VPN_PEERS,
        {"jeeves": {"label": "🇳🇱", "transports": ["awg"]}},
        dead={"wooster"},
    )
    assert [s["node"] for s in await vpn_nodes.live_vpn_servers(link)] == ["jeeves"]


async def test_probe_targets_excludes_self_and_flattens_transports():
    link = ServiceStateLink(
        _two_vpn_nodes(),
        _VPN_PEERS,
        {
            "jeeves": {"label": "🇳🇱", "transports": ["awg", "reality"]},
            "wooster": {"label": "🇺🇸", "transports": ["reality"]},
        },
    )
    targets = await vpn_nodes.probe_targets(link, exclude="alfred")
    assert targets == [
        vpn_nodes.ProbeTarget(server="jeeves", transport="awg"),
        vpn_nodes.ProbeTarget(server="jeeves", transport="reality"),
        vpn_nodes.ProbeTarget(server="wooster", transport="reality"),
    ]


async def test_probe_targets_excludes_self_when_self_is_a_vpn_server():
    link = ServiceStateLink(
        _two_vpn_nodes(),
        _VPN_PEERS,
        {
            "jeeves": {"label": "🇳🇱", "transports": ["awg"]},
            "wooster": {"label": "🇺🇸", "transports": ["reality"]},
        },
    )
    targets = await vpn_nodes.probe_targets(link, exclude="jeeves")
    assert targets == [vpn_nodes.ProbeTarget(server="wooster", transport="reality")]


async def test_probe_targets_empty_when_no_vpn_anywhere():
    link = FakeLink(_node_state("alfred", [_svc("monitor")]))
    assert await vpn_nodes.probe_targets(link, exclude="alfred") == []
