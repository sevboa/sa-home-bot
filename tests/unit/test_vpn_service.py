"""Служба vpn: сэмплер, реконсайлер, issue/reissue/revoke, квоты — поверх
подменяемого AwgBackend (настоящий `awg` в тестах не зовём)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from sa_home_bot.config import RealityTransportConfig, Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn.protocol import TRANSPORT_AWG, TRANSPORT_REALITY
from sa_home_bot.vpn.service import _FLOWER_NAMES, GB, VpnService, _random_device_label

CHAT = 111
OTHER_CHAT = 222


class FakeAwg:
    """AwgBackend в памяти: pubkey → (address, rx, tx); ключи детерминированы."""

    def __init__(self) -> None:
        self.peers: dict[str, str] = {}  # pubkey -> address (то, что реально на интерфейсе)
        self.transfer_bytes: dict[str, tuple[int, int]] = {}
        self.handshakes: dict[str, int] = {}
        self._keygen = 0
        self.server_pub = "server-pubkey"

    async def server_public_key(self) -> str:
        return self.server_pub

    async def add_peer(self, public_key: str, address: str) -> None:
        self.peers[public_key] = address
        self.transfer_bytes.setdefault(public_key, (0, 0))

    async def remove_peer(self, public_key: str) -> None:
        self.peers.pop(public_key, None)

    async def transfer(self) -> dict[str, tuple[int, int]]:
        return {pk: self.transfer_bytes.get(pk, (0, 0)) for pk in self.peers}

    async def latest_handshakes(self) -> dict[str, int]:
        return dict(self.handshakes)

    async def generate_keypair(self) -> tuple[str, str]:
        self._keygen += 1
        return f"private-{self._keygen}", f"public-{self._keygen}"

    def set_traffic(self, public_key: str, rx: int, tx: int) -> None:
        self.transfer_bytes[public_key] = (rx, tx)


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)
    backend = FakeAwg()
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    cfg = VpnConfig(
        subnet="10.9.0.0/29",  # маленькая подсеть: .1 сервер, .2-.6 хосты — удобно для лимита
        base_quota_gb=1,  # 1 ГБ — быстрые пороги в тестах, не 500
        extra_step_gb=1,
        self_ceiling_gb=3,
        warn_remaining_gb=0,  # переопределяется по тестам, где нужно
        endpoint_host="203.0.113.9",
    )
    svc = VpnService(Settings(vpn=cfg), db, backend, emit)
    yield svc, backend, events
    await db.close()


async def test_issue_creates_peer_and_config(env):
    svc, backend, events = env
    result = await svc.run_command(
        vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "телефон"}
    )
    assert "PrivateKey" in result["config_text"]
    assert "server-pubkey" in result["config_text"]
    assert "203.0.113.9:51820" in result["config_text"]
    assert result["qr_png_b64"]  # непустой PNG в base64
    assert result["address"].startswith("10.9.0.")
    assert backend.peers  # реально добавлен на интерфейс
    assert any(name == vpn_protocol.EVENT_VPN_PEER_ISSUED for name, _ in events)


async def test_private_key_never_stored_in_db(env):
    svc, backend, _events = env
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "тел"})
    cur = await svc._db.conn.execute("SELECT * FROM vpn_peers")
    rows = await cur.fetchall()
    assert len(rows) == 1
    row_keys = rows[0].keys()
    assert "public_key" in row_keys
    assert "private_key" not in row_keys
    # публичный ключ хранится, но никакой приватный материал не проскочил
    # даже в address/device_label.
    assert "private" not in rows[0]["public_key"]


def test_random_device_label_avoids_active_names_when_possible():
    used = set(_FLOWER_NAMES[:-1])
    assert _random_device_label(used) == _FLOWER_NAMES[-1]


def test_random_device_label_falls_back_to_full_pool_when_exhausted():
    assert _random_device_label(set(_FLOWER_NAMES)) in _FLOWER_NAMES


async def test_issue_assigns_random_english_label_ignoring_manual_input(env):
    svc, _backend, _events = env
    result = await svc.run_command(
        vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "Мой телефон"}
    )
    # Имя больше не вводится вручную (решение 2026-08-04) — служба сама
    # выбирает случайное английское слово, любой переданный device_label
    # игнорируется.
    assert result["device_label"] in _FLOWER_NAMES


async def test_issue_reports_prior_device_count(env):
    svc, _backend, _events = env
    # bot/handlers/vpn.py и bot/tools.py::tool_vpn выбирают порядок
    # файл/QR по этому полю (решение пользователя 2026-08-04): 0 у первого
    # устройства чата, дальше — растёт с каждой новой выдачей.
    first = await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    assert first["prior_device_count"] == 0
    second = await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    assert second["prior_device_count"] == 1


async def test_issue_has_no_device_cap(env):
    svc, _backend, _events = env
    # Старый max_peers_per_chat=2 остановил бы это на третьем устройстве;
    # подсеть фикстуры (/29) вмещает 5 хостов — весь пул, без отдельного
    # лимита числа устройств.
    for label in ("a", "b", "c", "d", "e"):
        await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": label})
    result = await svc.run_command(vpn_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert len(result["devices"]) == 5


async def test_reissue_expires_old_peer_and_keeps_label(env):
    svc, backend, _events = env
    first = await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    label = first["device_label"]  # имя случайное (решение 2026-08-04) — берём фактическое
    assert len(backend.peers) == 1
    old_address = first["address"]

    second = await svc.run_command(
        vpn_protocol.ACTION_REISSUE, {"chat_id": CHAT, "device_label": label}
    )
    # старый пир снят с интерфейса, на интерфейсе ровно один (новый) пир
    assert len(backend.peers) == 1
    assert second["config_text"] != first["config_text"]
    assert second["device_label"] == label  # перевыпуск не меняет имя устройства

    cur = await svc._db.conn.execute(
        "SELECT status FROM vpn_peers WHERE address = ?", (old_address,)
    )
    assert (await cur.fetchone())["status"] == "expired"


async def test_revoke_removes_peer_from_interface(env):
    svc, backend, _events = env
    issued = await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    assert len(backend.peers) == 1
    result = await svc.run_command(
        vpn_protocol.ACTION_REVOKE, {"chat_id": CHAT, "device_label": issued["device_label"]}
    )
    assert result["revoked"] is True
    assert len(backend.peers) == 0


async def test_revoke_unknown_device_is_bad_request(env):
    svc, _backend, _events = env
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(
            vpn_protocol.ACTION_REVOKE, {"chat_id": CHAT, "device_label": "нет такого"}
        )
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_reconcile_adds_missing_and_removes_stray_peers(env):
    svc, backend, _events = env
    issued = await svc.run_command(
        vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "тел"}
    )
    # кто-то руками снял пира с интерфейса, не трогая БД.
    cur = await svc._db.conn.execute("SELECT public_key FROM vpn_peers")
    pubkey = (await cur.fetchone())["public_key"]
    backend.peers.pop(pubkey, None)
    # и добавил чужого, которого в БД нет вовсе.
    backend.peers["stray"] = "10.9.0.9"

    await svc.reconcile()

    assert pubkey in backend.peers  # восстановлен
    assert "stray" not in backend.peers  # снят
    assert issued["address"] == backend.peers[pubkey]


async def test_sampler_accumulates_delta(env):
    svc, backend, _events = env
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "тел"})
    pubkey = next(iter(backend.peers))

    backend.set_traffic(pubkey, rx=1000, tx=500)
    await svc.sample_once()
    backend.set_traffic(pubkey, rx=1800, tx=700)
    await svc.sample_once()

    usage = await svc.run_command(vpn_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert usage["used_bytes"] == 2500  # (1000+500) + (800+200)


async def test_sampler_treats_negative_delta_as_reset(env):
    """Переподнятый интерфейс обнуляет счётчики `awg show transfer` —
    отрицательная дельта не должна уходить в минус, а считается от нуля."""
    svc, backend, _events = env
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "тел"})
    pubkey = next(iter(backend.peers))

    backend.set_traffic(pubkey, rx=5000, tx=5000)
    await svc.sample_once()
    # интерфейс переподняли — счётчики обнулились и снова растут с нуля.
    backend.set_traffic(pubkey, rx=100, tx=50)
    await svc.sample_once()

    usage = await svc.run_command(vpn_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert usage["used_bytes"] == 10000 + 150  # первый тик + второй (не минус)


async def test_sampler_month_rollover_keeps_separate_history(env, monkeypatch):
    import sa_home_bot.vpn.service as svc_module

    svc, backend, _events = env
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "тел"})
    pubkey = next(iter(backend.peers))

    backend.set_traffic(pubkey, rx=1000, tx=0)
    monkeypatch.setattr(svc_module, "_month_key", lambda when: "2026-01")
    await svc.sample_once()

    backend.set_traffic(pubkey, rx=1500, tx=0)
    monkeypatch.setattr(svc_module, "_month_key", lambda when: "2026-02")
    await svc.sample_once()

    cur = await svc._db.conn.execute(
        "SELECT month, used_bytes FROM vpn_peer_usage ORDER BY month"
    )
    rows = {row["month"]: row["used_bytes"] for row in await cur.fetchall()}
    assert rows == {"2026-01": 1000, "2026-02": 500}


async def test_quota_exceeded_blocks_peer_and_emits_events(env):
    svc, backend, events = env
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "тел"})
    pubkey = next(iter(backend.peers))

    backend.set_traffic(pubkey, rx=GB, tx=0)  # ровно лимит (base_quota_gb=1)
    await svc.sample_once()

    assert pubkey not in backend.peers  # снят реконсайлером
    names = [n for n, _ in events]
    assert vpn_protocol.EVENT_VPN_QUOTA_EXCEEDED in names
    assert vpn_protocol.EVENT_VPN_PEER_BLOCKED in names

    usage = await svc.run_command(vpn_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert usage["blocked"] is True


async def test_quota_warning_fires_once_then_again_after_grant(env):
    svc, backend, events = env
    cfg = svc._cfg
    cfg.warn_remaining_gb = 1  # предупреждать за 1 ГБ до исчерпания
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "тел"})
    pubkey = next(iter(backend.peers))

    # лимит — 1 ГБ (base_quota_gb=1), remaining=0 после 1 ГБ трафика — уже
    # в зоне предупреждения (0 <= warn_remaining_gb=1 ГБ), но ниже 100%.
    backend.set_traffic(pubkey, rx=int(0.5 * GB), tx=0)
    await svc.sample_once()
    warnings = [d for n, d in events if n == vpn_protocol.EVENT_VPN_QUOTA_WARNING]
    assert len(warnings) == 1

    # повторный тик без изменения лимита — предупреждение не дублируется.
    backend.set_traffic(pubkey, rx=int(0.6 * GB), tx=0)
    await svc.sample_once()
    warnings = [d for n, d in events if n == vpn_protocol.EVENT_VPN_QUOTA_WARNING]
    assert len(warnings) == 1

    # гость добавил +1 ГБ — лимит вырос, предупреждение может сработать снова.
    await svc.run_command(vpn_protocol.ACTION_GRANT_EXTRA, {"chat_id": CHAT})
    backend.set_traffic(pubkey, rx=int(1.5 * GB), tx=0)
    await svc.sample_once()
    warnings = [d for n, d in events if n == vpn_protocol.EVENT_VPN_QUOTA_WARNING]
    assert len(warnings) == 2


async def test_access_restored_event_after_grant_unblocks(env):
    svc, backend, events = env
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "тел"})
    pubkey = next(iter(backend.peers))
    backend.set_traffic(pubkey, rx=GB, tx=0)
    await svc.sample_once()
    assert pubkey not in backend.peers

    await svc.run_command(vpn_protocol.ACTION_GRANT_EXTRA, {"chat_id": CHAT})

    names = [n for n, _ in events]
    assert vpn_protocol.EVENT_VPN_ACCESS_RESTORED in names
    # реконсайлер внутри check_thresholds уже вернул пира на интерфейс.
    assert pubkey in backend.peers


async def test_grant_extra_hits_self_ceiling_then_falls_back_to_request(env):
    svc, _backend, events = env
    # Тест — про потолок self_ceiling_gb, не про порог "трафик заканчивается"
    # (см. test_grant_extra_refused_while_plenty_of_quota_remains): снимаем
    # второй гейт заведомо большим порогом, чтобы гость был "низким по
    # трафику" при любом остатке в пределах self_ceiling_gb.
    svc._cfg.warn_remaining_gb = svc._cfg.self_ceiling_gb
    # self_ceiling_gb=3, base_quota_gb=1, extra_step_gb=1 → после двух self
    # грантов (1+1+1=3) третий должен упереться в потолок.
    await svc.run_command(vpn_protocol.ACTION_GRANT_EXTRA, {"chat_id": CHAT})
    await svc.run_command(vpn_protocol.ACTION_GRANT_EXTRA, {"chat_id": CHAT})
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(vpn_protocol.ACTION_GRANT_EXTRA, {"chat_id": CHAT})
    assert excinfo.value.code == vpn_protocol.ERR_QUOTA_CEILING

    # фолбэк — заявка админу.
    result = await svc.run_command(
        vpn_protocol.ACTION_REQUEST_EXTRA, {"chat_id": CHAT, "bytes": GB}
    )
    assert result["status"] == "pending"
    assert any(n == vpn_protocol.EVENT_VPN_EXTRA_REQUESTED for n, _ in events)


async def test_grant_extra_refused_while_plenty_of_quota_remains(env):
    svc, backend, _events = env
    # warn_remaining_gb=0 (дефолт фикстуры) — самообслуживание закрыто,
    # пока остаток НЕ достиг этого порога.
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "тел"})
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(vpn_protocol.ACTION_GRANT_EXTRA, {"chat_id": CHAT})
    assert excinfo.value.code == ERR_BAD_REQUEST

    # трафик исчерпан (remaining=0) — теперь самообслуживание открыто.
    pubkey = next(iter(backend.peers))
    backend.set_traffic(pubkey, rx=GB, tx=0)  # ровно лимит (base_quota_gb=1)
    await svc.sample_once()
    result = await svc.run_command(vpn_protocol.ACTION_GRANT_EXTRA, {"chat_id": CHAT})
    assert result["limit_bytes"] == 2 * GB


async def test_usage_without_chat_id_includes_node_reserve_and_device_count(env):
    svc, _backend, _events = env
    svc._cfg.node_limit_gb = 10
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "a"})
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "b"})
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": OTHER_CHAT, "device_label": "c"})

    summary = await svc.run_command(vpn_protocol.ACTION_USAGE, {})
    by_chat = {row["chat_id"]: row for row in summary["chats"]}
    assert by_chat[CHAT]["device_count"] == 2
    assert by_chat[OTHER_CHAT]["device_count"] == 1
    # base_quota_gb=1 ГБ на гостя, два активных гостя → резерв 2 ГБ из 10.
    assert summary["node"]["reserved_bytes"] == 2 * GB
    assert summary["node"]["free_bytes"] == 8 * GB


async def test_usage_without_chat_id_excludes_revoked_guests_from_reserve(env):
    svc, _backend, _events = env
    issued = await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    await svc.run_command(
        vpn_protocol.ACTION_REVOKE, {"chat_id": CHAT, "device_label": issued["device_label"]}
    )
    summary = await svc.run_command(vpn_protocol.ACTION_USAGE, {})
    assert summary["chats"] == []
    assert summary["node"]["reserved_bytes"] == 0


async def test_resolve_request_approve_grants_quota(env):
    svc, _backend, events = env
    req = await svc.run_command(
        vpn_protocol.ACTION_REQUEST_EXTRA, {"chat_id": CHAT, "bytes": GB}
    )
    before = await svc.run_command(vpn_protocol.ACTION_USAGE, {"chat_id": CHAT})

    result = await svc.run_command(
        vpn_protocol.ACTION_RESOLVE_REQUEST,
        {"request_id": req["request_id"], "approve": True},
    )
    assert result["status"] == "approved"
    after = await svc.run_command(vpn_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert after["limit_bytes"] == before["limit_bytes"] + GB
    assert any(n == vpn_protocol.EVENT_VPN_EXTRA_RESOLVED for n, _ in events)


async def test_resolve_request_deny_does_not_grant(env):
    svc, _backend, _events = env
    req = await svc.run_command(
        vpn_protocol.ACTION_REQUEST_EXTRA, {"chat_id": CHAT, "bytes": GB}
    )
    before = await svc.run_command(vpn_protocol.ACTION_USAGE, {"chat_id": CHAT})
    result = await svc.run_command(
        vpn_protocol.ACTION_RESOLVE_REQUEST,
        {"request_id": req["request_id"], "approve": False},
    )
    assert result["status"] == "denied"
    after = await svc.run_command(vpn_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert after["limit_bytes"] == before["limit_bytes"]


async def test_resolve_request_twice_is_rejected(env):
    svc, _backend, _events = env
    req = await svc.run_command(
        vpn_protocol.ACTION_REQUEST_EXTRA, {"chat_id": CHAT, "bytes": GB}
    )
    await svc.run_command(
        vpn_protocol.ACTION_RESOLVE_REQUEST, {"request_id": req["request_id"], "approve": True}
    )
    with pytest.raises(ProtoError) as excinfo:
        await svc.run_command(
            vpn_protocol.ACTION_RESOLVE_REQUEST,
            {"request_id": req["request_id"], "approve": True},
        )
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_usage_without_chat_id_summarizes_all_chats_for_admin(env):
    svc, backend, _events = env
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "a"})
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": OTHER_CHAT, "device_label": "b"})
    pubkeys = list(backend.peers)
    backend.set_traffic(pubkeys[0], rx=100, tx=0)
    backend.set_traffic(pubkeys[1], rx=200, tx=0)
    await svc.sample_once()

    summary = await svc.run_command(vpn_protocol.ACTION_USAGE, {})
    by_chat = {row["chat_id"]: row["used_bytes"] for row in summary["chats"]}
    assert by_chat[CHAT] == 100
    assert by_chat[OTHER_CHAT] == 200


async def test_peers_lists_all_chats_for_admin(env):
    svc, _backend, _events = env
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "a"})
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": OTHER_CHAT, "device_label": "b"})
    result = await svc.run_command(vpn_protocol.ACTION_PEERS, {})
    chat_ids = {row["chat_id"] for row in result["peers"]}
    assert chat_ids == {CHAT, OTHER_CHAT}


async def test_set_quota_admin_sets_absolute_limit(env):
    svc, _backend, _events = env
    await svc.run_command(
        vpn_protocol.ACTION_SET_QUOTA, {"chat_id": CHAT, "bytes": 5 * GB}
    )
    usage = await svc.run_command(vpn_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert usage["limit_bytes"] == 5 * GB


async def test_issue_stamps_own_server(env):
    """Этап 39: каждый инстанс vpn помечает выданный пир своим [node].id —
    по этому полю бот раскладывает подключения по локациям."""
    svc, _backend, _events = env
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    cur = await svc._db.conn.execute("SELECT server FROM vpn_peers")
    assert (await cur.fetchone())["server"] == svc._node


async def test_usage_and_peers_report_server(env):
    svc, _backend, _events = env
    await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    usage = await svc.run_command(vpn_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert usage["devices"][0]["server"] == svc._node
    peers = await svc.run_command(vpn_protocol.ACTION_PEERS, {})
    assert peers["peers"][0]["server"] == svc._node


async def test_backfill_server_fills_pre_stage39_peers(env):
    """Пиры без server (выданы до этапа 39) — все на этой ноде, разово
    проставляются её именем; уже заполненные не трогаются."""
    svc, _backend, _events = env
    await svc._db.conn.execute(
        "INSERT INTO vpn_peers (chat_id, device_label, public_key, address, status, "
        "created_at, server) VALUES (?, 'old', 'pk-old', '10.9.0.2', 'active', '2026-01-01', NULL)",
        (CHAT,),
    )
    await svc._db.conn.execute(
        "INSERT INTO vpn_peers (chat_id, device_label, public_key, address, status, "
        "created_at, server) VALUES (?, 'kept', 'pk-kept', '10.9.0.3', 'active', "
        "'2026-01-01', 'wooster')",
        (OTHER_CHAT,),
    )
    await svc._db.conn.commit()
    await svc.backfill_server()
    cur = await svc._db.conn.execute("SELECT public_key, server FROM vpn_peers ORDER BY public_key")
    got = {row["public_key"]: row["server"] for row in await cur.fetchall()}
    assert got == {"pk-old": svc._node, "pk-kept": "wooster"}


# ---------------------------------------------------------------------------
# Второй транспорт — VLESS+Reality через xray (подэтап 39.0.x). Общая квота с
# AmneziaWG: трафик обоих транспортов гостя суммируется против одного лимита.
# ---------------------------------------------------------------------------


class FakeXray:
    """XrayBackend в памяти: email → uuid (кто в inbound) + email → (up, down)."""

    def __init__(self) -> None:
        self.clients: dict[str, str] = {}
        self.traffic: dict[str, tuple[int, int]] = {}

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
async def env_both(tmp_path):
    """Нода с ОБОИМИ транспортами (awg + reality)."""
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)
    awg = FakeAwg()
    xray = FakeXray()
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    cfg = VpnConfig(
        subnet="10.9.0.0/29",
        base_quota_gb=1,
        extra_step_gb=1,
        self_ceiling_gb=3,
        warn_remaining_gb=0,
        endpoint_host="203.0.113.9",
        transports=[TRANSPORT_AWG, TRANSPORT_REALITY],
        reality=RealityTransportConfig(
            endpoint_host="198.51.100.7",
            server_public_key="SRV_PUB",
            short_id="abcd1234",
            sni="www.google.com",
        ),
    )
    svc = VpnService(Settings(vpn=cfg), db, awg, emit, reality_backend=xray)
    yield svc, awg, xray, events
    await db.close()


async def _issue_reality(svc, chat_id=CHAT):
    return await svc.run_command(
        vpn_protocol.ACTION_ISSUE, {"chat_id": chat_id, "transport": TRANSPORT_REALITY}
    )


async def test_reality_issue_creates_peer_and_singbox_artifacts(env_both):
    svc, _awg, xray, events = env_both
    result = await _issue_reality(svc)
    assert result["transport"] == TRANSPORT_REALITY
    assert '"type": "vless"' in result["config_text"]
    assert "SRV_PUB" in result["config_text"]
    assert result["share_url"].startswith("vless://")
    assert "198.51.100.7:8443" in result["share_url"]
    assert result["deep_link"].startswith("hiddify://import/")
    assert result["qr_png_b64"]
    assert xray.clients  # реально добавлен в inbound
    cur = await svc._db.conn.execute(
        "SELECT transport, public_key, address FROM vpn_peers WHERE chat_id = ?", (CHAT,)
    )
    row = await cur.fetchone()
    assert row["transport"] == TRANSPORT_REALITY
    assert row["address"] == f"c{CHAT}-{result['device_label']}"  # email в колонке address
    assert row["public_key"] == list(xray.clients.values())[0]  # uuid


async def test_transport_must_be_specified_when_node_carries_both(env_both):
    svc, _awg, _xray, _events = env_both
    with pytest.raises(ProtoError) as exc:
        await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    assert exc.value.code == ERR_BAD_REQUEST


async def test_unknown_transport_is_rejected(env_both):
    svc, _awg, _xray, _events = env_both
    with pytest.raises(ProtoError) as exc:
        await svc.run_command(
            vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "transport": "wireguard"}
        )
    assert exc.value.code == ERR_BAD_REQUEST


async def test_single_transport_node_needs_no_transport_arg(env):
    """awg-only нода (дефолтный env) — transport можно не указывать."""
    svc, _backend, _events = env
    result = await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    assert result["transport"] == TRANSPORT_AWG


async def test_reality_only_node_hides_awg_actions_in_describe(tmp_path):
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)

    async def emit(*_a):
        return None

    cfg = VpnConfig(
        transports=[TRANSPORT_REALITY],
        reality=RealityTransportConfig(server_public_key="P", short_id="s", endpoint_host="h"),
    )
    svc = VpnService(Settings(vpn=cfg), db, FakeAwg(), emit, reality_backend=FakeXray())
    caps = set(svc.describe().capabilities)
    assert vpn_protocol.ACTION_ISSUE in caps
    assert vpn_protocol.ACTION_PROXY_LINK not in caps
    assert vpn_protocol.ACTION_APK_INFO not in caps
    assert svc.describe().info  # smoke
    state = await svc.get_state()
    assert state["transports"] == [TRANSPORT_REALITY]
    await db.close()


async def test_reality_transport_missing_config_is_disabled(tmp_path):
    """transport reality в списке, но нет секции [vpn.reality] → недоступен."""
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)

    async def emit(*_a):
        return None

    cfg = VpnConfig(transports=[TRANSPORT_REALITY])  # reality=None
    svc = VpnService(Settings(vpn=cfg), db, FakeAwg(), emit, reality_backend=None)
    assert (await svc.get_state())["transports"] == []
    with pytest.raises(ProtoError):
        await svc.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT})
    await db.close()


async def test_reality_reconcile_adds_missing_and_removes_stray(env_both):
    svc, _awg, xray, _events = env_both
    result = await _issue_reality(svc)
    email = f"c{CHAT}-{result['device_label']}"
    # xray потерял юзера (рестарт) + завёлся лишний вручную
    xray.clients.clear()
    xray.clients["stray@x"] = "u-stray"
    await svc.reconcile()
    assert set(xray.clients) == {email}


async def test_reality_sampler_counts_into_shared_quota(env_both):
    svc, awg, xray, _events = env_both
    # awg-устройство
    awg_res = await svc.run_command(
        vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "transport": TRANSPORT_AWG}
    )
    awg_pk = next(iter(awg.peers))
    awg.set_traffic(awg_pk, 200_000_000, 100_000_000)  # 0.3 ГБ
    # reality-устройство того же гостя
    r_res = await _issue_reality(svc)
    email = f"c{CHAT}-{r_res['device_label']}"
    xray.set_traffic(email, 250_000_000, 150_000_000)  # 0.4 ГБ

    await svc.sample_once()

    usage = await svc.run_command(vpn_protocol.ACTION_USAGE, {"chat_id": CHAT})
    assert usage["used_bytes"] == 700_000_000  # 0.3 + 0.4 против ОДНОГО лимита
    assert usage["limit_bytes"] == GB
    labels = {d["device_label"]: d["transport"] for d in usage["devices"]}
    assert labels == {
        awg_res["device_label"]: TRANSPORT_AWG,
        r_res["device_label"]: TRANSPORT_REALITY,
    }


async def test_reality_reissue_keeps_transport_and_swaps_client(env_both):
    svc, _awg, xray, _events = env_both
    first = await _issue_reality(svc)
    label = first["device_label"]
    old_email = f"c{CHAT}-{label}"
    old_uuid = xray.clients[old_email]

    second = await svc.run_command(
        vpn_protocol.ACTION_REISSUE, {"chat_id": CHAT, "device_label": label}
    )
    assert second["transport"] == TRANSPORT_REALITY
    assert second["device_label"] == label
    # тот же email (label не меняется), но новый uuid; старый снят
    assert set(xray.clients) == {old_email}
    assert xray.clients[old_email] != old_uuid
    cur = await svc._db.conn.execute(
        "SELECT status FROM vpn_peers WHERE public_key = ?", (old_uuid,)
    )
    assert (await cur.fetchone())["status"] == "expired"


async def test_reality_revoke_removes_client_from_xray(env_both):
    svc, _awg, xray, _events = env_both
    result = await _issue_reality(svc)
    label = result["device_label"]
    await svc.run_command(vpn_protocol.ACTION_REVOKE, {"chat_id": CHAT, "device_label": label})
    assert xray.clients == {}
    cur = await svc._db.conn.execute(
        "SELECT status FROM vpn_peers WHERE chat_id = ? AND device_label = ?", (CHAT, label)
    )
    assert (await cur.fetchone())["status"] == "revoked"


async def test_reality_quota_block_reconciles_both_transports(env_both):
    svc, awg, xray, events = env_both
    awg_res = await svc.run_command(
        vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "transport": TRANSPORT_AWG}
    )
    await _issue_reality(svc)
    awg_pk = next(iter(awg.peers))
    awg.set_traffic(awg_pk, GB, 0)  # ровно лимит 1 ГБ awg-трафиком

    await svc.sample_once()

    # оба транспорта гостя сняты с серверов (общая блокировка по квоте)
    assert xray.clients == {}
    assert awg.peers == {}
    assert any(n == vpn_protocol.EVENT_VPN_QUOTA_EXCEEDED for n, _ in events)
    _ = awg_res
