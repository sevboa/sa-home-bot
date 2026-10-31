from __future__ import annotations

import sqlite3
import stat

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.config import Settings
from sa_home_bot.devlab import connect as devlab_connect
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import connect_http, http
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.nodes import SwarmNodes, node_at
from sa_home_bot.devlab.hub.service import HubService
from sa_home_bot.devlab.hub.store import HUB_DB_NAME, HubStore, Principal, hash_token
from sa_home_bot.proto.messages import ProtoError
from tests.unit.support.fakes import LOOPBACK, Clock, FakeNodes

GUARDED_PATH = "/guarded"
SWARM_TOKEN = "swarm-secret"


async def guarded(request: web.Request) -> web.Response:
    return web.json_response({"who": request[http.PRINCIPAL_KEY].name})


@pytest.fixture
async def store(tmp_path):
    hub_store = HubStore(tmp_path)
    await hub_store.open()
    yield hub_store
    await hub_store.close()


async def serve(store, nodes):
    app = http.build_app(store, nodes, SWARM_TOKEN)
    app.router.add_get(GUARDED_PATH, guarded)
    connect_http.add_routes(app)
    return TestClient(TestServer(app))


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("role", protocol.ROLES)
@pytest.mark.parametrize(
    ("nodes", "status"),
    [
        ({"pc": {LOOPBACK}}, 200),
        ({"pc": {"100.64.0.9"}, "other": {LOOPBACK}}, 403),
        ({"other": {LOOPBACK}}, 403),
        ({}, 403),
    ],
)
async def test_token_works_only_from_its_node(store, role, nodes, status):
    token = await store.issue_token("pc", role)
    async with await serve(store, FakeNodes(nodes)) as client:
        resp = await client.get(GUARDED_PATH, headers=bearer(token))
        assert resp.status == status


async def test_health_stays_public_and_bad_token_is_401(store):
    async with await serve(store, FakeNodes({})) as client:
        assert (await client.get(protocol.HTTP_HEALTH_PATH)).status == 200
        assert (await client.get(GUARDED_PATH, headers=bearer("nope"))).status == 401


def swarm_nodes(states, clock, resolved=None):
    calls = {"fetch": 0}
    resolved = resolved if resolved is not None else {}

    async def fetch():
        calls["fetch"] += 1
        state = states[0]
        if isinstance(state, Exception):
            raise state
        return state

    async def resolve(host):
        if host not in resolved:
            raise OSError(host)
        return frozenset(resolved[host])

    return SwarmNodes(fetch, "alfred", "100.64.0.1", clock=clock, resolve=resolve), calls


def peers_state(*peers):
    return {"peers": [{"id": n, "endpoint": f"tcp://{h}:7000"} for n, h in peers]}


async def test_swarm_nodes_resolve_cache_and_fallback():
    clock = Clock()
    states = [peers_state(("mycraft", "mycraft.ts.net"))]
    nodes, calls = swarm_nodes(
        states, clock, {"mycraft.ts.net": {"100.64.0.2"}}
    )
    first = await nodes.addresses()
    assert first == {"alfred": {"100.64.0.1"}, "mycraft": {"100.64.0.2"}}
    states[0] = peers_state(("jeeves", "jeeves.ts.net"))
    await nodes.addresses()
    assert calls["fetch"] == 1
    clock.advance(seconds=3600)
    states[0] = ConnectionError("рой недоступен")
    assert await nodes.addresses() == first
    assert calls["fetch"] == 2


async def test_swarm_nodes_keep_last_ips_when_dns_fails_and_own_ip_always_present():
    clock = Clock()
    states = [peers_state(("mycraft", "mycraft.ts.net"))]
    resolved = {"mycraft.ts.net": {"100.64.0.2"}}
    nodes, _ = swarm_nodes(states, clock, resolved)
    await nodes.addresses()
    clock.advance(seconds=3600)
    resolved.clear()
    after = await nodes.addresses()
    assert after["mycraft"] == {"100.64.0.2"}
    assert after["alfred"] == {"100.64.0.1"}


def test_node_at_needs_unique_owner():
    book = {"a": frozenset({"1"}), "b": frozenset({"2"}), "c": frozenset({"2"})}
    assert node_at(book, "1") == "a"
    assert node_at(book, "2") is None
    assert node_at(book, "9") is None


def connect_headers(token=SWARM_TOKEN):
    return {protocol.HEADER_SWARM_TOKEN: token}


