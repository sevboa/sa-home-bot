from __future__ import annotations

from typing import Any

from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ProtoError,
)
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.vpn import qr, service_common
from sa_home_bot.vpn.protocol import (
    GB,
    PROXY_SECRET_SEED,
)


class ProxyMixin:
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
        month = service_common.month_key(utc_now())
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
