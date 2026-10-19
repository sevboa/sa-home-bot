from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import datetime, timedelta
from typing import Any

from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.domain.vpn_check import (
    OK as CHECK_OK,
)
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    Address,
    ProtoError,
)
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.vpn import service_common
from sa_home_bot.vpn import subscription as subs
from sa_home_bot.vpn.protocol import (
    ACTION_ISSUE,
    ACTION_REISSUE,
    ACTION_SUB_GEN,
    ACTION_SUB_LINKS,
    PROBE_CHAT_ID,
    SERVICE_NAME,
    TRANSPORT_AWG,
    TRANSPORT_REALITY,
)
from sa_home_bot.vpn.subweb import public_host as sub_public_host


class SubscriptionLinksMixin:
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
        month = service_common.month_key(utc_now())
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
                stale = self._sub_stale_info.get((token, node))
                if stale is not None:
                    nodes.append(dataclasses.replace(stale, health="", awg=False, awg_key=False))
                continue
            try:
                if reply.get("info"):
                    info = subs.NodeInfo.from_wire(reply["info"], local=node == self._node_id)
                    nodes.append(info)
                    self._sub_stale_info[(token, node)] = info
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
        if len(self._sub_stale_info) > 4000:
            self._sub_stale_info.clear()
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
