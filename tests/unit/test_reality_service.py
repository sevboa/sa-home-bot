"""Служба reality: сэмплер, реконсайлер, issue/reissue/revoke, квоты — поверх
подменяемого XrayBackend (настоящий `xray` в тестах не зовём).

Миррор tests/unit/test_vpn_service.py: транспорт — UUID клиента xray + его
email вместо ключа WireGuard + адреса в подсети, учёт — из stats() вместо
`awg show transfer`.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from sa_home_bot.config import RealityConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError
from sa_home_bot.reality import protocol as reality_protocol
from sa_home_bot.reality.service import _FLOWER_NAMES, GB, RealityService, _random_device_label

CHAT = 111
OTHER_CHAT = 222


class FakeXray:
    """XrayBackend в памяти: email → (uuid, up, down)."""

    def __init__(self) -> None:
        self.clients: dict[str, str] = {}  # email -> uuid (кто реально в inbound)
        self.traffic: dict[str, tuple[int, int]] = {}  # email -> (up, down)

    async def add_client(self, client_uuid: str, email: str, flow: str) -> None:
        self.clients[email] = client_uuid
        self.traffic.setdefault(email, (0, 0))

    async def remove_client(self, email: str) -> None:
        self.clients.pop(email, None)

    async def list_clients(self) -> set[str]:
        return set(self.clients)

    async def stats(self) -> dict[str, tuple[int, int]]:
        return {email: self.traffic.get(email, (0, 0)) for email in self.clients}

    def set_traffic(self, email: str, up: int, down: int) -> None:
        self.traffic[email] = (up, down)


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(tmp_path / "reality.sqlite")
    await db.open()
    await apply_migrations(db)
    backend = FakeXray()
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    cfg = RealityConfig(
        base_quota_gb=1,  # 1 ГБ — быстрые пороги в тестах, не 500
        extra_step_gb=1,
        self_ceiling_gb=3,
        warn_remaining_gb=0,  # переопределяется по тестам, где нужно
        endpoint_host="203.0.113.9",
        server_public_key="SRV_PUB",
        short_id="abcd1234",
        sni="www.google.com",
    )
    svc = RealityService(Settings(reality=cfg), db, backend, emit)
    yield svc, backend, events
    await db.close()


def _email_of(backend: FakeXray) -> str:
    return next(iter(backend.clients))


async def test_issue_creates_peer_and_artifacts(env):
    svc, backend, events = env
    result = await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    assert '"type": "vless"' in result["config_text"]
    assert "SRV_PUB" in result["config_text"]
    assert result["share_url"].startswith("vless://")
    assert "203.0.113.9:8443" in result["share_url"]
    assert result["deep_link"].startswith("hiddify://import/")
    assert result["qr_png_b64"]
    assert result["uuid"]
    assert backend.clients  # реально добавлен в inbound
    assert any(n == reality_protocol.EVENT_REALITY_PEER_ISSUED for n, _ in events)


async def test_private_material_not_in_db_only_uuid(env):
    svc, _backend, _events = env
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    cur = await svc._db.conn.execute("SELECT * FROM reality_peers")
    rows = await cur.fetchall()
    assert len(rows) == 1
    keys = rows[0].keys()
    assert "uuid" in keys
    assert "private_key" not in keys and "public_key" not in keys


def test_random_device_label_avoids_active_names_when_possible():
    used = set(_FLOWER_NAMES[:-1])
    assert _random_device_label(used) == _FLOWER_NAMES[-1]


def test_random_device_label_falls_back_to_full_pool_when_exhausted():
    assert _random_device_label(set(_FLOWER_NAMES)) in _FLOWER_NAMES


async def test_issue_assigns_random_english_label_ignoring_manual_input(env):
    svc, _backend, _events = env
    result = await svc.run_command(
        reality_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "Мой телефон"}
    )
    assert result["device_label"] in _FLOWER_NAMES


async def test_issue_reports_prior_device_count(env):
    svc, _backend, _events = env
    first = await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    assert first["prior_device_count"] == 0
    second = await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    assert second["prior_device_count"] == 1


async def test_issue_has_no_device_cap(env):
    svc, _backend, _events = env
    for label in ("a", "b", "c", "d", "e", "f", "g"):
        await svc.run_command(
            reality_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": label}
        )
    result = await svc.run_command(reality_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert len(result["devices"]) == 7


async def test_reissue_expires_old_peer_and_keeps_label(env):
    svc, backend, _events = env
    first = await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    label = first["device_label"]
    assert len(backend.clients) == 1

    second = await svc.run_command(
        reality_protocol.ACTION_REISSUE, {"chat_id": CHAT, "device_label": label}
    )
    assert len(backend.clients) == 1  # старый снят, на inbound ровно один новый
    assert second["uuid"] != first["uuid"]
    assert second["device_label"] == label

    cur = await svc._db.conn.execute(
        "SELECT status FROM reality_peers WHERE uuid = ?", (first["uuid"],)
    )
    assert (await cur.fetchone())["status"] == "expired"


async def test_revoke_removes_client_from_inbound(env):
    svc, backend, _events = env
    issued = await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    assert len(backend.clients) == 1
    result = await svc.run_command(
        reality_protocol.ACTION_REVOKE,
        {"chat_id": CHAT, "device_label": issued["device_label"]},
    )
    assert result["revoked"] is True
    assert len(backend.clients) == 0


async def test_revoke_unknown_device_is_bad_request(env):
    svc, _backend, _events = env
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(
            reality_protocol.ACTION_REVOKE, {"chat_id": CHAT, "device_label": "нет такого"}
        )
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_reconcile_adds_missing_and_removes_stray_clients(env):
    svc, backend, _events = env
    issued = await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    email = _email_of(backend)
    # кто-то руками снял юзера из inbound, не трогая БД
    backend.clients.pop(email, None)
    # и добавил чужого, которого в БД нет
    backend.clients["stray@x"] = "stray-uuid"

    await svc.reconcile()

    assert email in backend.clients  # восстановлен
    assert "stray@x" not in backend.clients  # снят
    assert backend.clients[email] == issued["uuid"]


async def test_sampler_accumulates_delta(env):
    svc, backend, _events = env
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    email = _email_of(backend)

    backend.set_traffic(email, up=500, down=1000)
    await svc.sample_once()
    backend.set_traffic(email, up=700, down=1800)
    await svc.sample_once()

    usage = await svc.run_command(reality_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert usage["used_bytes"] == 2500  # (500+1000) + (200+800)


async def test_sampler_treats_negative_delta_as_reset(env):
    svc, backend, _events = env
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    email = _email_of(backend)

    backend.set_traffic(email, up=5000, down=5000)
    await svc.sample_once()
    backend.set_traffic(email, up=50, down=100)  # xray переподняли — счётчики с нуля
    await svc.sample_once()

    usage = await svc.run_command(reality_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert usage["used_bytes"] == 10000 + 150


async def test_sampler_month_rollover_keeps_separate_history(env, monkeypatch):
    import sa_home_bot.reality.service as svc_module

    svc, backend, _events = env
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    email = _email_of(backend)

    backend.set_traffic(email, up=1000, down=0)
    monkeypatch.setattr(svc_module, "_month_key", lambda when: "2026-01")
    await svc.sample_once()

    backend.set_traffic(email, up=1500, down=0)
    monkeypatch.setattr(svc_module, "_month_key", lambda when: "2026-02")
    await svc.sample_once()

    cur = await svc._db.conn.execute(
        "SELECT month, used_bytes FROM reality_peer_usage ORDER BY month"
    )
    rows = {row["month"]: row["used_bytes"] for row in await cur.fetchall()}
    assert rows == {"2026-01": 1000, "2026-02": 500}


async def test_sampler_stamps_last_seen(env):
    svc, backend, _events = env
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    email = _email_of(backend)
    backend.set_traffic(email, up=10, down=20)
    await svc.sample_once()
    usage = await svc.run_command(reality_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert usage["devices"][0]["last_seen_at"] is not None


async def test_quota_exceeded_blocks_peer_and_emits_events(env):
    svc, backend, events = env
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    email = _email_of(backend)

    backend.set_traffic(email, up=GB, down=0)  # ровно лимит (base_quota_gb=1)
    await svc.sample_once()

    assert email not in backend.clients  # снят реконсайлером
    names = [n for n, _ in events]
    assert reality_protocol.EVENT_REALITY_QUOTA_EXCEEDED in names
    assert reality_protocol.EVENT_REALITY_PEER_BLOCKED in names

    usage = await svc.run_command(reality_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert usage["blocked"] is True


async def test_quota_warning_fires_once_then_again_after_grant(env):
    svc, backend, events = env
    svc._cfg.warn_remaining_gb = 1
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    email = _email_of(backend)

    backend.set_traffic(email, up=int(0.5 * GB), down=0)
    await svc.sample_once()
    warnings = [d for n, d in events if n == reality_protocol.EVENT_REALITY_QUOTA_WARNING]
    assert len(warnings) == 1

    backend.set_traffic(email, up=int(0.6 * GB), down=0)
    await svc.sample_once()
    warnings = [d for n, d in events if n == reality_protocol.EVENT_REALITY_QUOTA_WARNING]
    assert len(warnings) == 1

    await svc.run_command(reality_protocol.ACTION_GRANT_EXTRA, {"chat_id": CHAT})
    backend.set_traffic(email, up=int(1.5 * GB), down=0)
    await svc.sample_once()
    warnings = [d for n, d in events if n == reality_protocol.EVENT_REALITY_QUOTA_WARNING]
    assert len(warnings) == 2


async def test_access_restored_event_after_grant_unblocks(env):
    svc, backend, events = env
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    email = _email_of(backend)
    backend.set_traffic(email, up=GB, down=0)
    await svc.sample_once()
    assert email not in backend.clients

    await svc.run_command(reality_protocol.ACTION_GRANT_EXTRA, {"chat_id": CHAT})

    names = [n for n, _ in events]
    assert reality_protocol.EVENT_REALITY_ACCESS_RESTORED in names
    assert email in backend.clients  # реконсайлер вернул


async def test_grant_extra_hits_self_ceiling_then_falls_back_to_request(env):
    svc, _backend, events = env
    svc._cfg.warn_remaining_gb = svc._cfg.self_ceiling_gb
    await svc.run_command(reality_protocol.ACTION_GRANT_EXTRA, {"chat_id": CHAT})
    await svc.run_command(reality_protocol.ACTION_GRANT_EXTRA, {"chat_id": CHAT})
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(reality_protocol.ACTION_GRANT_EXTRA, {"chat_id": CHAT})
    assert excinfo.value.code == reality_protocol.ERR_QUOTA_CEILING

    result = await svc.run_command(
        reality_protocol.ACTION_REQUEST_EXTRA, {"chat_id": CHAT, "bytes": GB}
    )
    assert result["status"] == "pending"
    assert any(n == reality_protocol.EVENT_REALITY_EXTRA_REQUESTED for n, _ in events)


async def test_grant_extra_refused_while_plenty_of_quota_remains(env):
    svc, backend, _events = env
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(reality_protocol.ACTION_GRANT_EXTRA, {"chat_id": CHAT})
    assert excinfo.value.code == ERR_BAD_REQUEST

    email = _email_of(backend)
    backend.set_traffic(email, up=GB, down=0)
    await svc.sample_once()
    result = await svc.run_command(reality_protocol.ACTION_GRANT_EXTRA, {"chat_id": CHAT})
    assert result["limit_bytes"] == 2 * GB


async def test_usage_without_chat_id_includes_node_reserve_and_device_count(env):
    svc, _backend, _events = env
    svc._cfg.node_limit_gb = 10
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": OTHER_CHAT})

    summary = await svc.run_command(reality_protocol.ACTION_USAGE, {})
    by_chat = {row["chat_id"]: row for row in summary["chats"]}
    assert by_chat[CHAT]["device_count"] == 2
    assert by_chat[OTHER_CHAT]["device_count"] == 1
    assert summary["node"]["reserved_bytes"] == 2 * GB
    assert summary["node"]["free_bytes"] == 8 * GB


async def test_usage_without_chat_id_excludes_revoked_guests_from_reserve(env):
    svc, _backend, _events = env
    issued = await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    await svc.run_command(
        reality_protocol.ACTION_REVOKE,
        {"chat_id": CHAT, "device_label": issued["device_label"]},
    )
    summary = await svc.run_command(reality_protocol.ACTION_USAGE, {})
    assert summary["chats"] == []
    assert summary["node"]["reserved_bytes"] == 0


async def test_resolve_request_approve_grants_quota(env):
    svc, _backend, events = env
    req = await svc.run_command(
        reality_protocol.ACTION_REQUEST_EXTRA, {"chat_id": CHAT, "bytes": GB}
    )
    before = await svc.run_command(reality_protocol.ACTION_USAGE, {"chat_id": CHAT})
    result = await svc.run_command(
        reality_protocol.ACTION_RESOLVE_REQUEST,
        {"request_id": req["request_id"], "approve": True},
    )
    assert result["status"] == "approved"
    after = await svc.run_command(reality_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert after["limit_bytes"] == before["limit_bytes"] + GB
    assert any(n == reality_protocol.EVENT_REALITY_EXTRA_RESOLVED for n, _ in events)


async def test_resolve_request_deny_does_not_grant(env):
    svc, _backend, _events = env
    req = await svc.run_command(
        reality_protocol.ACTION_REQUEST_EXTRA, {"chat_id": CHAT, "bytes": GB}
    )
    before = await svc.run_command(reality_protocol.ACTION_USAGE, {"chat_id": CHAT})
    result = await svc.run_command(
        reality_protocol.ACTION_RESOLVE_REQUEST,
        {"request_id": req["request_id"], "approve": False},
    )
    assert result["status"] == "denied"
    after = await svc.run_command(reality_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert after["limit_bytes"] == before["limit_bytes"]


async def test_resolve_request_twice_is_rejected(env):
    svc, _backend, _events = env
    req = await svc.run_command(
        reality_protocol.ACTION_REQUEST_EXTRA, {"chat_id": CHAT, "bytes": GB}
    )
    await svc.run_command(
        reality_protocol.ACTION_RESOLVE_REQUEST,
        {"request_id": req["request_id"], "approve": True},
    )
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(
            reality_protocol.ACTION_RESOLVE_REQUEST,
            {"request_id": req["request_id"], "approve": True},
        )
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_peers_lists_all_chats_for_admin(env):
    svc, _backend, _events = env
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": OTHER_CHAT})
    result = await svc.run_command(reality_protocol.ACTION_PEERS, {})
    chat_ids = {row["chat_id"] for row in result["peers"]}
    assert chat_ids == {CHAT, OTHER_CHAT}


async def test_set_quota_admin_sets_absolute_limit(env):
    svc, _backend, _events = env
    await svc.run_command(reality_protocol.ACTION_SET_QUOTA, {"chat_id": CHAT, "bytes": 5 * GB})
    usage = await svc.run_command(reality_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert usage["limit_bytes"] == 5 * GB


async def test_issue_stamps_own_server(env):
    svc, _backend, _events = env
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    cur = await svc._db.conn.execute("SELECT server FROM reality_peers")
    assert (await cur.fetchone())["server"] == svc._node


async def test_usage_and_peers_report_server(env):
    svc, _backend, _events = env
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    usage = await svc.run_command(reality_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert usage["devices"][0]["server"] == svc._node
    peers = await svc.run_command(reality_protocol.ACTION_PEERS, {})
    assert peers["peers"][0]["server"] == svc._node


async def test_get_state_counts_active_peers(env):
    svc, _backend, _events = env
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    await svc.run_command(reality_protocol.ACTION_ISSUE, {"chat_id": OTHER_CHAT})
    state = await svc.get_state()
    assert state == {"node": svc._node, "service": "reality", "active_peers": 2}
