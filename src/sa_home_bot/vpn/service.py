from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import random
import socket
import time
import uuid as uuidlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.backup.snapshot import TRIGGER_ACTIONS as SNAPSHOT_TRIGGER_ACTIONS
from sa_home_bot.backup.snapshot import SnapshotBackup
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.domain.vpn_check import (
    ALERTING as CHECK_ALERTING,
)
from sa_home_bot.domain.vpn_check import (
    OK as CHECK_OK,
)
from sa_home_bot.domain.vpn_check import (
    CheckResult,
    KnownCheckState,
    reconcile_vpn_check,
    rollup_status,
)
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ActionParam,
    ActionSpec,
    Address,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.proto.server import undeclared_action
from sa_home_bot.reality.client_config import (
    RealityParams,
    render_deep_link,
    render_singbox_config,
    render_vless_url,
)
from sa_home_bot.reality.xray import XrayBackend
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.vpn import apk as apk_client
from sa_home_bot.vpn import awg_conf, qr
from sa_home_bot.vpn import subscription as subs
from sa_home_bot.vpn.awg import AwgBackend
from sa_home_bot.vpn.protocol import (
    ACTION_APK_CHUNK,
    ACTION_APK_INFO,
    ACTION_APK_SET_FILE_ID,
    ACTION_CHECK_NOW,
    ACTION_CHECK_STATUS,
    ACTION_GET_SUBSCRIPTION,
    ACTION_GET_VLESS,
    ACTION_GRANT_EXTRA,
    ACTION_ISSUE,
    ACTION_PEERS,
    ACTION_PROXY_LINK,
    ACTION_PROXY_ROTATE_SECRET,
    ACTION_PROXY_USAGE,
    ACTION_REISSUE,
    ACTION_REPORT_CHECK,
    ACTION_REQUEST_EXTRA,
    ACTION_RESOLVE_REQUEST,
    ACTION_REVOKE,
    ACTION_SET_ACCESS,
    ACTION_SET_QUOTA,
    ACTION_SUB_GEN,
    ACTION_SUB_LINKS,
    ACTION_TELEGRAM_EGRESS,
    ACTION_USAGE,
    ERR_QUOTA_CEILING,
    EVENT_VPN_ACCESS_RESTORED,
    EVENT_VPN_CHECK_FAILED,
    EVENT_VPN_CHECK_RECOVERED,
    EVENT_VPN_EXTRA_REQUESTED,
    EVENT_VPN_EXTRA_RESOLVED,
    EVENT_VPN_NODE_QUOTA_WARNING,
    EVENT_VPN_PEER_BLOCKED,
    EVENT_VPN_PEER_ISSUED,
    EVENT_VPN_QUOTA_EXCEEDED,
    EVENT_VPN_QUOTA_WARNING,
    EVENT_VPN_SERVER_RESTORED,
    FLOWER_NAMES,
    GB,
    PROBE_CHAT_ID,
    PROXY_SECRET_SEED,
    SERVICE_NAME,
    TELEGRAM_EGRESS_TARGET,
    TELEGRAM_EGRESS_TRANSPORT,
    TRANSPORT_AWG,
    TRANSPORT_REALITY,
    TRANSPORTS,
    country_flag,
)
from sa_home_bot.vpn.proxy_backend import ProxyBackend, RealProxyBackend
from sa_home_bot.vpn.subweb import public_host as sub_public_host
from sa_home_bot.vpn_check import protocol as vpn_check_protocol

log = logging.getLogger(__name__)

MAX_DEVICE_LABEL_LEN = 64

NODE_SERVICE = "node"
ACTION_TRIGGER_PEERS = "trigger_peers"

CHECK_STALE_FACTOR = 3


def _random_device_label(used: set[str]) -> str:
    pool = [name for name in FLOWER_NAMES if name not in used] or list(FLOWER_NAMES)
    return random.choice(pool)


APK_INFO_MEMO_S = 60.0
APK_API_TIMEOUT_S = 15.0
APK_DOWNLOAD_TIMEOUT_S = 90.0
APK_CHUNK_BYTES = 700 * 1024

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


def _month_key(when: datetime) -> str:
    return when.strftime("%Y-%m")


def _allocate_address(subnet: str, used: set[str]) -> str:
    network = ipaddress.ip_network(subnet, strict=False)
    hosts = list(network.hosts())
    for host in hosts[1:]:
        addr = str(host)
        if addr not in used:
            return addr
    raise ProtoError(ERR_BAD_REQUEST, "подсеть VPN исчерпана — нет свободных адресов")


def _reality_email(chat_id: int, device_label: str) -> str:
    return f"c{chat_id}-{device_label}"