async def test_connect_issues_client_token_for_sender_node(store):
    async with await serve(store, FakeNodes({"laptop": {LOOPBACK}, "pc": {"100.64.0.9"}})) as c:
        resp = await c.post(protocol.HTTP_CONNECT_PATH, headers=connect_headers())
        body = await resp.json()
        assert resp.status == 200
        assert body[protocol.FIELD_NAME] == "laptop"
        assert body[protocol.FIELD_ROLE] == protocol.ROLE_CLIENT
        first = body[protocol.FIELD_TOKEN]
        assert (await c.get(GUARDED_PATH, headers=bearer(first))).status == 200
        resp = await c.post(protocol.HTTP_CONNECT_PATH, headers=connect_headers())
        second = (await resp.json())[protocol.FIELD_TOKEN]
        assert (await c.get(GUARDED_PATH, headers=bearer(first))).status == 401
        assert (await c.get(GUARDED_PATH, headers=bearer(second))).status == 200


@pytest.mark.parametrize(
    ("nodes", "headers", "status"),
    [
        ({"laptop": {LOOPBACK}}, {}, 401),
        ({"laptop": {LOOPBACK}}, connect_headers("wrong"), 401),
        ({"laptop": {"100.64.0.9"}}, connect_headers(), 403),
    ],
)
async def test_connect_rejects_bad_swarm_token_and_foreign_address(store, nodes, headers, status):
    async with await serve(store, FakeNodes(nodes)) as client:
        resp = await client.post(protocol.HTTP_CONNECT_PATH, headers=headers)
        assert resp.status == status
    assert await store.count_active() == 0


async def test_connect_leaves_other_roles_of_the_node_alone(store):
    executor = await store.issue_token("laptop", protocol.ROLE_EXECUTOR)
    gateway = await store.issue_token("laptop", protocol.ROLE_GATEWAY)
    async with await serve(store, FakeNodes({"laptop": {LOOPBACK}})) as client:
        resp = await client.post(protocol.HTTP_CONNECT_PATH, headers=connect_headers())
        assert resp.status == 200
        for token in (executor, gateway, (await resp.json())[protocol.FIELD_TOKEN]):
            assert (await client.get(GUARDED_PATH, headers=bearer(token))).status == 200
        await client.post(protocol.HTTP_CONNECT_PATH, headers=connect_headers())
        assert (await client.get(GUARDED_PATH, headers=bearer(executor))).status == 200


async def test_node_holds_one_token_per_role_and_revokes_by_role(store):
    client = await store.issue_token("mycraft", protocol.ROLE_CLIENT)
    executor = await store.issue_token("mycraft", protocol.ROLE_EXECUTOR)
    with pytest.raises(HubError):
        await store.issue_token("mycraft", protocol.ROLE_EXECUTOR)
    with pytest.raises(HubError):
        await store.revoke_token("mycraft")
    with pytest.raises(HubError):
        await store.revoke_token("mycraft", protocol.ROLE_GATEWAY)
    await store.revoke_token("mycraft", protocol.ROLE_EXECUTOR)
    assert await store.authenticate(executor) is None
    assert (await store.authenticate(client)).role == protocol.ROLE_CLIENT
    await store.revoke_token("mycraft")
    assert await store.authenticate(client) is None


async def test_heartbeats_are_kept_per_node_and_role(store):
    beat = {protocol.FIELD_MODELS: [{protocol.MODEL_NAME: "flint"}]}
    await store.heartbeat(Principal("mycraft", protocol.ROLE_EXECUTOR), {})
    await store.heartbeat(Principal("mycraft", protocol.ROLE_GATEWAY), beat)
    assert await store.executor_alive()
    assert [m[protocol.MODEL_NAME] for m in await store.models()] == ["flint"]


LEGACY_SCHEMA = """
CREATE TABLE tokens (
    name TEXT PRIMARY KEY, role TEXT NOT NULL, hash TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL, revoked TEXT
);
CREATE TABLE heartbeats (
    name TEXT PRIMARY KEY, role TEXT NOT NULL, data TEXT NOT NULL, seen TEXT NOT NULL
);
"""


