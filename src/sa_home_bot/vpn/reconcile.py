from __future__ import annotations

from typing import Any

from sa_home_bot.utils.clock import utc_now
from sa_home_bot.vpn import service_common
from sa_home_bot.vpn.protocol import (
    EVENT_VPN_SERVER_RESTORED,
    PROBE_CHAT_ID,
    TRANSPORT_AWG,
    TRANSPORT_REALITY,
)
from sa_home_bot.vpn.service_common import (
    log,
)


class ReconcileMixin:
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
        month = service_common.month_key(utc_now())
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
