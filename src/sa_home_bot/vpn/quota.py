from __future__ import annotations

from typing import Any

from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ProtoError,
)
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.vpn import service_common
from sa_home_bot.vpn.protocol import (
    ERR_QUOTA_CEILING,
    EVENT_VPN_ACCESS_RESTORED,
    EVENT_VPN_EXTRA_REQUESTED,
    EVENT_VPN_EXTRA_RESOLVED,
    EVENT_VPN_NODE_QUOTA_WARNING,
    EVENT_VPN_PEER_BLOCKED,
    EVENT_VPN_QUOTA_EXCEEDED,
    EVENT_VPN_QUOTA_WARNING,
    GB,
    PROBE_CHAT_ID,
)


class QuotaMixin:
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

    async def _set_quota(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        raw_bytes = args.get("bytes")
        if raw_bytes is None:
            raise ProtoError(ERR_BAD_REQUEST, "не указан bytes — целевой лимит месяца")
        target = int(raw_bytes)
        month = service_common.month_key(utc_now())
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

        month = service_common.month_key(utc_now())
        await self._check_thresholds(chat_id, month)
        await self.reconcile()
        return await self._usage({"chat_id": chat_id})

    async def _grant_extra(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        month = service_common.month_key(utc_now())
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
            month = service_common.month_key(utc_now())
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