async def test_legacy_name_keyed_database_is_migrated_keeping_tokens(tmp_path):
    token = "dlh_legacy"
    with sqlite3.connect(tmp_path / HUB_DB_NAME) as legacy:
        legacy.executescript(LEGACY_SCHEMA)
        legacy.execute(
            "INSERT INTO tokens VALUES (?, ?, ?, ?, NULL)",
            ("alfred", protocol.ROLE_CLIENT, hash_token(token), "2026-11-01T00:00:00+00:00"),
        )
        legacy.execute(
            "INSERT INTO heartbeats VALUES ('pc', ?, '{}', '2026-11-01T00:00:00+00:00')",
            (protocol.ROLE_EXECUTOR,),
        )
    migrated = HubStore(tmp_path)
    await migrated.open()
    try:
        assert (await migrated.authenticate(token)) == Principal("alfred", protocol.ROLE_CLIENT)
        await migrated.issue_token("alfred", protocol.ROLE_EXECUTOR)
        assert (await migrated.authenticate(token)).role == protocol.ROLE_CLIENT
        cur = await migrated._db.conn.execute("SELECT count(*) AS n FROM heartbeats")
        assert (await cur.fetchone())["n"] == 1
    finally:
        await migrated.close()
    reopened = HubStore(tmp_path)
    await reopened.open()
    assert (await reopened.authenticate(token)).name == "alfred"
    await reopened.close()


async def test_issue_token_name_must_be_swarm_node(store):
    service = HubService(store, FakeNodes({"alfred": {"1"}, "mycraft": {"2"}}))
    issued = await service.run_command(
        protocol.ACTION_ISSUE_TOKEN, {"name": "mycraft", "role": protocol.ROLE_EXECUTOR}
    )
    assert issued["name"] == "mycraft"
    with pytest.raises(ProtoError):
        await service.run_command(
            protocol.ACTION_ISSUE_TOKEN, {"name": "laptop", "role": protocol.ROLE_CLIENT}
        )


async def test_issue_token_name_unchecked_when_swarm_list_unknown(store):
    service = HubService(store, FakeNodes({"alfred": {"1"}}))
    issued = await service.run_command(
        protocol.ACTION_ISSUE_TOKEN, {"name": "anything", "role": protocol.ROLE_CLIENT}
    )
    assert issued["name"] == "anything"


def settings():
    return Settings.model_validate({"swarm": {"token": SWARM_TOKEN}})


async def test_nodectl_connect_writes_private_file_and_hides_token(tmp_path, capsys):
    path = tmp_path / "cfg" / "devlab-token"
    ran = []

    async def fetch(url, swarm_token):
        assert url == "http://alfred:8720"
        assert swarm_token == SWARM_TOKEN
        return "dlh_secret"

    code = await devlab_connect.connect(
        settings(),
        token_path=str(path),
        skills_dir=str(tmp_path / "skills"),
        fetch=fetch,
        runner=lambda argv: ran.append(argv) or 0,
        which=lambda name: "/usr/bin/claude",
    )
    out = capsys.readouterr()
    assert code == 0
    assert (tmp_path / "skills" / "devlab-test" / "SKILL.md").read_text() == (
        devlab_connect.skill_source()
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().strip() == "dlh_secret"
    assert "dlh_secret" not in out.out + out.err
    assert ran[0][:3] == ["claude", "mcp", "add"]
    assert ran[0][3:8] == ["--scope", "user", "--transport", "http", "devlab"]
    assert ran[0][8] == "http://alfred:8720/mcp"
    assert ran[0][-1] == "Authorization: Bearer dlh_secret"


async def test_nodectl_connect_without_claude_prints_command_without_token(tmp_path, capsys):
    path = tmp_path / "devlab-token"

    async def fetch(url, swarm_token):
        return "dlh_secret"

    code = await devlab_connect.connect(
        settings(),
        token_path=str(path),
        skills_dir=str(tmp_path / "skills"),
        fetch=fetch,
        which=lambda name: None,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "claude mcp add --scope user --transport http devlab http://alfred:8720/mcp" in out
    assert f"$(cat {path})" in out
    assert "dlh_secret" not in out


async def test_nodectl_connect_reports_hub_error(tmp_path, capsys):
    async def fetch(url, swarm_token):
        raise devlab_connect.ConnectError("хаб ответил HTTP 403")

    code = await devlab_connect.connect(
        settings(), token_path=str(tmp_path / "t"), fetch=fetch, which=lambda name: None
    )
    assert code == 1
    assert not (tmp_path / "t").exists()
    assert "403" in capsys.readouterr().err
