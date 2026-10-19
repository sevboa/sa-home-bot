from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from sa_home_bot.proto.messages import (
    ProtoError,
)
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.vpn import service_common
from sa_home_bot.vpn.protocol import (
    GB,
    TRANSPORT_AWG,
    TRANSPORT_REALITY,
)
from sa_home_bot.vpn.service_common import (
    log,
)

ONLINE_WINDOW_S = 300


def _stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


class UsageMixin:
    async def _live(self, fetch, rows: list, transport: str) -> dict:
        if not any((row["transport"] or TRANSPORT_AWG) == transport for row in rows):
            return {}
        try:
            return await fetch()
        except Exception:
            log.warning("vpn: живой опрос %s для проверки устройства не удался", transport)
            return {}

    async def _device_online(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        label = self._required_device_label(args)
        cur = await self._db.conn.execute(
            "SELECT p.transport, p.public_key, p.address, p.last_handshake_at, "
            "c.last_rx, c.last_tx FROM vpn_peers p "
            "LEFT JOIN vpn_counters c ON c.public_key = p.public_key "
            "WHERE p.chat_id = ? AND p.device_label = ? AND p.status = 'active'",
            (chat_id, label),
        )
        rows = await cur.fetchall()
        handshakes = (
            await self._live(self._backend.latest_handshakes, rows, TRANSPORT_AWG)
            if self._has(TRANSPORT_AWG)
            else {}
        )
        stats = (
            await self._live(self._reality.stats, rows, TRANSPORT_REALITY)
            if self._reality is not None
            else {}
        )
        now = utc_now()
        connections = []
        for row in rows:
            transport = row["transport"] or TRANSPORT_AWG
            seen = _stamp(row["last_handshake_at"])
            if transport == TRANSPORT_AWG:
                ts = handshakes.get(row["public_key"], 0)
                if ts:
                    live = datetime.fromtimestamp(ts, tz=UTC)
                    seen = max(seen, live) if seen else live
            elif row["address"] in stats:
                a, b = stats[row["address"]]
                if row["last_rx"] is not None and a + b > row["last_rx"] + row["last_tx"]:
                    seen = now
            online = seen is not None and (now - seen).total_seconds() <= ONLINE_WINDOW_S
            connections.append(
                {
                    "transport": transport,
                    "online": online,
                    "seen_at": seen.isoformat() if seen else None,
                }
            )
        return {
            "node": self._node,
            "label": self._cfg.location,
            "device_label": label,
            "connections": connections,
        }

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

    async def _usage(self, args: dict[str, Any]) -> dict[str, Any]:
        month = service_common.month_key(utc_now())
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
        month = service_common.month_key(now)
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
