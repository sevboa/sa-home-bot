from __future__ import annotations

import uuid as uuidlib
from typing import Any

from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ProtoError,
)
from sa_home_bot.reality.client_config import (
    render_deep_link,
    render_singbox_config,
    render_vless_url,
)
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.vpn import awg_conf, qr
from sa_home_bot.vpn.protocol import (
    EVENT_VPN_PEER_ISSUED,
    TRANSPORT_AWG,
    TRANSPORT_REALITY,
    TRANSPORTS,
    country_flag,
)
from sa_home_bot.vpn.service_common import (
    MAX_DEVICE_LABEL_LEN,
    _allocate_address,
    _random_device_label,
    _reality_email,
    log,
)


class PeersMixin:
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
        label = PeersMixin._device_label(args)
        if not label:
            raise ProtoError(ERR_BAD_REQUEST, "не указано устройство (device_label)")
        return label

    @property
    def _reality_enabled(self) -> bool:
        return self._reality is not None and self._reality_cfg is not None

    @staticmethod
    def _explicit_label(args: dict[str, Any]) -> str | None:
        label = PeersMixin._device_label(args)
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