class VpnService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        backend: AwgBackend,
        emit: EmitFn,
        *,
        node_link: ServiceLink | None = None,
        proxy_backend: ProxyBackend | None = None,
        reality_backend: XrayBackend | None = None,
    ) -> None:
        self._cfg = settings.vpn
        self._db = db
        self._backend = backend
        self._reality = reality_backend
        self._reality_cfg = settings.vpn.reality
        wanted = list(settings.vpn.transports or [TRANSPORT_AWG])
        self._transports: tuple[str, ...] = tuple(
            t
            for t in wanted
            if t == TRANSPORT_AWG or (t == TRANSPORT_REALITY and reality_backend is not None)
        )
        self._proxy_backend = proxy_backend or RealProxyBackend()
        self._emit = emit
        self._node = socket.gethostname()
        self._server_pubkey: str | None = None
        self._apk_checked_at: datetime | None = None
        self._node_link = node_link
        self.backup: SnapshotBackup | None = None
        self._issue_lock = asyncio.Lock()
        self._node_id = settings.node.id or socket.gethostname()
        self._sub_secret = subs.derive_secret(self._cfg.sub_secret, settings.swarm.token)
        self.sub_web: Any = None
        self._sub_cache: dict[str, tuple[float, subs.Subscription | None]] = {}
        self._sub_stale: dict[tuple[str, str], list[subs.SubEntry]] = {}
        self._peer_nodes_cache: tuple[float, list[str]] | None = None

    def _has(self, transport: str) -> bool:
        return transport in self._transports

    def describe(self) -> ServiceDescription:
        chat_id_param = ActionParam(name="chat_id", type="int", title="Чей это гость")
        device_param = ActionParam(name="device_label", type="string", title="Устройство")
        optional_device_param = ActionParam(
            name="device_label", type="string", required=False, title="Устройство (имя)"
        )
        transport_param = ActionParam(
            name="transport", type="string", required=False, title="Транспорт (awg/reality)"
        )

        capabilities: list[str] = [
            ACTION_PEERS,
            ACTION_ISSUE,
            ACTION_REISSUE,
            ACTION_REVOKE,
            ACTION_USAGE,
            ACTION_SET_QUOTA,
            ACTION_SET_ACCESS,
            ACTION_GRANT_EXTRA,
            ACTION_REQUEST_EXTRA,
            ACTION_RESOLVE_REQUEST,
        ]
        actions: list[ActionSpec] = [
            ActionSpec(id=ACTION_PEERS, title="🔌 Все пиры"),
            ActionSpec(
                id=ACTION_ISSUE,
                title="➕ Выдать доступ",
                params=(chat_id_param, transport_param, optional_device_param),
            ),
            ActionSpec(
                id=ACTION_REISSUE,
                title="🔄 Перевыпустить",
                params=(chat_id_param, device_param, transport_param),
            ),
            ActionSpec(
                id=ACTION_REVOKE,
                title="🚫 Отозвать",
                params=(chat_id_param, device_param, transport_param),
            ),
            ActionSpec(
                id=ACTION_USAGE,
                title="📊 Расход",
                params=(ActionParam(name="chat_id", type="int", required=False, title="Чей"),),
            ),
            ActionSpec(
                id=ACTION_SET_QUOTA,
                title="🎚 Задать квоту",
                params=(
                    chat_id_param,
                    ActionParam(name="bytes", type="int", title="Лимит месяца, байт"),
                ),
            ),
            ActionSpec(
                id=ACTION_SET_ACCESS,
                title="🎟 Допуск на сервер",
                params=(
                    chat_id_param,
                    ActionParam(name="allowed", type="bool", title="Допущен"),
                    ActionParam(
                        name="base_gb",
                        type="int",
                        required=False,
                        title="Личная база, ГБ (пусто — общая)",
                    ),
                ),
            ),
            ActionSpec(id=ACTION_GRANT_EXTRA, title="➕100 ГБ", params=(chat_id_param,)),
            ActionSpec(
                id=ACTION_REQUEST_EXTRA,
                title="✋ Заявка на трафик",
                params=(
                    chat_id_param,
                    ActionParam(name="bytes", type="int", required=False, title="Сколько"),
                ),
            ),
            ActionSpec(
                id=ACTION_RESOLVE_REQUEST,
                title="✅ Решить заявку",
                params=(
                    ActionParam(name="request_id", type="int", title="Номер заявки"),
                    ActionParam(name="approve", type="bool", title="Одобрить"),
                ),
            ),
        ]
        if self._reality_enabled:
            capabilities += [ACTION_GET_VLESS]
            actions += [
                ActionSpec(
                    id=ACTION_GET_VLESS,
                    title="🔗 Ссылка VLESS",
                    params=(chat_id_param, device_param),
                ),
            ]
        if self._reality_enabled:
            capabilities += [ACTION_GET_SUBSCRIPTION, ACTION_SUB_LINKS, ACTION_SUB_GEN]
            actions += [
                ActionSpec(
                    id=ACTION_GET_SUBSCRIPTION,
                    title="🔌 Подписка Hiddify",
                    params=(chat_id_param, device_param),
                ),
                ActionSpec(
                    id=ACTION_SUB_LINKS,
                    title="🔗 VLESS по токену подписки",
                    params=(ActionParam(name="token", type="string", title="Токен"),),
                ),
                ActionSpec(
                    id=ACTION_SUB_GEN,
                    title="🔗 Поколение токена устройства",
                    params=(chat_id_param, device_param),
                ),
            ]
        if self._has(TRANSPORT_AWG):
            capabilities += [ACTION_APK_INFO]
            actions += [
                ActionSpec(id=ACTION_APK_INFO, title="📱 Приложение"),
                ActionSpec(
                    id=ACTION_APK_CHUNK,
                    title="📦 Кусок APK",
                    params=(
                        ActionParam(name="offset", type="int", title="Смещение"),
                        ActionParam(name="length", type="int", required=False, title="Длина"),
                    ),
                ),
                ActionSpec(
                    id=ACTION_APK_SET_FILE_ID,
                    title="🆔 Запомнить file_id",
                    params=(ActionParam(name="telegram_file_id", type="string", title="file_id"),),
                ),
            ]
        capabilities += [ACTION_CHECK_NOW, ACTION_CHECK_STATUS, ACTION_TELEGRAM_EGRESS]
        actions += [
            ActionSpec(
                id=ACTION_REPORT_CHECK,
                title="📡 Отчёт проверки VPN",
                params=(
                    ActionParam(name="node", type="string", title="Нода"),
                    ActionParam(name="results", title="Результаты"),
                ),
            ),
            ActionSpec(id=ACTION_CHECK_NOW, title="🛰 Проверить сеть сейчас"),
            ActionSpec(id=ACTION_CHECK_STATUS, title="🛰 Статус проверок сети"),
            ActionSpec(
                id=ACTION_TELEGRAM_EGRESS,
                title="📡 Маршрут до Telegram через эту ноду",
                params=(ActionParam(name="observer", type="string", title="Нода бота"),),
            ),
        ]
        if self._cfg.mtg_public_host:
            capabilities += [
                ACTION_PROXY_LINK,
                ACTION_PROXY_ROTATE_SECRET,
                ACTION_PROXY_USAGE,
            ]
            actions += [
                ActionSpec(id=ACTION_PROXY_LINK, title="🌐 Ссылка прокси"),
                ActionSpec(id=ACTION_PROXY_ROTATE_SECRET, title="🔁 Сменить секрет прокси"),
                ActionSpec(id=ACTION_PROXY_USAGE, title="📊 Расход прокси"),
            ]
        if self.backup is not None:
            for spec in self.backup.action_specs():
                capabilities.append(spec.id)
                actions.append(spec)
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=tuple(capabilities),
            actions=tuple(actions),
        )

    async def get_state(self) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM vpn_peers WHERE status = 'active'"
        )
        row = await cur.fetchone()
        server_pubkey: str | None = None
        if self._has(TRANSPORT_AWG):
            try:
                server_pubkey = await self._server_public_key()
            except Exception:
                log.warning("vpn: не удалось прочитать публичный ключ awg-сервера", exc_info=True)
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "active_peers": row["n"] if row else 0,
            "label": self._cfg.location,
            "server_public_key": server_pubkey,
            "transports": list(self._transports),
            "check": await self._check_rollup(server=self._node),
            "access_control": True,
        }

    @staticmethod
    def _chat_id(args: dict[str, Any]) -> int:
        raw = args.get("chat_id")
        if raw is None:
            raise ProtoError(ERR_BAD_REQUEST, "не указан chat_id — доступ выдаётся человеку")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ProtoError(ERR_BAD_REQUEST, f"chat_id должен быть числом: {raw!r}") from exc

    async def _server_public_key(self) -> str:
        if self._server_pubkey is None:
            self._server_pubkey = await self._backend.server_public_key()
        return self._server_pubkey

    async def _active_addresses(self) -> set[str]:
        cur = await self._db.conn.execute(
            "SELECT address FROM vpn_peers WHERE status = 'active' AND transport = ?",
            (TRANSPORT_AWG,),
        )
        return {row["address"] for row in await cur.fetchall()}

    async def _blocked_chats(self, month: str) -> set[int]:
        cur = await self._db.conn.execute(
            "SELECT chat_id FROM vpn_quota_state WHERE month = ? AND blocked_at IS NOT NULL",
            (month,),
        )
        return {row["chat_id"] for row in await cur.fetchall()}

    async def _allowed_chats(self) -> set[int]:
        cur = await self._db.conn.execute("SELECT chat_id FROM vpn_chat_access WHERE allowed = 1")
        return {row["chat_id"] for row in await cur.fetchall()} | {PROBE_CHAT_ID}

    async def _quota_state(self, chat_id: int, month: str) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT warned_limit_bytes, blocked_at FROM vpn_quota_state "
            "WHERE chat_id = ? AND month = ?",
            (chat_id, month),
        )
        row = await cur.fetchone()
        if row is None:
            return {"warned_limit_bytes": None, "blocked_at": None}
        return {"warned_limit_bytes": row["warned_limit_bytes"], "blocked_at": row["blocked_at"]}

    async def _set_quota_state(self, chat_id: int, month: str, **updates: Any) -> None:
        current = await self._quota_state(chat_id, month)
        current.update(updates)
        await self._db.conn.execute(
            "INSERT INTO vpn_quota_state (chat_id, month, warned_limit_bytes, blocked_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, month) DO UPDATE SET "
            "warned_limit_bytes = excluded.warned_limit_bytes, blocked_at = excluded.blocked_at",
            (chat_id, month, current["warned_limit_bytes"], current["blocked_at"]),
        )
        await self._db.conn.commit()

    async def _used_bytes(self, chat_id: int, month: str) -> int:
        cur = await self._db.conn.execute(
            "SELECT COALESCE(SUM(u.used_bytes), 0) AS total FROM vpn_peer_usage u "
            "JOIN vpn_peers p ON p.id = u.peer_id WHERE p.chat_id = ? AND u.month = ?",
            (chat_id, month),
        )
        row = await cur.fetchone()
        return int(row["total"] or 0)

    async def _granted_bytes(self, chat_id: int, month: str, *, source: str | None = None) -> int:
        if source is None:
            cur = await self._db.conn.execute(
                "SELECT COALESCE(SUM(bytes), 0) AS total FROM vpn_quota_grants "
                "WHERE chat_id = ? AND month = ?",
                (chat_id, month),
            )
        else:
            cur = await self._db.conn.execute(
                "SELECT COALESCE(SUM(bytes), 0) AS total FROM vpn_quota_grants "
                "WHERE chat_id = ? AND month = ? AND source = ?",
                (chat_id, month, source),
            )
        row = await cur.fetchone()
        return int(row["total"] or 0)

    async def _access(self, chat_id: int) -> tuple[bool, int | None]:
        if chat_id == PROBE_CHAT_ID:
            return True, None
        cur = await self._db.conn.execute(
            "SELECT allowed, base_bytes FROM vpn_chat_access WHERE chat_id = ?", (chat_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return False, None
        return bool(row["allowed"]), row["base_bytes"]

    async def _base_bytes(self, chat_id: int) -> int:
        _allowed, base = await self._access(chat_id)
        return self._cfg.base_quota_gb * GB if base is None else int(base)

    async def _require_access(self, chat_id: int) -> None:
        allowed, _base = await self._access(chat_id)
        if allowed:
            return
        raise ProtoError(
            ERR_BAD_REQUEST,
            f"на сервере «{self._cfg.location or self._node}» доступ не открыт — "
            "его выдаёт владелец в /vpn → «👥 Все гости»",
        )

    async def _limit_bytes(self, chat_id: int, month: str) -> int:
        return await self._base_bytes(chat_id) + await self._granted_bytes(chat_id, month)

    async def _add_grant(
        self, chat_id: int, month: str, bytes_: int, *, source: str, request_id: int | None = None
    ) -> None:
        await self._db.conn.execute(
            "INSERT INTO vpn_quota_grants (chat_id, month, bytes, source, request_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, month, bytes_, source, request_id, utc_now().isoformat()),
        )
        await self._db.conn.commit()

    async def _device_usage(
        self, chat_id: int, month: str, devices: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        cur = await self._db.conn.execute(
            "SELECT p.device_label AS label, COALESCE(p.transport, 'awg') AS transport, "
            "COALESCE(SUM(u.used_bytes), 0) AS used "
            "FROM vpn_peers p JOIN vpn_peer_usage u ON u.peer_id = p.id "
            "WHERE p.chat_id = ? AND u.month = ? GROUP BY p.device_label, 2",
            (chat_id, month),
        )
        by_pair = {(r["label"], r["transport"]): int(r["used"]) for r in await cur.fetchall()}
        result: dict[str, dict[str, Any]] = {}
        for dev in devices:
            label = dev["device_label"]
            transport = dev["transport"]
            entry = result.setdefault(label, {"device_label": label, "connections": []})
            entry["connections"].append(
                {
                    "transport": transport,
                    "status": dev["status"],
                    "last_handshake_at": dev["last_handshake_at"],
                    "created_at": dev["created_at"],
                    "broken": bool(dev.get("broken", False)),
                    "used_bytes": by_pair.get((label, transport), 0),
                }
            )
        for label, entry in result.items():
            entry["used_bytes"] = sum(v for (lbl, _t), v in by_pair.items() if lbl == label)
        return list(result.values())

    async def _peers_for_chat(self, chat_id: int) -> list[dict[str, Any]]:
        cur = await self._db.conn.execute(
            "SELECT device_label, transport, status, created_at, last_handshake_at, server "
            "FROM vpn_peers WHERE chat_id = ? AND status = 'active' ORDER BY created_at",
            (chat_id,),
        )
        return [
            {
                "device_label": row["device_label"],
                "transport": row["transport"] or TRANSPORT_AWG,
                "status": row["status"],
                "created_at": row["created_at"],
                "last_handshake_at": row["last_handshake_at"],
                "server": row["server"] or self._node,
            }
            for row in await cur.fetchall()
        ]

    async def _mark_broken(
        self, chat_id: int, devices: list[dict[str, Any]], *, withheld: bool
    ) -> list[dict[str, Any]]:
        for device in devices:
            device["broken"] = False
        cur = await self._db.conn.execute(
            "SELECT device_label, transport, public_key, address, server_pubkey "
            "FROM vpn_peers WHERE chat_id = ? AND status = 'active'",
            (chat_id,),
        )
        rows = {
            (row["device_label"], row["transport"] or TRANSPORT_AWG): row
            for row in await cur.fetchall()
        }
        try:
            live_awg = (
                set((await self._backend.transfer()).keys()) if self._has(TRANSPORT_AWG) else None
            )
            live_r = await self._reality.list_clients() if self._reality_enabled else None
            live_key = await self._server_public_key() if self._has(TRANSPORT_AWG) else None
        except Exception:
            log.warning("vpn: сверка пиров с сервером не удалась", exc_info=True)
            return devices
        for device in devices:
            row = rows.get((device["device_label"], device.get("transport") or TRANSPORT_AWG))
            if row is None:
                continue
            if (row["transport"] or TRANSPORT_AWG) == TRANSPORT_AWG:
                if live_awg is None:
                    continue
                stale_key = bool(row["server_pubkey"] and live_key) and (
                    row["server_pubkey"] != live_key
                )
                missing = not withheld and row["public_key"] not in live_awg
                device["broken"] = bool(stale_key or missing)
            elif live_r is not None and not withheld:
                device["broken"] = row["address"] not in live_r
        return devices

    async def backfill_server(self) -> None:
        cur = await self._db.conn.execute(
            "UPDATE vpn_peers SET server = ? WHERE server IS NULL", (self._node,)
        )
        if cur.rowcount:
            await self._db.conn.commit()
            log.info("vpn: проставлен server=%s у %d старых пиров", self._node, cur.rowcount)

    async def backfill_access(self) -> None:
        cur = await self._db.conn.execute(
            "INSERT INTO vpn_chat_access (chat_id, allowed, base_bytes, updated_at) "
            "SELECT DISTINCT chat_id, 1, NULL, ? FROM vpn_peers WHERE status = 'active' "
            "ON CONFLICT(chat_id) DO NOTHING",
            (utc_now().isoformat(),),
        )
        if cur.rowcount:
            await self._db.conn.commit()
            log.info("vpn: допуск проставлен %d гостям с активными пирами", cur.rowcount)
        cur = await self._db.conn.execute(
            "SELECT DISTINCT chat_id FROM vpn_peers "
            "WHERE chat_id NOT IN (SELECT chat_id FROM vpn_chat_access)"
        )
        orphans = [row["chat_id"] for row in await cur.fetchall()]
        if orphans:
            log.info("vpn: гости с историей, но без активных пиров — допуск не выдан: %s", orphans)

    async def reconcile(self) -> None:
        month = _month_key(utc_now())
        blocked = await self._blocked_chats(month)
        allowed = await self._allowed_chats()
        cur = await self._db.conn.execute(
            "SELECT chat_id, transport, public_key, address FROM vpn_peers WHERE status = 'active'"
        )
        rows = await cur.fetchall()
        active = [
            row for row in rows if row["chat_id"] in allowed and row["chat_id"] not in blocked
        ]

        if self._has(TRANSPORT_AWG):
            desired = {
                row["public_key"]: row["address"]
                for row in active
                if (row["transport"] or TRANSPORT_AWG) == TRANSPORT_AWG
            }
            current = set((await self._backend.transfer()).keys())
            for pubkey in current - desired.keys():
                await self._backend.remove_peer(pubkey)
            for pubkey, address in desired.items():
                if pubkey not in current:
                    await self._backend.add_peer(pubkey, address)

        if self._reality_enabled:
            desired_r = {
                row["address"]: row["public_key"]
                for row in active
                if row["transport"] == TRANSPORT_REALITY
            }
            current_r = await self._reality.list_clients()
            for email in current_r - desired_r.keys():
                await self._reality.remove_client(email)
            for email, client_uuid in desired_r.items():
                if email not in current_r:
                    await self._reality.add_client(client_uuid, email, self._reality_cfg.flow)

    async def _meta_get(self, key: str) -> str | None:
        cur = await self._db.conn.execute("SELECT value FROM vpn_meta WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def _meta_set(self, key: str, value: str) -> None:
        await self._db.conn.execute(
            "INSERT INTO vpn_meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, utc_now().isoformat()),
        )
        await self._db.conn.commit()

    async def check_restore(self) -> list[dict[str, Any]]:
        fingerprints: dict[str, str] = {}
        try:
            if self._has(TRANSPORT_AWG):
                fingerprints[TRANSPORT_AWG] = await self._server_public_key()
        except Exception:
            log.warning("vpn: сверка ключа awg-сервера после старта не удалась", exc_info=True)
        if self._reality_enabled and self._reality_cfg.server_public_key:
            fingerprints[TRANSPORT_REALITY] = self._reality_cfg.server_public_key
        affected: dict[tuple[int, str, str], None] = {}
        key_changed = False
        for transport, current in fingerprints.items():
            meta_key = f"server_key:{transport}"
            recorded = await self._meta_get(meta_key)
            if recorded == current:
                continue
            key_changed = key_changed or recorded is not None
            cur = await self._db.conn.execute(
                "SELECT chat_id, device_label, server_pubkey FROM vpn_peers "
                "WHERE status = 'active' AND chat_id != ? "
                "AND COALESCE(transport, 'awg') = ? ORDER BY chat_id, created_at",
                (PROBE_CHAT_ID, transport),
            )
            for row in await cur.fetchall():
                stale = bool(row["server_pubkey"]) and row["server_pubkey"] != current
                if stale or recorded is not None:
                    affected[(row["chat_id"], row["device_label"], transport)] = None
            await self._meta_set(meta_key, current)
        items = [
            {"chat_id": c, "device_label": label, "transport": t} for (c, label, t) in affected
        ]
        if items or key_changed:
            log.warning("vpn: ключ сервера сменился, задето подключений: %d", len(items))
            await self._emit(
                EVENT_VPN_SERVER_RESTORED,
                {"node": self._node, "location": self._cfg.location, "affected": items},
            )
        return items

    async def _active_labels(self, chat_id: int) -> set[str]:
        cur = await self._db.conn.execute(
            "SELECT device_label FROM vpn_peers WHERE chat_id = ? AND status = 'active'",
            (chat_id,),
        )
        return {row["device_label"] for row in await cur.fetchall()}

    async def _active_row(self, chat_id: int, label: str, transport: str | None):
        sql = (
            "SELECT id, public_key, address, transport FROM vpn_peers "
            "WHERE chat_id = ? AND device_label = ? AND status = 'active'"
        )
        params: list[Any] = [chat_id, label]
        if transport:
            sql += " AND COALESCE(transport, 'awg') = ?"
            params.append(transport)
        cur = await self._db.conn.execute(sql + " ORDER BY created_at, id", params)
        return await cur.fetchone()

    @staticmethod
    def _optional_transport(args: dict[str, Any]) -> str | None:
        raw = str(args.get("transport") or "").strip().lower()
        if not raw:
            return None
        if raw not in TRANSPORTS:
            raise ProtoError(ERR_BAD_REQUEST, f"неизвестный транспорт {raw!r}")
        return raw

    @staticmethod
    def _device_label(args: dict[str, Any]) -> str:
        return str(args.get("device_label") or "").strip()

    @staticmethod
    def _required_device_label(args: dict[str, Any]) -> str:
        label = VpnService._device_label(args)
        if not label:
            raise ProtoError(ERR_BAD_REQUEST, "не указано устройство (device_label)")
        return label

    @property
    def _reality_enabled(self) -> bool:
        return self._reality is not None and self._reality_cfg is not None

    @staticmethod
    def _explicit_label(args: dict[str, Any]) -> str | None:
        label = VpnService._device_label(args)
        if not label:
            return None
        if len(label) > MAX_DEVICE_LABEL_LEN or any(ord(c) < 32 for c in label):
            raise ProtoError(
                ERR_BAD_REQUEST,
                f"имя устройства не длиннее {MAX_DEVICE_LABEL_LEN} символов и без управляющих",
            )
        return label

    def _resolve_transport(self, args: dict[str, Any]) -> str:
        raw = str(args.get("transport") or "").strip().lower()
        if raw:
            if raw not in self._transports:
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    f"транспорт {raw!r} на сервере {self._node} недоступен "
                    f"(есть: {', '.join(self._transports) or '—'})",
                )
            return raw
        if len(self._transports) == 1:
            return self._transports[0]
        if not self._transports:
            raise ProtoError(
                ERR_BAD_REQUEST, f"на сервере {self._node} нет ни одного транспорта VPN"
            )
        raise ProtoError(
            ERR_BAD_REQUEST,
            f"сервер {self._node} несёт несколько транспортов "
            f"({', '.join(self._transports)}) — укажите transport",
        )

    async def _issue(
        self,
        args: dict[str, Any],
        *,
        forced_label: str | None = None,
        forced_transport: str | None = None,
    ) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        transport = forced_transport or self._resolve_transport(args)
        await self._require_access(chat_id)
        existing_labels = await self._active_labels(chat_id)
        explicit = None if forced_label else self._explicit_label(args)
        device_label = forced_label or explicit or _random_device_label(existing_labels)
        now = utc_now().isoformat()

        self._sub_cache.clear()
        async with self._issue_lock:
            if explicit is not None and await self._active_row(chat_id, explicit, transport):
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    f"устройство «{explicit}» ({transport}) уже выдано — "
                    "для нового ключа используйте перевыпуск",
                )
            artifacts = await self._issue_locked(chat_id, device_label, transport, now)

        await self._emit(
            EVENT_VPN_PEER_ISSUED,
            {
                "chat_id": chat_id,
                "device_label": device_label,
                "transport": transport,
                **self._where(),
            },
        )
        return {
            **artifacts,
            "transport": transport,
            "device_label": device_label,
            "location": self._cfg.location,
            "prior_device_count": len(existing_labels),
        }

    def _reality_artifacts(self, client_uuid: str, device_label: str) -> dict[str, Any]:
        assert self._reality_cfg is not None
        params = self._reality_params()
        config_text = render_singbox_config(params, client_uuid)
        flag = country_flag(self._cfg.location)
        profile_label = f"{flag} {device_label}" if flag else device_label
        share_url = render_vless_url(params, client_uuid, profile_label)
        return {
            "config_text": config_text,
            "share_url": share_url,
            "deep_link": render_deep_link(share_url),
            "qr_png_b64": qr.png_b64(share_url),
        }

    async def _issue_locked(
        self, chat_id: int, device_label: str, transport: str, now: str
    ) -> dict[str, Any]:
        if transport == TRANSPORT_AWG:
            private_key, public_key = await self._backend.generate_keypair()
            address = _allocate_address(self._cfg.subnet, await self._active_addresses())
            server_pub = await self._server_public_key()
            await self._db.conn.execute(
                "INSERT INTO vpn_peers (chat_id, device_label, transport, public_key, address, "
                "status, created_at, server, server_pubkey) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (
                    chat_id,
                    device_label,
                    TRANSPORT_AWG,
                    public_key,
                    address,
                    now,
                    self._node,
                    server_pub,
                ),
            )
            await self._db.conn.commit()
            await self._backend.add_peer(public_key, address)
            conf = awg_conf.render(self._cfg, private_key, address, server_pub)
            artifacts: dict[str, Any] = {
                "config_text": conf,
                "qr_png_b64": qr.png_b64(conf),
                "address": address,
            }
        else:
            assert self._reality_enabled
            client_uuid = str(uuidlib.uuid4())
            email = _reality_email(chat_id, device_label)
            await self._db.conn.execute(
                "INSERT INTO vpn_peers (chat_id, device_label, transport, public_key, address, "
                "status, created_at, server, server_pubkey) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (
                    chat_id,
                    device_label,
                    TRANSPORT_REALITY,
                    client_uuid,
                    email,
                    now,
                    self._node,
                    self._reality_cfg.server_public_key or None,
                ),
            )
            await self._db.conn.commit()
            await self._reality.add_client(client_uuid, email, self._reality_cfg.flow)
            artifacts = self._reality_artifacts(client_uuid, device_label)
        return artifacts

    async def _remove_from_backend(self, transport: str, public_key: str, address: str) -> None:
        if transport == TRANSPORT_REALITY:
            if self._reality is not None:
                await self._reality.remove_client(address)
        else:
            await self._backend.remove_peer(public_key)

    async def _reissue(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        device_label = self._required_device_label(args)
        self._sub_cache.clear()
        await self._require_access(chat_id)
        wanted = self._optional_transport(args)
        row = await self._active_row(chat_id, device_label, wanted)
        transport = row["transport"] or TRANSPORT_AWG if row is not None else None
        if row is not None:
            await self._db.conn.execute(
                "UPDATE vpn_peers SET status = 'expired', revoked_at = ? WHERE public_key = ?",
                (utc_now().isoformat(), row["public_key"]),
            )
            await self._db.conn.commit()
            await self._remove_from_backend(transport, row["public_key"], row["address"])
        try:
            return await self._issue(args, forced_label=device_label, forced_transport=transport)
        finally:
            self._sub_cache.clear()

    async def _revoke(self, args: dict[str, Any]) -> dict[str, Any]:
        self._sub_cache.clear()
        chat_id = self._chat_id(args)
        device_label = self._device_label(args)
        row = await self._active_row(chat_id, device_label, self._optional_transport(args))
        if row is None:
            raise ProtoError(ERR_BAD_REQUEST, f"нет активного устройства «{device_label}»")
        await self._db.conn.execute(
            "UPDATE vpn_peers SET status = 'revoked', revoked_at = ? WHERE public_key = ?",
            (utc_now().isoformat(), row["public_key"]),
        )
        await self._db.conn.commit()
        await self._remove_from_backend(
            row["transport"] or TRANSPORT_AWG, row["public_key"], row["address"]
        )
        return {
            "revoked": True,
            "device_label": device_label,
            "transport": row["transport"] or TRANSPORT_AWG,
        }

    async def _get_vless(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        device_label = self._required_device_label(args)
        if not self._reality_enabled:
            raise ProtoError(ERR_BAD_REQUEST, f"на сервере {self._node} нет VLESS")
        await self._require_access(chat_id)
        row = await self._active_row(chat_id, device_label, TRANSPORT_REALITY)
        if row is None:
            raise ProtoError(ERR_BAD_REQUEST, f"у «{device_label}» нет активного VLESS-подключения")
        return {
            **self._reality_artifacts(row["public_key"], device_label),
            "transport": TRANSPORT_REALITY,
            "device_label": device_label,
            "location": self._cfg.location,
        }

    async def _get_subscription(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        device_label = self._required_device_label(args)
        if not self._reality_enabled:
            raise ProtoError(ERR_BAD_REQUEST, f"на сервере {self._node} нет VLESS")
        if self.sub_web is None or not self.sub_web.listening:
            raise ProtoError(ERR_BAD_REQUEST, f"страница подписки на {self._node} не запущена")
        await self._require_access(chat_id)
        row = await self._active_row(chat_id, device_label, TRANSPORT_REALITY)
        if row is None:
            raise ProtoError(ERR_BAD_REQUEST, f"у «{device_label}» нет активного VLESS-подключения")
        gen = await self._device_gen(chat_id, device_label)
        for peer_gen in await self._peer_gens(chat_id, device_label):
            gen = max(gen, peer_gen)
        token = subs.make_token(self._sub_secret, chat_id, device_label, gen)
        return {
            "page_url": self.sub_web.page_url(token),
            "sub_url": self.sub_web.sub_url(token),
            "singbox_url": self.sub_web.sub_url(token) + "?format=singbox",
            "device_label": device_label,
            "node": self._node_id,
            "https": bool(self.sub_web.tls),
        }

    def _reality_params(self) -> RealityParams:
        assert self._reality_cfg is not None
        return RealityParams.from_config(self._reality_cfg)

    def _sub_entry_name(self) -> str:
        return self._cfg.location or self._node_id

    async def _device_gen(self, chat_id: int, device_label: str) -> int:
        cur = await self._db.conn.execute(
            "SELECT revoked_at FROM vpn_peers WHERE chat_id = ? AND device_label = ? "
            "AND COALESCE(transport, 'awg') = ? AND status != 'active' AND revoked_at IS NOT NULL",
            (chat_id, device_label, TRANSPORT_REALITY),
        )
        best = 0
        for row in await cur.fetchall():
            try:
                best = max(best, int(datetime.fromisoformat(row["revoked_at"]).timestamp()))
            except ValueError:
                continue
        return best

    async def _peer_gens(self, chat_id: int, device_label: str) -> list[int]:
        if self._node_link is None:
            return []
        out: list[int] = []
        for node in await self._sub_peer_nodes():
            try:
                reply = await self._node_link.command(
                    ACTION_SUB_GEN,
                    {"chat_id": chat_id, "device_label": device_label},
                    dst=Address(node=node, service=SERVICE_NAME),
                    timeout=6.0,
                )
                out.append(int(reply.get("gen") or 0))
            except (ServiceUnavailableError, ProtoError, TimeoutError, OSError, ValueError):
                continue
        return out

    async def _sub_local(self, token: str) -> tuple[int, str, str] | None:
        if not self._reality_enabled:
            return None
        cur = await self._db.conn.execute(
            "SELECT chat_id, device_label, public_key FROM vpn_peers "
            "WHERE status = 'active' AND COALESCE(transport, 'awg') = ? AND chat_id != ?",
            (TRANSPORT_REALITY, PROBE_CHAT_ID),
        )
        for row in await cur.fetchall():
            if subs.token_matches(self._sub_secret, token, row["chat_id"], row["device_label"]):
                allowed, _base = await self._access(row["chat_id"])
                if not allowed:
                    return None
                return row["chat_id"], row["device_label"], row["public_key"]
        return None

    def _node_ip(self) -> str:
        if self._reality_cfg is not None and self._reality_cfg.endpoint_host:
            return self._reality_cfg.endpoint_host
        return self._cfg.endpoint_host

    def _sub_base(self) -> str:
        if self._cfg.sub_port <= 0 or self._reality_cfg is None:
            return ""
        return f"https://{sub_public_host(self._cfg)}:{self._cfg.sub_port}"

    async def _sub_health(self) -> str:
        rows = [r for r in await self._check_rollup(server=self._node) if r.get("status")]
        mine = [r for r in rows if r.get("transport") == TRANSPORT_REALITY] or rows
        if not mine:
            return ""
        return "ok" if all(r["status"] == CHECK_OK for r in mine) else "bad"

    async def _sub_links(self, args: dict[str, Any]) -> dict[str, Any]:
        token = str(args.get("token") or "")
        if not subs.valid_token_shape(token):
            return {"found": False}
        base = {
            "node": self._node_id,
            "name": self._sub_entry_name(),
            "ip": self._node_ip(),
            "awg": self._has(TRANSPORT_AWG),
            "base": self._sub_base(),
        }
        found = await self._sub_local(token)
        if found is None:
            return {"found": False, "info": await self._sub_device_info(token, args, base)}
        chat_id, label, client_uuid = found
        month = _month_key(utc_now())
        entry = subs.SubEntry(
            node=self._node_id,
            name=self._sub_entry_name(),
            uuid=client_uuid,
            params=self._reality_params(),
        )
        awg_key = await self._active_row(chat_id, label, TRANSPORT_AWG) is not None
        return {
            "found": True,
            "device_label": label,
            "chat_id": chat_id,
            "gen": await self._device_gen(chat_id, label),
            "entry": entry.to_wire(),
            "info": {
                **base,
                "health": await self._sub_health(),
                "awg_key": awg_key,
                "device": True,
            },
            "used_bytes": await self._used_bytes(chat_id, month),
            "limit_bytes": await self._limit_bytes(chat_id, month),
        }

    async def _sub_device_info(
        self, token: str, args: dict[str, Any], base: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            chat_id = int(args.get("chat_id") or 0)
        except (TypeError, ValueError):
            return base
        label = str(args.get("device_label") or "")
        if (
            not chat_id
            or chat_id == PROBE_CHAT_ID
            or not label
            or not subs.token_matches(self._sub_secret, token, chat_id, label)
        ):
            return base
        allowed, _base = await self._access(chat_id)
        if not allowed:
            return {**base, "awg": False}
        awg_key = await self._active_row(chat_id, label, TRANSPORT_AWG) is not None
        return {**base, "awg_key": awg_key, "device": True}

    async def _sub_peer_nodes(self) -> list[str]:
        if self._node_link is None:
            return []
        now = time.monotonic()
        if self._peer_nodes_cache and now - self._peer_nodes_cache[0] < 60:
            return self._peer_nodes_cache[1]
        from sa_home_bot.bot import vpn_nodes

        nodes = [n for n in await vpn_nodes.live_vpn_nodes(self._node_link) if n != self._node_id]
        self._peer_nodes_cache = (now, nodes)
        return nodes

    async def _sub_ask(self, node: str, args: dict[str, Any]) -> dict[str, Any] | None:
        if node == self._node_id:
            return await self._sub_links(args)
        return await self._sub_ask_peer(node, args)

    async def _sub_ask_peer(self, node: str, args: dict[str, Any]) -> dict[str, Any] | None:
        assert self._node_link is not None
        try:
            return await self._node_link.command(
                ACTION_SUB_LINKS,
                args,
                dst=Address(node=node, service=SERVICE_NAME),
                timeout=6.0,
            )
        except ServiceUnavailableError:
            return None
        except (ProtoError, TimeoutError, OSError):
            return {"found": False}

    async def resolve_subscription(self, token: str) -> subs.Subscription | None:
        if not subs.valid_token_shape(token):
            return None
        now = time.monotonic()
        cached = self._sub_cache.get(token)
        if cached is not None and now - cached[0] < (20 if cached[1] else 30):
            return cached[1]
        entries: list[subs.SubEntry] = []
        nodes: list[subs.NodeInfo] = []
        label = ""
        chat_id = 0
        newest_gen = 0
        used = total = 0
        local = await self._sub_links({"token": token})
        answers: list[tuple[str, dict[str, Any] | None]] = [(self._node_id, local)]
        peers = await self._sub_peer_nodes()
        if peers:
            replies = await asyncio.gather(
                *(self._sub_ask_peer(n, {"token": token}) for n in peers)
            )
            answers += list(zip(peers, replies, strict=True))
        known = next((r for _n, r in answers if r and r.get("found")), None)
        missing = [n for n, r in answers if r is not None and not r.get("found")]
        if known is not None and missing:
            hint = {
                "token": token,
                "chat_id": known.get("chat_id"),
                "device_label": known.get("device_label"),
            }
            again = await asyncio.gather(*(self._sub_ask(n, hint) for n in missing))
            redo = dict(zip(missing, again, strict=True))
            answers = [(n, redo[n] if (redo.get(n) or {}).get("info") else r) for n, r in answers]
        for node, reply in answers:
            if reply is None:
                entries += self._sub_stale.get((token, node), [])
                continue
            try:
                if reply.get("info"):
                    info = subs.NodeInfo.from_wire(reply["info"], local=node == self._node_id)
                    nodes.append(info)
            except (KeyError, TypeError, ValueError):
                pass
            if not reply.get("found"):
                self._sub_stale.pop((token, node), None)
                continue
            try:
                entry = subs.SubEntry.from_wire(reply["entry"])
            except (KeyError, TypeError, ValueError):
                continue
            entries.append(entry)
            self._sub_stale[(token, node)] = [entry]
            label = label or str(reply.get("device_label") or "")
            chat_id = chat_id or int(reply.get("chat_id") or 0)
            newest_gen = max(newest_gen, int(reply.get("gen") or 0))
            used += int(reply.get("used_bytes") or 0)
            total += int(reply.get("limit_bytes") or 0)
        result: subs.Subscription | None = None
        if entries and (subs.token_gen(token) or 0) >= newest_gen:
            if not label:
                label = "устройство"
            nxt = utc_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            nxt = (nxt + timedelta(days=32)).replace(day=1)
            result = subs.Subscription(
                device_label=label,
                entries=tuple(subs.sort_entries(entries)),
                used_bytes=used,
                total_bytes=total,
                expire_ts=int(nxt.timestamp()),
                chat_id=chat_id,
                nodes=tuple(sorted(nodes, key=lambda n: (n.name, n.node))),
            )
        if len(self._sub_cache) > 2000:
            self._sub_cache.clear()
        self._sub_cache[token] = (now, result)
        return result

    async def web_issue_awg(
        self, chat_id: int, device_label: str, node: str, *, replace: bool
    ) -> dict[str, Any]:
        action = ACTION_REISSUE if replace else ACTION_ISSUE
        args = {"chat_id": chat_id, "device_label": device_label, "transport": TRANSPORT_AWG}
        self._sub_cache.clear()
        try:
            if node == self._node_id:
                return await self.run_command(action, args)
            if self._node_link is None:
                raise ProtoError(ERR_BAD_REQUEST, "нода недоступна")
            return await self._node_link.command(
                action, args, dst=Address(node=node, service=SERVICE_NAME), timeout=20.0
            )
        finally:
            self._sub_cache.clear()

    async def _peers(self, _args: dict[str, Any]) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT chat_id, device_label, transport, address, status, created_at, "
            "last_handshake_at, server FROM vpn_peers ORDER BY chat_id, created_at"
        )
        peers = [
            {
                "chat_id": row["chat_id"],
                "device_label": row["device_label"],
                "transport": row["transport"] or TRANSPORT_AWG,
                "address": row["address"],
                "status": row["status"],
                "created_at": row["created_at"],
                "last_handshake_at": row["last_handshake_at"],
                "server": row["server"] or self._node,
            }
            for row in await cur.fetchall()
        ]
        return {"peers": peers}

    async def _usage(self, args: dict[str, Any]) -> dict[str, Any]:
        month = _month_key(utc_now())
        raw_chat_id = args.get("chat_id")
        if raw_chat_id is not None:
            chat_id = self._chat_id(args)
            used = await self._used_bytes(chat_id, month)
            limit = await self._limit_bytes(chat_id, month)
            state = await self._quota_state(chat_id, month)
            allowed, base_bytes = await self._access(chat_id)
            devices = await self._mark_broken(
                chat_id,
                await self._peers_for_chat(chat_id),
                withheld=state["blocked_at"] is not None or not allowed,
            )
            return {
                "chat_id": chat_id,
                "month": month,
                "node": self._node,
                "label": self._cfg.location,
                "used_bytes": used,
                "limit_bytes": limit,
                "remaining_bytes": max(limit - used, 0),
                "blocked": state["blocked_at"] is not None,
                "allowed": allowed,
                "base_limit_bytes": (
                    self._cfg.base_quota_gb * GB if base_bytes is None else int(base_bytes)
                ),
                "personal_base": base_bytes is not None,
                "devices": devices,
                "device_usage": await self._device_usage(chat_id, month, devices),
                "transports": list(self._transports),
                "check": await self._check_rollup(server=self._node),
                "proxy_available": bool(self._cfg.mtg_public_host),
            }
        cur = await self._db.conn.execute(
            "SELECT chat_id FROM vpn_peers WHERE status = 'active' "
            "UNION SELECT chat_id FROM vpn_chat_access WHERE allowed = 1"
        )
        active_chat_ids = [row["chat_id"] for row in await cur.fetchall()]
        chats = []
        reserved_bytes = 0
        for cid in active_chat_ids:
            limit = await self._limit_bytes(cid, month)
            allowed, _base = await self._access(cid)
            chats.append(
                {
                    "chat_id": cid,
                    "used_bytes": await self._used_bytes(cid, month),
                    "limit_bytes": limit,
                    "allowed": allowed,
                    "device_count": len(await self._peers_for_chat(cid)),
                }
            )
            reserved_bytes += limit
        node_limit_bytes = self._cfg.node_limit_gb * GB
        return {
            "month": month,
            "chats": chats,
            "node": {
                "limit_bytes": node_limit_bytes,
                "reserved_bytes": reserved_bytes,
                "free_bytes": max(node_limit_bytes - reserved_bytes, 0),
            },
        }

    async def _set_quota(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        raw_bytes = args.get("bytes")
        if raw_bytes is None:
            raise ProtoError(ERR_BAD_REQUEST, "не указан bytes — целевой лимит месяца")
        target = int(raw_bytes)
        month = _month_key(utc_now())
        current_limit = await self._limit_bytes(chat_id, month)
        delta = target - current_limit
        if delta:
            await self._add_grant(chat_id, month, delta, source="admin")
        await self._check_thresholds(chat_id, month)
        return await self._usage({"chat_id": chat_id})

    async def _set_access(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        if "allowed" not in args:
            raise ProtoError(ERR_BAD_REQUEST, "не указан allowed — допущен ли гость на сервер")
        allowed = bool(args["allowed"])

        _was_allowed, base_bytes = await self._access(chat_id)
        if "base_gb" in args:
            raw = args["base_gb"]
            if raw is None:
                base_bytes = None
            else:
                gb = int(raw)
                if gb <= 0:
                    raise ProtoError(
                        ERR_BAD_REQUEST,
                        "base_gb должен быть больше нуля — чтобы закрыть доступ, передайте "
                        "allowed=false (нулевой лимит читался бы как исчерпанная квота и слал "
                        "бы гостю ⛔️-уведомления)",
                    )
                base_bytes = gb * GB

        await self._db.conn.execute(
            "INSERT INTO vpn_chat_access (chat_id, allowed, base_bytes, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET allowed = excluded.allowed, "
            "base_bytes = excluded.base_bytes, updated_at = excluded.updated_at",
            (chat_id, int(allowed), base_bytes, utc_now().isoformat()),
        )
        await self._db.conn.commit()

        month = _month_key(utc_now())
        await self._check_thresholds(chat_id, month)
        await self.reconcile()
        return await self._usage({"chat_id": chat_id})

    async def _grant_extra(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        month = _month_key(utc_now())
        used = await self._used_bytes(chat_id, month)
        limit = await self._limit_bytes(chat_id, month)
        remaining = limit - used
        threshold = self._cfg.warn_remaining_gb * GB
        if remaining > threshold:
            raise ProtoError(
                ERR_BAD_REQUEST,
                "самообслуживание доступно только когда остаётся меньше "
                f"{self._cfg.warn_remaining_gb} ГБ трафика — сейчас остаётся "
                f"{remaining / GB:.1f} ГБ, попробуйте позже",
            )
        self_granted = await self._granted_bytes(chat_id, month, source="self")
        step = self._cfg.extra_step_gb * GB
        ceiling = self._cfg.self_ceiling_gb * GB
        base = await self._base_bytes(chat_id)
        if base + self_granted + step > ceiling:
            raise ProtoError(
                ERR_QUOTA_CEILING,
                "достигнут потолок самообслуживания — нужна заявка админу",
            )
        await self._add_grant(chat_id, month, step, source="self")
        await self._check_thresholds(chat_id, month)
        return await self._usage({"chat_id": chat_id})

    async def _request_extra(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        raw_bytes = args.get("bytes")
        bytes_ = int(raw_bytes) if raw_bytes is not None else self._cfg.extra_step_gb * GB
        cur = await self._db.conn.execute(
            "INSERT INTO vpn_requests (chat_id, bytes, status, created_at) "
            "VALUES (?, ?, 'pending', ?)",
            (chat_id, bytes_, utc_now().isoformat()),
        )
        await self._db.conn.commit()
        request_id = cur.lastrowid
        await self._emit(
            EVENT_VPN_EXTRA_REQUESTED,
            {"request_id": request_id, "chat_id": chat_id, "bytes": bytes_, **self._where()},
        )
        return {"request_id": request_id, "status": "pending"}

    async def _resolve_request(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_id = args.get("request_id")
        if raw_id is None:
            raise ProtoError(ERR_BAD_REQUEST, "не указан request_id")
        request_id = int(raw_id)
        approve = bool(args.get("approve"))
        cur = await self._db.conn.execute(
            "SELECT chat_id, bytes, status FROM vpn_requests WHERE id = ?", (request_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise ProtoError(ERR_BAD_REQUEST, f"нет заявки №{request_id}")
        if row["status"] != "pending":
            raise ProtoError(ERR_BAD_REQUEST, f"заявка №{request_id} уже решена")
        status = "approved" if approve else "denied"
        await self._db.conn.execute(
            "UPDATE vpn_requests SET status = ?, decided_at = ? WHERE id = ?",
            (status, utc_now().isoformat(), request_id),
        )
        await self._db.conn.commit()
        chat_id = row["chat_id"]
        if approve:
            month = _month_key(utc_now())
            await self._add_grant(
                chat_id, month, row["bytes"], source="admin", request_id=request_id
            )
            await self._check_thresholds(chat_id, month)
        await self._emit(
            EVENT_VPN_EXTRA_RESOLVED,
            {
                "request_id": request_id,
                "chat_id": chat_id,
                "approved": approve,
                "bytes": row["bytes"],
                **self._where(),
            },
        )
        return {"request_id": request_id, "status": status}

    def _where(self) -> dict[str, str]:
        return {"node": self._node, "location": self._cfg.location}

    async def _check_thresholds(self, chat_id: int, month: str) -> None:
        allowed, _base = await self._access(chat_id)
        if not allowed:
            return
        used = await self._used_bytes(chat_id, month)
        limit = await self._limit_bytes(chat_id, month)
        remaining = limit - used
        state = await self._quota_state(chat_id, month)
        warn_threshold = self._cfg.warn_remaining_gb * GB

        if used >= limit:
            if state["blocked_at"] is None:
                await self._set_quota_state(chat_id, month, blocked_at=utc_now().isoformat())
                await self.reconcile()
                await self._emit(EVENT_VPN_QUOTA_EXCEEDED, {"chat_id": chat_id, **self._where()})
                await self._emit(EVENT_VPN_PEER_BLOCKED, {"chat_id": chat_id})
            return
        if state["blocked_at"] is not None:
            await self._set_quota_state(chat_id, month, blocked_at=None)
            await self.reconcile()
            await self._emit(EVENT_VPN_ACCESS_RESTORED, {"chat_id": chat_id, **self._where()})
        if remaining <= warn_threshold and state["warned_limit_bytes"] != limit:
            await self._set_quota_state(chat_id, month, warned_limit_bytes=limit)
            await self._emit(
                EVENT_VPN_QUOTA_WARNING,
                {"chat_id": chat_id, "remaining_bytes": max(remaining, 0), **self._where()},
            )

    async def _check_node_limit(self, month: str) -> None:
        cur = await self._db.conn.execute(
            "SELECT COALESCE(SUM(used_bytes), 0) AS total FROM vpn_peer_usage WHERE month = ?",
            (month,),
        )
        row = await cur.fetchone()
        total = int(row["total"] or 0)
        limit = self._cfg.node_limit_gb * GB
        threshold = limit - self._cfg.warn_remaining_gb * GB
        if total < threshold:
            return
        state = await self._quota_state(PROBE_CHAT_ID, month)
        if state["warned_limit_bytes"] == limit:
            return
        await self._set_quota_state(PROBE_CHAT_ID, month, warned_limit_bytes=limit)
        await self._emit(
            EVENT_VPN_NODE_QUOTA_WARNING,
            {"used_bytes": total, "limit_bytes": limit, **self._where()},
        )

    PROXY_PEER_IDS = {"mtg": -1, "socks": -2}

    async def _proxy_secret(self) -> str:
        cur = await self._db.conn.execute("SELECT mtg_secret FROM proxy_state WHERE id = 1")
        row = await cur.fetchone()
        if row is not None:
            return str(row["mtg_secret"])
        await self._set_proxy_secret(PROXY_SECRET_SEED)
        return PROXY_SECRET_SEED

    async def _set_proxy_secret(self, secret: str) -> None:
        await self._db.conn.execute(
            "INSERT INTO proxy_state (id, mtg_secret, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET mtg_secret = excluded.mtg_secret, "
            "updated_at = excluded.updated_at",
            (secret, utc_now().isoformat()),
        )
        await self._db.conn.commit()

    async def _proxy_link(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self._cfg.mtg_public_host:
            raise ProtoError(
                ERR_BAD_REQUEST,
                "не настроен [vpn].mtg_public_host — впишите публичный IP/домен jeeves",
            )
        secret = await self._proxy_secret()
        query = f"server={self._cfg.mtg_public_host}&port={self._cfg.mtg_port}&secret={secret}"
        tg_link = f"tg://proxy?{query}"
        return {
            "tg_link": tg_link,
            "t_me_link": f"https://t.me/proxy?{query}",
            "qr_png_b64": qr.png_b64(tg_link),
            "host": self._cfg.mtg_public_host,
            "port": self._cfg.mtg_port,
            "secret": secret,
            "socks_host": self._cfg.socks_host,
            "socks_port": self._cfg.socks_port,
            "node": self._node,
            "label": self._cfg.location,
        }

    async def _proxy_rotate_secret(self, args: dict[str, Any]) -> dict[str, Any]:
        new_secret = await self._proxy_backend.generate_secret(self._cfg.mtg_domain)
        await self._proxy_backend.rotate_secret(new_secret)
        await self._set_proxy_secret(new_secret)
        return await self._proxy_link(args)

    async def _proxy_usage(self, args: dict[str, Any]) -> dict[str, Any]:
        month = _month_key(utc_now())
        used: dict[str, int] = {}
        for key, peer_id in self.PROXY_PEER_IDS.items():
            cur = await self._db.conn.execute(
                "SELECT used_bytes FROM vpn_peer_usage WHERE peer_id = ? AND month = ?",
                (peer_id, month),
            )
            row = await cur.fetchone()
            used[key] = int(row["used_bytes"]) if row else 0
        cur = await self._db.conn.execute(
            "SELECT COALESCE(SUM(used_bytes), 0) AS total FROM vpn_peer_usage WHERE month = ?",
            (month,),
        )
        row = await cur.fetchone()
        return {
            "mtg_bytes": used["mtg"],
            "socks_bytes": used["socks"],
            "node_used_bytes": int(row["total"] or 0),
            "node_limit_bytes": self._cfg.node_limit_gb * GB,
        }

    async def _sample_proxy(self, month: str) -> None:
        try:
            counters = await self._proxy_backend.counters()
        except ProtoError:
            return
        now_iso = utc_now().isoformat()
        for key, total in counters.items():
            pubkey = f"proxy-{key}"
            peer_id = self.PROXY_PEER_IDS[key]
            cur = await self._db.conn.execute(
                "SELECT last_rx FROM vpn_counters WHERE public_key = ?", (pubkey,)
            )
            prev = await cur.fetchone()
            delta = total if prev is None else max(total - prev["last_rx"], 0)
            if delta:
                await self._db.conn.execute(
                    "INSERT INTO vpn_peer_usage (peer_id, month, used_bytes) VALUES (?, ?, ?) "
                    "ON CONFLICT(peer_id, month) DO UPDATE SET "
                    "used_bytes = used_bytes + excluded.used_bytes",
                    (peer_id, month, delta),
                )
            await self._db.conn.execute(
                "INSERT INTO vpn_counters (public_key, last_rx, last_tx, updated_at) "
                "VALUES (?, ?, 0, ?) "
                "ON CONFLICT(public_key) DO UPDATE SET "
                "last_rx = excluded.last_rx, updated_at = excluded.updated_at",
                (pubkey, total, now_iso),
            )
        await self._db.conn.commit()

    async def sample_once(self) -> None:
        now = utc_now()
        month = _month_key(now)
        transfer = await self._backend.transfer() if self._has(TRANSPORT_AWG) else {}
        handshakes = await self._backend.latest_handshakes() if self._has(TRANSPORT_AWG) else {}
        reality_stats = await self._reality.stats() if self._reality is not None else {}

        cur = await self._db.conn.execute(
            "SELECT id, chat_id, transport, public_key, address FROM vpn_peers "
            "WHERE status = 'active'"
        )
        rows = await cur.fetchall()
        touched_chats: set[int] = set()
        for row in rows:
            transport = row["transport"] or TRANSPORT_AWG
            counter_key = row["public_key"]
            handshake_ts = 0
            if transport == TRANSPORT_AWG:
                if row["public_key"] not in transfer:
                    continue
                a, b = transfer[row["public_key"]]
                handshake_ts = handshakes.get(row["public_key"], 0)
            else:
                if row["address"] not in reality_stats:
                    continue
                a, b = reality_stats[row["address"]]
            total = a + b
            cur2 = await self._db.conn.execute(
                "SELECT last_rx, last_tx FROM vpn_counters WHERE public_key = ?", (counter_key,)
            )
            prev = await cur2.fetchone()
            if prev is None:
                delta = total
            else:
                prev_total = prev["last_rx"] + prev["last_tx"]
                delta = total - prev_total if total >= prev_total else total
            if delta:
                await self._db.conn.execute(
                    "INSERT INTO vpn_peer_usage (peer_id, month, used_bytes) VALUES (?, ?, ?) "
                    "ON CONFLICT(peer_id, month) DO UPDATE SET "
                    "used_bytes = used_bytes + excluded.used_bytes",
                    (row["id"], month, delta),
                )
                touched_chats.add(row["chat_id"])
                if transport == TRANSPORT_REALITY:
                    await self._db.conn.execute(
                        "UPDATE vpn_peers SET last_handshake_at = ? WHERE id = ?",
                        (now.isoformat(), row["id"]),
                    )
            await self._db.conn.execute(
                "INSERT INTO vpn_counters (public_key, last_rx, last_tx, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(public_key) DO UPDATE SET "
                "last_rx = excluded.last_rx, last_tx = excluded.last_tx, "
                "updated_at = excluded.updated_at",
                (counter_key, a, b, now.isoformat()),
            )
            if handshake_ts:
                await self._db.conn.execute(
                    "UPDATE vpn_peers SET last_handshake_at = ? WHERE id = ?",
                    (datetime.fromtimestamp(handshake_ts, tz=UTC).isoformat(), row["id"]),
                )
        await self._db.conn.commit()
        if self._cfg.mtg_public_host:
            await self._sample_proxy(month)
        for chat_id in touched_chats:
            await self._check_thresholds(chat_id, month)
        await self._check_node_limit(month)
        await self.reconcile()

    async def usage_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.sample_interval_s)
            try:
                await self.sample_once()
            except Exception:
                log.exception("vpn: сбой сэмплера трафика")

    async def _dispatch_checks(self) -> dict[str, Any]:
        if self._node_link is None:
            log.warning("vpn: node_link не настроен — проверки доступности не разосланы")
            return {"dispatched_to": [], "unreachable": [], "skipped": []}
        try:
            result = await self._node_link.command(
                ACTION_TRIGGER_PEERS,
                {
                    "service": vpn_check_protocol.SERVICE_NAME,
                    "action": vpn_check_protocol.ACTION_CHECK,
                    "args": {"server": self._node, "targets": list(self._cfg.check_targets)},
                    "timeout_s": self._cfg.check_dispatch_timeout_s,
                },
                dst=Address(node=self._node, service=NODE_SERVICE),
                timeout=self._cfg.check_dispatch_timeout_s + 3.0,
            )
        except (ServiceUnavailableError, ProtoError, TimeoutError) as exc:
            log.warning("vpn: не удалось разослать проверки доступности: %s", exc)
            return {"dispatched_to": [], "unreachable": [], "skipped": [], "error": str(exc)}
        return {
            "dispatched_to": result.get("dispatched", []),
            "unreachable": result.get("unreachable", []),
            "skipped": result.get("skipped", []),
        }

    async def check_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.check_interval_s)
            try:
                await self._dispatch_checks()
            except Exception:
                log.exception("vpn: сбой цикла проверок доступности")

    async def _report_check(self, args: dict[str, Any]) -> dict[str, Any]:
        node = str(args.get("node", "")).strip()
        results = args.get("results")
        if not node or not isinstance(results, list) or not results:
            raise ProtoError(ERR_BAD_REQUEST, "нужны node и непустой список results")
        now = utc_now()
        runs: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in results:
            if not isinstance(raw, dict):
                continue
            server = str(raw.get("server", "")).strip()
            transport = str(raw.get("transport", "")).strip()
            target = raw.get("target")
            if not server or not transport or not target:
                continue
            ms_raw = raw.get("ms")
            ms = int(ms_raw) if isinstance(ms_raw, int | float) else None
            error_raw = raw.get("error")
            ok = bool(raw.get("ok"))
            error = str(error_raw) if error_raw else None
            run = runs.setdefault(
                (server, transport), {"failed": [], "recovered": [], "total": 0, "bad": 0}
            )
            run["total"] += 1
            run["bad"] += not ok
            to_status = await self._apply_check_result(
                node, server, transport, str(target), ok, ms, error, now
            )
            if to_status == CHECK_ALERTING:
                run["failed"].append({"target": str(target), "error": error})
            elif to_status == CHECK_OK:
                run["recovered"].append(str(target))
        for (server, transport), run in runs.items():
            base = {"node": node, "server": server, "transport": transport, "total": run["total"]}
            if run["failed"]:
                await self._emit(
                    EVENT_VPN_CHECK_FAILED,
                    {
                        **base,
                        "targets": run["failed"],
                        "all_failed": run["bad"] == run["total"],
                        "consecutive": self._cfg.check_fail_threshold,
                    },
                )
            if run["recovered"]:
                await self._emit(
                    EVENT_VPN_CHECK_RECOVERED,
                    {**base, "targets": run["recovered"], "all_ok": run["bad"] == 0},
                )
        return {"accepted": True}

    async def _apply_check_result(
        self,
        node: str,
        server: str,
        transport: str,
        target: str,
        ok: bool,
        latency_ms: int | None,
        error: str | None,
        now: datetime,
    ) -> str | None:
        cur = await self._db.conn.execute(
            "SELECT status, consecutive_count, alerting_since, first_seen_at, "
            "notified_alert_at, notified_cleared_at FROM vpn_check_states "
            "WHERE node = ? AND server = ? AND transport = ? AND target = ?",
            (node, server, transport, target),
        )
        row = await cur.fetchone()
        known = None
        notified_alert_at = None
        notified_cleared_at = None
        first_seen_at = now.isoformat()
        if row is not None:
            alerting_since = (
                datetime.fromisoformat(row["alerting_since"]) if row["alerting_since"] else None
            )
            known = KnownCheckState(
                status=row["status"],
                consecutive_count=row["consecutive_count"],
                alerting_since=alerting_since,
            )
            notified_alert_at = row["notified_alert_at"]
            notified_cleared_at = row["notified_cleared_at"]
            first_seen_at = row["first_seen_at"]

        result = CheckResult(
            node=node,
            server=server,
            transport=transport,
            target=target,
            ok=ok,
            latency_ms=latency_ms,
            error=error,
        )
        state, transition = reconcile_vpn_check(
            result,
            known,
            now,
            fail_threshold=self._cfg.check_fail_threshold,
            clear_threshold=self._cfg.check_clear_threshold,
        )
        if transition is not None and transition.to_status == CHECK_ALERTING:
            notified_alert_at, notified_cleared_at = now.isoformat(), None
        elif transition is not None and transition.to_status == CHECK_OK:
            notified_cleared_at = now.isoformat()

        await self._db.conn.execute(
            "INSERT INTO vpn_check_states ("
            "node, server, transport, target, status, last_ok, last_latency_ms, last_error, "
            "consecutive_count, alerting_since, first_seen_at, last_seen_at, "
            "notified_alert_at, notified_cleared_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(node, server, transport, target) DO UPDATE SET "
            "status = excluded.status, last_ok = excluded.last_ok, "
            "last_latency_ms = excluded.last_latency_ms, last_error = excluded.last_error, "
            "consecutive_count = excluded.consecutive_count, "
            "alerting_since = excluded.alerting_since, last_seen_at = excluded.last_seen_at, "
            "notified_alert_at = excluded.notified_alert_at, "
            "notified_cleared_at = excluded.notified_cleared_at",
            (
                node,
                server,
                transport,
                target,
                state.status,
                int(state.last_ok),
                state.last_latency_ms,
                state.last_error,
                state.consecutive_count,
                state.alerting_since.isoformat() if state.alerting_since else None,
                first_seen_at,
                now.isoformat(),
                notified_alert_at,
                notified_cleared_at,
            ),
        )
        await self._db.conn.commit()
        return transition.to_status if transition is not None else None

    async def _check_status(self) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT node, server, transport, target, status, last_ok, last_latency_ms, "
            "last_error, consecutive_count, alerting_since, last_seen_at FROM vpn_check_states "
            "ORDER BY server, transport, node, target"
        )
        rows = await cur.fetchall()
        states = [
            {
                "node": r["node"],
                "server": r["server"],
                "transport": r["transport"],
                "target": r["target"],
                "status": r["status"],
                "last_ok": bool(r["last_ok"]),
                "last_latency_ms": r["last_latency_ms"],
                "last_error": r["last_error"],
                "consecutive_count": r["consecutive_count"],
                "alerting_since": r["alerting_since"],
                "last_seen_at": r["last_seen_at"],
            }
            for r in rows
        ]
        return {"states": states, "rollup": await self._check_rollup()}

    async def _telegram_egress(self, args: dict[str, Any]) -> dict[str, Any]:
        observer = str(args.get("observer") or "").strip()
        if not observer:
            raise ProtoError(ERR_BAD_REQUEST, "не передан observer")
        cur = await self._db.conn.execute(
            "SELECT last_ok, last_latency_ms, last_seen_at FROM vpn_check_states "
            "WHERE node = ? AND server = ? AND transport = ? AND target = ?",
            (observer, self._node, TELEGRAM_EGRESS_TRANSPORT, TELEGRAM_EGRESS_TARGET),
        )
        row = await cur.fetchone()
        check: dict[str, Any] | None = None
        if row is not None:
            stale_before = (
                utc_now() - timedelta(seconds=self._cfg.check_interval_s * CHECK_STALE_FACTOR)
            ).isoformat()
            check = {
                "ok": bool(row["last_ok"]),
                "ms": row["last_latency_ms"],
                "seen_at": row["last_seen_at"],
                "stale": row["last_seen_at"] < stale_before,
            }
        socks_host = self._cfg.socks_host
        return {
            "node": self._node,
            "label": self._cfg.location,
            "socks": f"{socks_host}:{self._cfg.socks_port}" if socks_host else None,
            "check": check,
        }

    async def _check_rollup(self, *, server: str | None = None) -> list[dict[str, Any]]:
        stale_before = (
            utc_now() - timedelta(seconds=self._cfg.check_interval_s * CHECK_STALE_FACTOR)
        ).isoformat()
        sql = "SELECT node, server, transport, status FROM vpn_check_states WHERE last_seen_at >= ?"
        params: list[Any] = [stale_before]
        if server is not None:
            sql += " AND server = ?"
            params.append(server)
        cur = await self._db.conn.execute(sql, params)
        by_pair: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for row in await cur.fetchall():
            by_pair.setdefault((row["server"], row["transport"]), []).append(
                (row["node"], row["status"])
            )
        return [
            {
                "server": pair_server,
                "transport": transport,
                "status": rollup_status([status for _node, status in rows]),
                "observers": len({node for node, _status in rows}),
            }
            for (pair_server, transport), rows in sorted(by_pair.items())
        ]

    async def _apk_row(self) -> dict[str, Any] | None:
        cur = await self._db.conn.execute(
            "SELECT version, file_name, size, sha256, path, telegram_file_id, checked_at "
            "FROM vpn_apk WHERE id = 1"
        )
        row = await cur.fetchone()
        if row is None or row["path"] is None:
            return None
        return {
            "version": row["version"],
            "file_name": row["file_name"],
            "size": row["size"],
            "sha256": row["sha256"],
            "telegram_file_id": row["telegram_file_id"],
            "checked_at": row["checked_at"],
        }

    async def _download_apk(self, tag: str, asset: dict[str, Any]) -> None:
        url = str(asset.get("browser_download_url") or "")
        if not url:
            raise ProtoError(ERR_INTERNAL, "у релиза нет ссылки на APK")
        name = str(asset.get("name") or "amneziawg.apk")
        expected_size = int(asset.get("size") or 0)
        expected_sha = apk_client.asset_sha256(asset)

        cache_dir = self._cfg.apk_cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        dest = cache_dir / name
        tmp = cache_dir / f".{name}.part"
        await asyncio.to_thread(apk_client.download_sync, url, tmp, APK_DOWNLOAD_TIMEOUT_S)
        actual_size = tmp.stat().st_size
        if expected_size and actual_size != expected_size:
            tmp.unlink(missing_ok=True)
            raise ProtoError(
                ERR_INTERNAL,
                f"скачанный APK не совпал по размеру ({actual_size} != {expected_size})",
            )
        actual_sha = await asyncio.to_thread(apk_client.sha256_file, tmp)
        if expected_sha and expected_sha != actual_sha:
            tmp.unlink(missing_ok=True)
            raise ProtoError(ERR_INTERNAL, "скачанный APK не совпал по sha256")
        tmp.replace(dest)
        now = utc_now().isoformat()
        await self._db.conn.execute(
            "INSERT INTO vpn_apk (id, version, file_name, size, sha256, path, "
            "telegram_file_id, checked_at, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, NULL, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET version = excluded.version, "
            "file_name = excluded.file_name, size = excluded.size, sha256 = excluded.sha256, "
            "path = excluded.path, telegram_file_id = NULL, checked_at = excluded.checked_at, "
            "updated_at = excluded.updated_at",
            (tag, name, actual_size, actual_sha, str(dest), now, now),
        )
        await self._db.conn.commit()

    async def _apk_info(self, _args: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        if (
            self._apk_checked_at is not None
            and (now - self._apk_checked_at).total_seconds() < APK_INFO_MEMO_S
        ):
            row = await self._apk_row()
            if row is not None:
                return {**row, "stale": False}
        try:
            release = await asyncio.to_thread(
                apk_client.latest_release_sync, self._cfg.apk_repo, APK_API_TIMEOUT_S
            )
        except apk_client.ApkFetchError:
            row = await self._apk_row()
            if row is None:
                raise ProtoError(
                    ERR_INTERNAL, "APK ещё ни разу не скачан, а GitHub сейчас недоступен"
                ) from None
            return {**row, "stale": True}
        self._apk_checked_at = now
        tag = str(release.get("tag_name") or "")
        asset = apk_client.pick_apk_asset(release.get("assets") or [])
        if asset is None:
            row = await self._apk_row()
            if row is None:
                raise ProtoError(ERR_INTERNAL, "в последнем релизе не нашлось APK")
            return {**row, "stale": True}
        row = await self._apk_row()
        if row is not None and row.get("version") == tag and row.get("size") == asset.get("size"):
            return {**row, "stale": False}
        await self._download_apk(tag, asset)
        row = await self._apk_row()
        assert row is not None
        return {**row, "stale": False}

    async def _apk_chunk(self, args: dict[str, Any]) -> dict[str, Any]:
        offset = int(args.get("offset") or 0)
        length = int(args.get("length") or APK_CHUNK_BYTES)
        cur = await self._db.conn.execute("SELECT path, sha256, size FROM vpn_apk WHERE id = 1")
        row = await cur.fetchone()
        if row is None or row["path"] is None:
            raise ProtoError(ERR_BAD_REQUEST, "APK ещё не скачан — сначала apk_info")
        data = await asyncio.to_thread(apk_client.read_chunk, Path(row["path"]), offset, length)
        eof = offset + len(data) >= int(row["size"] or 0)
        return {
            "data_b64": base64.b64encode(data).decode(),
            "offset": offset,
            "eof": eof,
            "sha256": row["sha256"],
        }

    async def _apk_set_file_id(self, args: dict[str, Any]) -> dict[str, Any]:
        file_id = str(args.get("telegram_file_id") or "").strip()
        if not file_id:
            raise ProtoError(ERR_BAD_REQUEST, "не передан telegram_file_id")
        await self._db.conn.execute(
            "UPDATE vpn_apk SET telegram_file_id = ? WHERE id = 1", (file_id,)
        )
        await self._db.conn.commit()
        return {"ok": True}

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.backup is not None:
            own = await self.backup.handle(action, args)
            if own is not None:
                return own
        result = await self._run_command(action, args)
        if self.backup is not None and action in SNAPSHOT_TRIGGER_ACTIONS:
            self.backup.touch()
        return result

    async def _run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_PEERS:
            return await self._peers(args)
        if action == ACTION_ISSUE:
            return await self._issue(args)
        if action == ACTION_REISSUE:
            return await self._reissue(args)
        if action == ACTION_REVOKE:
            return await self._revoke(args)
        if action == ACTION_GET_VLESS:
            return await self._get_vless(args)
        if action == ACTION_GET_SUBSCRIPTION:
            return await self._get_subscription(args)
        if action == ACTION_SUB_LINKS:
            return await self._sub_links(args)
        if action == ACTION_SUB_GEN:
            label = self._device_label(args)
            return {"gen": await self._device_gen(self._chat_id(args), label)}
        if action == ACTION_USAGE:
            return await self._usage(args)
        if action == ACTION_SET_QUOTA:
            return await self._set_quota(args)
        if action == ACTION_SET_ACCESS:
            return await self._set_access(args)
        if action == ACTION_GRANT_EXTRA:
            return await self._grant_extra(args)
        if action == ACTION_REQUEST_EXTRA:
            return await self._request_extra(args)
        if action == ACTION_RESOLVE_REQUEST:
            return await self._resolve_request(args)
        if action == ACTION_APK_INFO:
            return await self._apk_info(args)
        if action == ACTION_APK_CHUNK:
            return await self._apk_chunk(args)
        if action == ACTION_APK_SET_FILE_ID:
            return await self._apk_set_file_id(args)
        if action == ACTION_REPORT_CHECK:
            return await self._report_check(args)
        if action == ACTION_CHECK_NOW:
            return await self._dispatch_checks()
        if action == ACTION_CHECK_STATUS:
            return await self._check_status()
        if action == ACTION_TELEGRAM_EGRESS:
            return await self._telegram_egress(args)
        if action == ACTION_PROXY_LINK:
            return await self._proxy_link(args)
        if action == ACTION_PROXY_ROTATE_SECRET:
            return await self._proxy_rotate_secret(args)
        if action == ACTION_PROXY_USAGE:
            return await self._proxy_usage(args)
        raise undeclared_action(action)
