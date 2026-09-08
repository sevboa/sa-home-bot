"""RealityService — ServiceHandler службы ``reality`` (VLESS+Reality доступ
через xray-core на ноде роя с белым IP, выдаваемый и учитываемый через бота).

Устроена по образцу ``vpn/service.py``: реконсайлер (а не разрозненные
add/remove), приватный учёт (только объёмы, без адресов назначения),
самообслуживание квот с потолком и заявками админу. Отличия от ``vpn``:

* транспорт — не ключ WireGuard + адрес в подсети, а UUID клиента xray;
  бэкенд (``reality/xray.py``) ходит в gRPC API xray без sudo и без рестарта;
* учёт трафика — из ``xray api statsquery`` (per-user uplink/downlink),
  дельта-модель та же, что у ``awg show transfer``;
* маршрутизация сплит-туннеля целиком на клиенте (Hiddify тянет remote
  rule-set сам) — сервер только раздаёт выход, geoip/geosite ему не нужны;
* прокси (mtg/microsocks), APK и пробы доступности (vpn_check) в MVP не
  переносятся.

Деньги/трафик — в ГБ = 10^9 байт (десятичные, как у провайдеров), в БД байты.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import random
import socket
import uuid as uuidlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.reality.client_config import render_deep_link, render_vless_url
from sa_home_bot.reality.client_config import render_singbox_config as _render_singbox
from sa_home_bot.reality.protocol import (
    ACTION_GRANT_EXTRA,
    ACTION_ISSUE,
    ACTION_PEERS,
    ACTION_REISSUE,
    ACTION_REQUEST_EXTRA,
    ACTION_RESOLVE_REQUEST,
    ACTION_REVOKE,
    ACTION_SET_QUOTA,
    ACTION_USAGE,
    ERR_QUOTA_CEILING,
    EVENT_REALITY_ACCESS_RESTORED,
    EVENT_REALITY_EXTRA_REQUESTED,
    EVENT_REALITY_EXTRA_RESOLVED,
    EVENT_REALITY_NODE_QUOTA_WARNING,
    EVENT_REALITY_PEER_BLOCKED,
    EVENT_REALITY_PEER_ISSUED,
    EVENT_REALITY_QUOTA_EXCEEDED,
    EVENT_REALITY_QUOTA_WARNING,
    SERVICE_NAME,
)
from sa_home_bot.reality.xray import XrayBackend

log = logging.getLogger(__name__)

# Байты в гигабайте — десятичный (10^9), как считают провайдеры трафика.
GB = 1_000_000_000

# Сентинельный chat_id для учёта состояния "весь канал ноды" в тех же
# таблицах, что и учёт по чатам — реальный Telegram chat_id никогда не 0.
NODE_SENTINEL_CHAT_ID = 0

# Имя устройства служба выбирает сама (как в vpn/service.py) — гость и модель
# путались, что вводить. Только английские буквы: имя используется и в
# ``#label`` ссылки vless://, и как имя файла ``<label>.json``.
_FLOWER_NAMES = (
    "Rose",
    "Lily",
    "Iris",
    "Aster",
    "Poppy",
    "Daisy",
    "Tulip",
    "Lotus",
    "Phlox",
    "Pansy",
    "Sedum",
    "Canna",
    "Hosta",
    "Orchid",
    "Violet",
    "Dahlia",
    "Azalea",
    "Camellia",
    "Jasmine",
    "Lilac",
    "Peony",
    "Zinnia",
    "Yarrow",
    "Crocus",
    "Freesia",
    "Begonia",
    "Petunia",
    "Gerbera",
    "Mallow",
    "Cosmos",
)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _month_key(when: datetime) -> str:
    return when.strftime("%Y-%m")


def _random_device_label(used: set[str]) -> str:
    pool = [name for name in _FLOWER_NAMES if name not in used] or list(_FLOWER_NAMES)
    return random.choice(pool)


def _client_email(chat_id: int, device_label: str) -> str:
    """email юзера в xray — человекочитаемо для ``statsquery``/``inbounduser``
    на сервере, уникально среди активных (chat_id + имя устройства; двух
    активных устройств с одним именем у чата не бывает — см.
    ``_random_device_label`` и ключ reissue/revoke)."""
    return f"c{chat_id}-{device_label}"


def _render_qr_png_b64(text: str) -> str:
    import segno

    qr = segno.make(text, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=4, border=2)
    return base64.b64encode(buf.getvalue()).decode()


class RealityService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        backend: XrayBackend,
        emit: EmitFn,
    ) -> None:
        self._cfg = settings.reality
        self._db = db
        self._backend = backend
        self._emit = emit
        self._node = socket.gethostname()

    def describe(self) -> ServiceDescription:
        chat_id_param = ActionParam(name="chat_id", type="int", title="Чей это гость")
        device_param = ActionParam(name="device_label", type="string", title="Устройство")
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=(
                ACTION_PEERS,
                ACTION_ISSUE,
                ACTION_REISSUE,
                ACTION_REVOKE,
                ACTION_USAGE,
                ACTION_SET_QUOTA,
                ACTION_GRANT_EXTRA,
                ACTION_REQUEST_EXTRA,
                ACTION_RESOLVE_REQUEST,
            ),
            actions=(
                ActionSpec(id=ACTION_PEERS, title="🔌 Все пиры"),
                ActionSpec(id=ACTION_ISSUE, title="➕ Выдать доступ", params=(chat_id_param,)),
                ActionSpec(
                    id=ACTION_REISSUE,
                    title="🔄 Перевыпустить",
                    params=(chat_id_param, device_param),
                ),
                ActionSpec(
                    id=ACTION_REVOKE, title="🚫 Отозвать", params=(chat_id_param, device_param)
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
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM reality_peers WHERE status = 'active'"
        )
        row = await cur.fetchone()
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "active_peers": row["n"] if row else 0,
        }

    # --- вспомогательное ---

    @staticmethod
    def _chat_id(args: dict[str, Any]) -> int:
        raw = args.get("chat_id")
        if raw is None:
            raise ProtoError(ERR_BAD_REQUEST, "не указан chat_id — доступ выдаётся человеку")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ProtoError(ERR_BAD_REQUEST, f"chat_id должен быть числом: {raw!r}") from exc

    async def _blocked_chats(self, month: str) -> set[int]:
        cur = await self._db.conn.execute(
            "SELECT chat_id FROM reality_quota_state WHERE month = ? AND blocked_at IS NOT NULL",
            (month,),
        )
        return {row["chat_id"] for row in await cur.fetchall()}

    async def _quota_state(self, chat_id: int, month: str) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT warned_limit_bytes, blocked_at FROM reality_quota_state "
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
            "INSERT INTO reality_quota_state (chat_id, month, warned_limit_bytes, blocked_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id, month) DO UPDATE SET "
            "warned_limit_bytes = excluded.warned_limit_bytes, blocked_at = excluded.blocked_at",
            (chat_id, month, current["warned_limit_bytes"], current["blocked_at"]),
        )
        await self._db.conn.commit()

    async def _used_bytes(self, chat_id: int, month: str) -> int:
        cur = await self._db.conn.execute(
            "SELECT COALESCE(SUM(u.used_bytes), 0) AS total FROM reality_peer_usage u "
            "JOIN reality_peers p ON p.id = u.peer_id WHERE p.chat_id = ? AND u.month = ?",
            (chat_id, month),
        )
        row = await cur.fetchone()
        return int(row["total"] or 0)

    async def _granted_bytes(self, chat_id: int, month: str, *, source: str | None = None) -> int:
        if source is None:
            cur = await self._db.conn.execute(
                "SELECT COALESCE(SUM(bytes), 0) AS total FROM reality_quota_grants "
                "WHERE chat_id = ? AND month = ?",
                (chat_id, month),
            )
        else:
            cur = await self._db.conn.execute(
                "SELECT COALESCE(SUM(bytes), 0) AS total FROM reality_quota_grants "
                "WHERE chat_id = ? AND month = ? AND source = ?",
                (chat_id, month, source),
            )
        row = await cur.fetchone()
        return int(row["total"] or 0)

    async def _limit_bytes(self, chat_id: int, month: str) -> int:
        return self._cfg.base_quota_gb * GB + await self._granted_bytes(chat_id, month)

    async def _add_grant(
        self, chat_id: int, month: str, bytes_: int, *, source: str, request_id: int | None = None
    ) -> None:
        await self._db.conn.execute(
            "INSERT INTO reality_quota_grants (chat_id, month, bytes, source, request_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, month, bytes_, source, request_id, _now().isoformat()),
        )
        await self._db.conn.commit()

    async def _active_labels(self, chat_id: int) -> set[str]:
        cur = await self._db.conn.execute(
            "SELECT device_label FROM reality_peers WHERE chat_id = ? AND status = 'active'",
            (chat_id,),
        )
        return {row["device_label"] for row in await cur.fetchall()}

    async def _peers_for_chat(self, chat_id: int) -> list[dict[str, Any]]:
        cur = await self._db.conn.execute(
            "SELECT device_label, status, created_at, last_seen_at, server FROM reality_peers "
            "WHERE chat_id = ? AND status = 'active' ORDER BY created_at",
            (chat_id,),
        )
        return [
            {
                "device_label": row["device_label"],
                "status": row["status"],
                "created_at": row["created_at"],
                "last_seen_at": row["last_seen_at"],
                "server": row["server"] or self._node,
            }
            for row in await cur.fetchall()
        ]

    # --- миграция данных ---

    async def backfill_server(self) -> None:
        """Пиры без проставленного сервера — все на этой ноде. На свежей БД
        строк нет (колонка ``server`` заполняется при выдаче), метод —
        страховка на случай ручных правок / будущих миграций."""
        cur = await self._db.conn.execute(
            "UPDATE reality_peers SET server = ? WHERE server IS NULL", (self._node,)
        )
        if cur.rowcount:
            await self._db.conn.commit()
            log.info("reality: проставлен server=%s у %d пиров", self._node, cur.rowcount)

    # --- reconciler ---

    async def reconcile(self) -> None:
        """Свести список юзеров xray с БД: активные пиры незаблокированных
        чатов должны быть в inbound, всё лишнее — снять. Вызывается при старте
        (после рестарта xray inbound пуст) и на каждом тике сэмплера."""
        month = _month_key(_now())
        blocked = await self._blocked_chats(month)
        cur = await self._db.conn.execute(
            "SELECT chat_id, uuid, email, device_label FROM reality_peers WHERE status = 'active'"
        )
        rows = await cur.fetchall()
        desired = {
            row["email"]: (row["uuid"], row["email"])
            for row in rows
            if row["chat_id"] not in blocked
        }
        current = await self._backend.list_clients()
        for email in current - desired.keys():
            await self._backend.remove_client(email)
        for email, (client_uuid, _email) in desired.items():
            if email not in current:
                await self._backend.add_client(client_uuid, email, self._cfg.flow)

    # --- issue/reissue/revoke ---

    def _client_artifacts(self, client_uuid: str, device_label: str) -> dict[str, Any]:
        """sing-box конфиг + vless-ссылка + deep-link + QR для одного
        устройства. ``self._cfg`` несёт поля с теми же именами, что
        ``client_config.RealityParams`` — ``render_*`` берут его по
        duck-typing."""
        config_text = _render_singbox(self._cfg, client_uuid)
        share_url = render_vless_url(self._cfg, client_uuid, device_label)
        return {
            "config_text": config_text,
            "share_url": share_url,
            "deep_link": render_deep_link(share_url),
            "qr_png_b64": _render_qr_png_b64(share_url),
        }

    async def _issue(
        self, args: dict[str, Any], *, forced_label: str | None = None
    ) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        existing_labels = await self._active_labels(chat_id)
        device_label = forced_label or _random_device_label(existing_labels)
        client_uuid = str(uuidlib.uuid4())
        email = _client_email(chat_id, device_label)
        now = _now().isoformat()
        await self._db.conn.execute(
            "INSERT INTO reality_peers (chat_id, device_label, uuid, email, status, "
            "created_at, server) VALUES (?, ?, ?, ?, 'active', ?, ?)",
            (chat_id, device_label, client_uuid, email, now, self._node),
        )
        await self._db.conn.commit()
        await self._backend.add_client(client_uuid, email, self._cfg.flow)
        await self._emit(
            EVENT_REALITY_PEER_ISSUED, {"chat_id": chat_id, "device_label": device_label}
        )
        return {
            **self._client_artifacts(client_uuid, device_label),
            "device_label": device_label,
            "uuid": client_uuid,
            # Число устройств чата ДО этой выдачи — бот выбирает по нему, что
            # показать первым: 0 → первое устройство, вероятно этот же телефон,
            # удобнее файл; иначе — вероятно для другого устройства → QR.
            "prior_device_count": len(existing_labels),
        }

    async def _reissue(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        device_label = str(args.get("device_label") or "").strip()
        if not device_label:
            raise ProtoError(ERR_BAD_REQUEST, "не указано устройство (device_label)")
        cur = await self._db.conn.execute(
            "SELECT uuid, email FROM reality_peers WHERE chat_id = ? AND device_label = ? "
            "AND status = 'active'",
            (chat_id, device_label),
        )
        row = await cur.fetchone()
        if row is not None:
            await self._db.conn.execute(
                "UPDATE reality_peers SET status = 'expired', revoked_at = ? WHERE uuid = ?",
                (_now().isoformat(), row["uuid"]),
            )
            await self._db.conn.commit()
            await self._backend.remove_client(row["email"])
        return await self._issue(args, forced_label=device_label)

    async def _revoke(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        device_label = str(args.get("device_label") or "").strip()
        cur = await self._db.conn.execute(
            "SELECT uuid, email FROM reality_peers WHERE chat_id = ? AND device_label = ? "
            "AND status = 'active'",
            (chat_id, device_label),
        )
        row = await cur.fetchone()
        if row is None:
            raise ProtoError(ERR_BAD_REQUEST, f"нет активного устройства «{device_label}»")
        await self._db.conn.execute(
            "UPDATE reality_peers SET status = 'revoked', revoked_at = ? WHERE uuid = ?",
            (_now().isoformat(), row["uuid"]),
        )
        await self._db.conn.commit()
        await self._backend.remove_client(row["email"])
        return {"revoked": True, "device_label": device_label}

    async def _peers(self, _args: dict[str, Any]) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT chat_id, device_label, status, created_at, last_seen_at, server "
            "FROM reality_peers ORDER BY chat_id, created_at"
        )
        peers = [
            {
                "chat_id": row["chat_id"],
                "device_label": row["device_label"],
                "status": row["status"],
                "created_at": row["created_at"],
                "last_seen_at": row["last_seen_at"],
                "server": row["server"] or self._node,
            }
            for row in await cur.fetchall()
        ]
        return {"peers": peers}

    # --- квоты ---

    async def _usage(self, args: dict[str, Any]) -> dict[str, Any]:
        month = _month_key(_now())
        raw_chat_id = args.get("chat_id")
        if raw_chat_id is not None:
            chat_id = self._chat_id(args)
            used = await self._used_bytes(chat_id, month)
            limit = await self._limit_bytes(chat_id, month)
            state = await self._quota_state(chat_id, month)
            return {
                "chat_id": chat_id,
                "month": month,
                "used_bytes": used,
                "limit_bytes": limit,
                "remaining_bytes": max(limit - used, 0),
                "blocked": state["blocked_at"] is not None,
                "devices": await self._peers_for_chat(chat_id),
            }
        cur = await self._db.conn.execute(
            "SELECT DISTINCT chat_id FROM reality_peers WHERE status = 'active'"
        )
        active_chat_ids = [row["chat_id"] for row in await cur.fetchall()]
        chats = []
        reserved_bytes = 0
        for cid in active_chat_ids:
            limit = await self._limit_bytes(cid, month)
            chats.append(
                {
                    "chat_id": cid,
                    "used_bytes": await self._used_bytes(cid, month),
                    "limit_bytes": limit,
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
        month = _month_key(_now())
        current_limit = await self._limit_bytes(chat_id, month)
        delta = target - current_limit
        if delta:
            await self._add_grant(chat_id, month, delta, source="admin")
        await self._check_thresholds(chat_id, month)
        return await self._usage({"chat_id": chat_id})

    async def _grant_extra(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        month = _month_key(_now())
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
        base = self._cfg.base_quota_gb * GB
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
            "INSERT INTO reality_requests (chat_id, bytes, status, created_at) "
            "VALUES (?, ?, 'pending', ?)",
            (chat_id, bytes_, _now().isoformat()),
        )
        await self._db.conn.commit()
        request_id = cur.lastrowid
        await self._emit(
            EVENT_REALITY_EXTRA_REQUESTED,
            {"request_id": request_id, "chat_id": chat_id, "bytes": bytes_},
        )
        return {"request_id": request_id, "status": "pending"}

    async def _resolve_request(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_id = args.get("request_id")
        if raw_id is None:
            raise ProtoError(ERR_BAD_REQUEST, "не указан request_id")
        request_id = int(raw_id)
        approve = bool(args.get("approve"))
        cur = await self._db.conn.execute(
            "SELECT chat_id, bytes, status FROM reality_requests WHERE id = ?", (request_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise ProtoError(ERR_BAD_REQUEST, f"нет заявки №{request_id}")
        if row["status"] != "pending":
            raise ProtoError(ERR_BAD_REQUEST, f"заявка №{request_id} уже решена")
        status = "approved" if approve else "denied"
        await self._db.conn.execute(
            "UPDATE reality_requests SET status = ?, decided_at = ? WHERE id = ?",
            (status, _now().isoformat(), request_id),
        )
        await self._db.conn.commit()
        chat_id = row["chat_id"]
        if approve:
            month = _month_key(_now())
            await self._add_grant(
                chat_id, month, row["bytes"], source="admin", request_id=request_id
            )
            await self._check_thresholds(chat_id, month)
        await self._emit(
            EVENT_REALITY_EXTRA_RESOLVED,
            {
                "request_id": request_id,
                "chat_id": chat_id,
                "approved": approve,
                "bytes": row["bytes"],
            },
        )
        return {"request_id": request_id, "status": status}

    async def _check_thresholds(self, chat_id: int, month: str) -> None:
        used = await self._used_bytes(chat_id, month)
        limit = await self._limit_bytes(chat_id, month)
        remaining = limit - used
        state = await self._quota_state(chat_id, month)
        warn_threshold = self._cfg.warn_remaining_gb * GB

        if used >= limit:
            if state["blocked_at"] is None:
                await self._set_quota_state(chat_id, month, blocked_at=_now().isoformat())
                await self.reconcile()
                await self._emit(EVENT_REALITY_QUOTA_EXCEEDED, {"chat_id": chat_id})
                await self._emit(EVENT_REALITY_PEER_BLOCKED, {"chat_id": chat_id})
            return
        if state["blocked_at"] is not None:
            await self._set_quota_state(chat_id, month, blocked_at=None)
            await self.reconcile()
            await self._emit(EVENT_REALITY_ACCESS_RESTORED, {"chat_id": chat_id})
        if remaining <= warn_threshold and state["warned_limit_bytes"] != limit:
            await self._set_quota_state(chat_id, month, warned_limit_bytes=limit)
            await self._emit(
                EVENT_REALITY_QUOTA_WARNING,
                {"chat_id": chat_id, "remaining_bytes": max(remaining, 0)},
            )

    async def _check_node_limit(self, month: str) -> None:
        cur = await self._db.conn.execute(
            "SELECT COALESCE(SUM(used_bytes), 0) AS total FROM reality_peer_usage WHERE month = ?",
            (month,),
        )
        row = await cur.fetchone()
        total = int(row["total"] or 0)
        limit = self._cfg.node_limit_gb * GB
        threshold = limit - self._cfg.warn_remaining_gb * GB
        if total < threshold:
            return
        state = await self._quota_state(NODE_SENTINEL_CHAT_ID, month)
        if state["warned_limit_bytes"] == limit:
            return
        await self._set_quota_state(NODE_SENTINEL_CHAT_ID, month, warned_limit_bytes=limit)
        await self._emit(
            EVENT_REALITY_NODE_QUOTA_WARNING, {"used_bytes": total, "limit_bytes": limit}
        )

    # --- сэмплер ---

    async def sample_once(self) -> None:
        stats = await self._backend.stats()
        now = _now()
        month = _month_key(now)
        cur = await self._db.conn.execute(
            "SELECT id, chat_id, email FROM reality_peers WHERE status = 'active'"
        )
        rows = await cur.fetchall()
        touched_chats: set[int] = set()
        for row in rows:
            email = row["email"]
            if email not in stats:
                continue
            up, down = stats[email]
            total = up + down
            cur2 = await self._db.conn.execute(
                "SELECT last_up, last_down FROM reality_counters WHERE email = ?", (email,)
            )
            prev = await cur2.fetchone()
            if prev is None:
                delta = total
            else:
                prev_total = prev["last_up"] + prev["last_down"]
                delta = total - prev_total if total >= prev_total else total
            if delta:
                await self._db.conn.execute(
                    "INSERT INTO reality_peer_usage (peer_id, month, used_bytes) "
                    "VALUES (?, ?, ?) ON CONFLICT(peer_id, month) DO UPDATE SET "
                    "used_bytes = used_bytes + excluded.used_bytes",
                    (row["id"], month, delta),
                )
                touched_chats.add(row["chat_id"])
                await self._db.conn.execute(
                    "UPDATE reality_peers SET last_seen_at = ? WHERE id = ?",
                    (now.isoformat(), row["id"]),
                )
            await self._db.conn.execute(
                "INSERT INTO reality_counters (email, last_up, last_down, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET "
                "last_up = excluded.last_up, last_down = excluded.last_down, "
                "updated_at = excluded.updated_at",
                (email, up, down, now.isoformat()),
            )
        await self._db.conn.commit()
        for chat_id in touched_chats:
            await self._check_thresholds(chat_id, month)
        await self._check_node_limit(month)
        await self.reconcile()

    async def usage_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.sample_interval_s)
            try:
                await self.sample_once()
            except Exception:  # noqa: BLE001 — сбой одного тика не должен ронять цикл
                log.exception("reality: сбой сэмплера трафика")

    # --- диспетчер ---

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_PEERS:
            return await self._peers(args)
        if action == ACTION_ISSUE:
            return await self._issue(args)
        if action == ACTION_REISSUE:
            return await self._reissue(args)
        if action == ACTION_REVOKE:
            return await self._revoke(args)
        if action == ACTION_USAGE:
            return await self._usage(args)
        if action == ACTION_SET_QUOTA:
            return await self._set_quota(args)
        if action == ACTION_GRANT_EXTRA:
            return await self._grant_extra(args)
        if action == ACTION_REQUEST_EXTRA:
            return await self._request_extra(args)
        if action == ACTION_RESOLVE_REQUEST:
            return await self._resolve_request(args)
        raise ValueError(f"необъявленное действие: {action}")
