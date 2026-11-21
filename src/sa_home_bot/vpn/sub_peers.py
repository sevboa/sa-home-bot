from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

from sa_home_bot.utils.clock import utc_now
from sa_home_bot.vpn import awg_conf
from sa_home_bot.vpn import subscription as subs
from sa_home_bot.vpn.protocol import (
    PROBE_CHAT_ID,
    PURPOSE_APP,
    PURPOSE_SUB,
    TRANSPORT_AWG,
)
from sa_home_bot.vpn.service_common import _allocate_address, log

AWG_VERSION_1 = "1.0"
AWG_VERSION_2 = "2.0"


@dataclass(frozen=True)
class AwgVersion:
    number: str
    cfg: Any
    subnet: str


class SubPeersMixin:
    def _awg_versions(self) -> list[AwgVersion]:
        versions = [AwgVersion(AWG_VERSION_1, self._cfg, self._cfg.subnet)]
        if self._cfg.awg_alt is not None:
            versions.append(
                AwgVersion(AWG_VERSION_2, self._cfg.awg_alt_render_cfg(), self._cfg.awg_alt.subnet)
            )
        return versions

    def _awg_version_of(self, address: str) -> str:
        alt = self._cfg.awg_alt
        if alt is not None and ipaddress.ip_interface(address).ip in ipaddress.ip_network(
            alt.subnet, strict=False
        ):
            return AWG_VERSION_2
        return AWG_VERSION_1

    async def _sub_peer_list(self, chat_id: int, device_label: str) -> list[Any]:
        cur = await self._db.conn.execute(
            "SELECT id, public_key, address, private_key, server_pubkey FROM vpn_peers "
            "WHERE chat_id = ? AND device_label = ? AND purpose = ? AND status = 'active' "
            "ORDER BY id",
            (chat_id, device_label, PURPOSE_SUB),
        )
        return list(await cur.fetchall())

    async def _sub_peer_rows(self, chat_id: int, device_label: str) -> dict[str, Any]:
        rows: dict[str, Any] = {}
        for row in await self._sub_peer_list(chat_id, device_label):
            rows.setdefault(self._awg_version_of(row["address"]), row)
        return rows

    async def _sub_peer_usable(self, row: Any) -> bool:
        if not row["private_key"]:
            return False
        if not row["server_pubkey"]:
            return True
        return row["server_pubkey"] == await self._server_public_key_for_address(row["address"])

    async def _sub_peers_complete(self, rows: dict[str, Any], versions: list[AwgVersion]) -> bool:
        for version in versions:
            row = rows.get(version.number)
            if row is None or not await self._sub_peer_usable(row):
                return False
        return True

    async def _has_active_app_peer(self, chat_id: int, device_label: str) -> bool:
        cur = await self._db.conn.execute(
            "SELECT 1 FROM vpn_peers WHERE chat_id = ? AND device_label = ? "
            "AND purpose = ? AND status = 'active' LIMIT 1",
            (chat_id, device_label, PURPOSE_APP),
        )
        return await cur.fetchone() is not None

    async def _drop_sub_peer(self, row: Any) -> None:
        await self._db.conn.execute(
            "UPDATE vpn_peers SET status = 'revoked', revoked_at = ? WHERE id = ?",
            (utc_now().isoformat(), row["id"]),
        )
        await self._db.conn.commit()
        await self._backend.remove_peer(row["public_key"])

    async def _create_sub_peer(self, chat_id: int, device_label: str, version: AwgVersion) -> None:
        private_key, public_key = await self._backend.generate_keypair()
        address = _allocate_address(version.subnet, await self._active_addresses())
        server_pub = await self._server_public_key_for_address(address)
        await self._db.conn.execute(
            "INSERT INTO vpn_peers (chat_id, device_label, transport, public_key, address, "
            "status, created_at, server, server_pubkey, purpose, private_key) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (
                chat_id,
                device_label,
                TRANSPORT_AWG,
                public_key,
                address,
                utc_now().isoformat(),
                self._node,
                server_pub,
                PURPOSE_SUB,
                private_key,
            ),
        )
        await self._db.conn.commit()
        await self._backend.add_peer(public_key, address)

    async def _ensure_sub_peers(
        self, chat_id: int, device_label: str, versions: list[AwgVersion]
    ) -> dict[str, Any]:
        rows = await self._sub_peer_rows(chat_id, device_label)
        if await self._sub_peers_complete(rows, versions):
            return rows
        async with self._issue_lock:
            if not await self._has_active_app_peer(chat_id, device_label):
                return {}
            rows = await self._sub_peer_rows(chat_id, device_label)
            for version in versions:
                row = rows.get(version.number)
                if row is not None and not await self._sub_peer_usable(row):
                    await self._drop_sub_peer(row)
                    row = None
                if row is None:
                    await self._create_sub_peer(chat_id, device_label, version)
            return await self._sub_peer_rows(chat_id, device_label)

    def _awg_sub_entry(self, version: AwgVersion, row: Any) -> subs.AwgSubEntry:
        cfg = version.cfg
        return subs.AwgSubEntry(
            node=self._node_id,
            name=self._sub_awg_name(version.number),
            version=version.number,
            private_key=row["private_key"],
            address=row["address"],
            server_public_key=row["server_pubkey"],
            endpoint_host=cfg.endpoint_host,
            port=int(cfg.endpoint_port),
            dns=cfg.dns,
            mtu=int(cfg.mtu),
            obfuscation=tuple(
                (key.lower(), str(getattr(cfg, key.lower()))) for key in awg_conf.issued_keys(cfg)
            ),
        )

    async def _sub_awg_entries(self, chat_id: int, device_label: str) -> list[subs.AwgSubEntry]:
        if chat_id == PROBE_CHAT_ID or not self._has(TRANSPORT_AWG):
            return []
        versions = self._awg_versions()
        try:
            rows = await self._ensure_sub_peers(chat_id, device_label, versions)
        except Exception:
            log.warning("vpn: не удалось выдать пиры подписки устройству")
            rows = await self._sub_peer_rows(chat_id, device_label)
        return [
            self._awg_sub_entry(version, rows[version.number])
            for version in versions
            if version.number in rows and await self._sub_peer_usable(rows[version.number])
        ]

    async def _revoke_orphan_sub_peers(self, chat_id: int, device_label: str) -> None:
        async with self._issue_lock:
            if await self._has_active_app_peer(chat_id, device_label):
                return
            for row in await self._sub_peer_list(chat_id, device_label):
                await self._drop_sub_peer(row)
