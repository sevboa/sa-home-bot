"""VpnService — ServiceHandler службы vpn (AmneziaWG-доступ на jeeves,
выдаваемый и учитываемый через бота).

Реконсайлер, а не разрозненные add/remove (решение из плана этапа 33):
``_reconcile_peers`` сравнивает желаемое состояние интерфейса (активные
пиры незаблокированных чатов — из БД, БД источник истины) с фактическим
(``awg show <iface> transfer``, он же список пиров на интерфейсе). Лишних
снимает, недостающих добавляет. Вызывается при старте (после рестарта jeeves
интерфейс пуст, БД помнит всех) и на каждом тике сэмплера — тем же ходом
закрывает и месячную разблокировку 1-го числа (новый месяц = чат не в списке
заблокированных), и снятие пира при блокировке, и самовосстановление после
ручных правок на сервере.

Приватность (решение плана): служба хранит только объёмы и время последнего
хендшейка. Ни адресов назначения, ни DNS-запросов, ни логов соединений —
adress пира это внутренний IP в VPN-подсети, не то, куда он ходит.

Деньги/трафик считаются в ГБ = 10^9 байт (десятичные, как у провайдеров),
в БД — байты.
"""

from __future__ import annotations

import asyncio
import base64
import io
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.vpn import apk as apk_client
from sa_home_bot.vpn.awg import AwgBackend
from sa_home_bot.vpn.protocol import (
    ACTION_APK_CHUNK,
    ACTION_APK_INFO,
    ACTION_APK_SET_FILE_ID,
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
    EVENT_VPN_ACCESS_RESTORED,
    EVENT_VPN_EXTRA_REQUESTED,
    EVENT_VPN_EXTRA_RESOLVED,
    EVENT_VPN_NODE_QUOTA_WARNING,
    EVENT_VPN_PEER_BLOCKED,
    EVENT_VPN_PEER_ISSUED,
    EVENT_VPN_QUOTA_EXCEEDED,
    EVENT_VPN_QUOTA_WARNING,
    SERVICE_NAME,
)

log = logging.getLogger(__name__)

# Байты в гигабайте — десятичный (10^9), как считают провайдеры трафика, а
# не 2^30 (гибибайт): пользователю обещают «500 ГБ», это должно совпадать.
GB = 1_000_000_000

# Сентинельный chat_id для учёта состояния "весь канал ноды" в тех же
# таблицах, что и учёт по чатам (vpn_quota_state) — реальный Telegram
# chat_id никогда не бывает 0, коллизии не будет.
NODE_SENTINEL_CHAT_ID = 0

# Сколько держать ответ apk_info без повторного похода на GitHub — гость,
# нажавший кнопку дважды подряд, не должен провоцировать два запроса к API.
APK_INFO_MEMO_S = 60.0
APK_API_TIMEOUT_S = 15.0
APK_DOWNLOAD_TIMEOUT_S = 90.0
# Кусок APK на одно сообщение протокола роя — с запасом от MAX_MESSAGE_BYTES
# (1 МиБ, proto/messages.py): base64 раздувает байты на треть, плюс сам
# конверт JSON.
APK_CHUNK_BYTES = 700 * 1024

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _month_key(when: datetime) -> str:
    return when.strftime("%Y-%m")


def _allocate_address(subnet: str, used: set[str]) -> str:
    """Наименьший свободный адрес подсети — первый хост (обычно .1)
    зарезервирован под сам сервер и в ``used`` не участвует."""
    network = ipaddress.ip_network(subnet, strict=False)
    hosts = list(network.hosts())
    for host in hosts[1:]:
        addr = str(host)
        if addr not in used:
            return addr
    raise ProtoError(ERR_BAD_REQUEST, "подсеть VPN исчерпана — нет свободных адресов")


def _render_client_conf(cfg: Any, private_key: str, address: str, server_public_key: str) -> str:
    endpoint = f"{cfg.endpoint_host}:{cfg.endpoint_port}"
    return (
        "[Interface]\n"
        f"PrivateKey = {private_key}\n"
        f"Address = {address}/32\n"
        f"DNS = {cfg.dns}\n"
        f"Jc = {cfg.jc}\n"
        f"Jmin = {cfg.jmin}\n"
        f"Jmax = {cfg.jmax}\n"
        f"S1 = {cfg.s1}\n"
        f"S2 = {cfg.s2}\n"
        f"H1 = {cfg.h1}\n"
        f"H2 = {cfg.h2}\n"
        f"H3 = {cfg.h3}\n"
        f"H4 = {cfg.h4}\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {server_public_key}\n"
        f"Endpoint = {endpoint}\n"
        "AllowedIPs = 0.0.0.0/0\n"
        "PersistentKeepalive = 25\n"
    )


def _render_qr_png_b64(text: str) -> str:
    import segno

    qr = segno.make(text, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=4, border=2)
    return base64.b64encode(buf.getvalue()).decode()


class VpnService:
    def __init__(
        self, settings: Settings, db: Database, backend: AwgBackend, emit: EmitFn
    ) -> None:
        self._cfg = settings.vpn
        self._db = db
        self._backend = backend
        self._emit = emit
        self._node = socket.gethostname()
        self._server_pubkey: str | None = None
        self._apk_checked_at: datetime | None = None

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
                ACTION_APK_INFO,
            ),
            actions=(
                ActionSpec(id=ACTION_PEERS, title="🔌 Все пиры"),
                ActionSpec(
                    id=ACTION_ISSUE,
                    title="➕ Выдать доступ",
                    params=(chat_id_param, device_param),
                ),
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
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT COUNT(*) AS n FROM vpn_peers WHERE status = 'active'"
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

    async def _server_public_key(self) -> str:
        if self._server_pubkey is None:
            self._server_pubkey = await self._backend.server_public_key()
        return self._server_pubkey

    async def _active_addresses(self) -> set[str]:
        cur = await self._db.conn.execute("SELECT address FROM vpn_peers WHERE status = 'active'")
        return {row["address"] for row in await cur.fetchall()}

    async def _blocked_chats(self, month: str) -> set[int]:
        cur = await self._db.conn.execute(
            "SELECT chat_id FROM vpn_quota_state WHERE month = ? AND blocked_at IS NOT NULL",
            (month,),
        )
        return {row["chat_id"] for row in await cur.fetchall()}

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

    async def _limit_bytes(self, chat_id: int, month: str) -> int:
        return self._cfg.base_quota_gb * GB + await self._granted_bytes(chat_id, month)

    async def _add_grant(
        self, chat_id: int, month: str, bytes_: int, *, source: str, request_id: int | None = None
    ) -> None:
        await self._db.conn.execute(
            "INSERT INTO vpn_quota_grants (chat_id, month, bytes, source, request_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, month, bytes_, source, request_id, _now().isoformat()),
        )
        await self._db.conn.commit()

    async def _peers_for_chat(self, chat_id: int) -> list[dict[str, Any]]:
        cur = await self._db.conn.execute(
            "SELECT device_label, status, created_at, last_handshake_at FROM vpn_peers "
            "WHERE chat_id = ? AND status = 'active' ORDER BY created_at",
            (chat_id,),
        )
        return [
            {
                "device_label": row["device_label"],
                "status": row["status"],
                "created_at": row["created_at"],
                "last_handshake_at": row["last_handshake_at"],
            }
            for row in await cur.fetchall()
        ]

    # --- reconciler ---

    async def reconcile(self) -> None:
        month = _month_key(_now())
        blocked = await self._blocked_chats(month)
        cur = await self._db.conn.execute(
            "SELECT chat_id, public_key, address FROM vpn_peers WHERE status = 'active'"
        )
        rows = await cur.fetchall()
        desired = {
            row["public_key"]: row["address"] for row in rows if row["chat_id"] not in blocked
        }
        current = set((await self._backend.transfer()).keys())
        for pubkey in current - desired.keys():
            await self._backend.remove_peer(pubkey)
        for pubkey, address in desired.items():
            if pubkey not in current:
                await self._backend.add_peer(pubkey, address)

    # --- issue/reissue/revoke ---

    async def _issue(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        device_label = str(args.get("device_label") or "").strip() or "устройство"
        # Число устройств на гостя намеренно не ограничено (решение
        # пользователя 2026-08-03) — реальный потолок стоимости уже задаёт
        # трафик (base_quota_gb/self_ceiling_gb), отдельный счётчик устройств
        # был бы лишней строгостью.
        private_key, public_key = await self._backend.generate_keypair()
        address = _allocate_address(self._cfg.subnet, await self._active_addresses())
        now = _now().isoformat()
        await self._db.conn.execute(
            "INSERT INTO vpn_peers (chat_id, device_label, public_key, address, status, "
            "created_at) VALUES (?, ?, ?, ?, 'active', ?)",
            (chat_id, device_label, public_key, address, now),
        )
        await self._db.conn.commit()
        await self._backend.add_peer(public_key, address)
        server_pub = await self._server_public_key()
        conf = _render_client_conf(self._cfg, private_key, address, server_pub)
        await self._emit(
            EVENT_VPN_PEER_ISSUED, {"chat_id": chat_id, "device_label": device_label}
        )
        return {
            "config_text": conf,
            "qr_png_b64": _render_qr_png_b64(conf),
            "address": address,
            "device_label": device_label,
        }

    async def _reissue(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        device_label = str(args.get("device_label") or "").strip()
        if not device_label:
            raise ProtoError(ERR_BAD_REQUEST, "не указано устройство (device_label)")
        cur = await self._db.conn.execute(
            "SELECT public_key FROM vpn_peers WHERE chat_id = ? AND device_label = ? "
            "AND status = 'active'",
            (chat_id, device_label),
        )
        row = await cur.fetchone()
        if row is not None:
            await self._db.conn.execute(
                "UPDATE vpn_peers SET status = 'expired', revoked_at = ? WHERE public_key = ?",
                (_now().isoformat(), row["public_key"]),
            )
            await self._db.conn.commit()
            await self._backend.remove_peer(row["public_key"])
        return await self._issue(args)

    async def _revoke(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        device_label = str(args.get("device_label") or "").strip()
        cur = await self._db.conn.execute(
            "SELECT public_key FROM vpn_peers WHERE chat_id = ? AND device_label = ? "
            "AND status = 'active'",
            (chat_id, device_label),
        )
        row = await cur.fetchone()
        if row is None:
            raise ProtoError(ERR_BAD_REQUEST, f"нет активного устройства «{device_label}»")
        await self._db.conn.execute(
            "UPDATE vpn_peers SET status = 'revoked', revoked_at = ? WHERE public_key = ?",
            (_now().isoformat(), row["public_key"]),
        )
        await self._db.conn.commit()
        await self._backend.remove_peer(row["public_key"])
        return {"revoked": True, "device_label": device_label}

    async def _peers(self, _args: dict[str, Any]) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT chat_id, device_label, address, status, created_at, last_handshake_at "
            "FROM vpn_peers ORDER BY chat_id, created_at"
        )
        peers = [
            {
                "chat_id": row["chat_id"],
                "device_label": row["device_label"],
                "address": row["address"],
                "status": row["status"],
                "created_at": row["created_at"],
                "last_handshake_at": row["last_handshake_at"],
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
        # Сводка для админа — только гости с ДЕЙСТВУЮЩИМ доступом (не
        # отозванным/просроченным): именно они «резервируют» трафик ноды,
        # revoked/expired ничего больше не стоят.
        cur = await self._db.conn.execute(
            "SELECT DISTINCT chat_id FROM vpn_peers WHERE status = 'active'"
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
            # "Резерв" — сумма ЛИМИТОВ (не факта потребления) активных
            # гостей: сколько канала занято обещаниями, даже если реально
            # ещё не потрачено — решение пользователя 2026-08-03, чтобы
            # видеть риск перерасхода тарифа ДО того, как он случится.
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
        # Самообслуживание открыто только когда трафик РЕАЛЬНО заканчивается
        # (решение пользователя 2026-08-03) — гость с почти полной квотой не
        # может докупить впрок; порог тот же, что у предупреждения
        # (warn_remaining_gb), чтобы «пришло предупреждение» и «можно
        # докупить» совпадали для гостя по смыслу.
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
            "INSERT INTO vpn_requests (chat_id, bytes, status, created_at) "
            "VALUES (?, ?, 'pending', ?)",
            (chat_id, bytes_, _now().isoformat()),
        )
        await self._db.conn.commit()
        request_id = cur.lastrowid
        await self._emit(
            EVENT_VPN_EXTRA_REQUESTED,
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
            EVENT_VPN_EXTRA_RESOLVED,
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
                await self._emit(EVENT_VPN_QUOTA_EXCEEDED, {"chat_id": chat_id})
                await self._emit(EVENT_VPN_PEER_BLOCKED, {"chat_id": chat_id})
            return
        if state["blocked_at"] is not None:
            await self._set_quota_state(chat_id, month, blocked_at=None)
            await self.reconcile()
            await self._emit(EVENT_VPN_ACCESS_RESTORED, {"chat_id": chat_id})
        if remaining <= warn_threshold and state["warned_limit_bytes"] != limit:
            await self._set_quota_state(chat_id, month, warned_limit_bytes=limit)
            await self._emit(
                EVENT_VPN_QUOTA_WARNING,
                {"chat_id": chat_id, "remaining_bytes": max(remaining, 0)},
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
        state = await self._quota_state(NODE_SENTINEL_CHAT_ID, month)
        if state["warned_limit_bytes"] == limit:
            return
        await self._set_quota_state(NODE_SENTINEL_CHAT_ID, month, warned_limit_bytes=limit)
        await self._emit(
            EVENT_VPN_NODE_QUOTA_WARNING, {"used_bytes": total, "limit_bytes": limit}
        )

    # --- сэмплер ---

    async def sample_once(self) -> None:
        transfer = await self._backend.transfer()
        handshakes = await self._backend.latest_handshakes()
        now = _now()
        month = _month_key(now)
        cur = await self._db.conn.execute(
            "SELECT id, chat_id, public_key FROM vpn_peers WHERE status = 'active'"
        )
        rows = await cur.fetchall()
        touched_chats: set[int] = set()
        for row in rows:
            pubkey = row["public_key"]
            if pubkey not in transfer:
                continue
            rx, tx = transfer[pubkey]
            total = rx + tx
            cur2 = await self._db.conn.execute(
                "SELECT last_rx, last_tx FROM vpn_counters WHERE public_key = ?", (pubkey,)
            )
            prev = await cur2.fetchone()
            if prev is None:
                # Первое наблюдение этого пира: он либо только что выдан
                # (интерфейс стартует с нуля), либо это первый тик сэмплера
                # после его появления — в обоих случаях верная база 0.
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
            await self._db.conn.execute(
                "INSERT INTO vpn_counters (public_key, last_rx, last_tx, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(public_key) DO UPDATE SET "
                "last_rx = excluded.last_rx, last_tx = excluded.last_tx, "
                "updated_at = excluded.updated_at",
                (pubkey, rx, tx, now.isoformat()),
            )
            handshake_ts = handshakes.get(pubkey, 0)
            if handshake_ts:
                await self._db.conn.execute(
                    "UPDATE vpn_peers SET last_handshake_at = ? WHERE id = ?",
                    (datetime.fromtimestamp(handshake_ts, tz=UTC).isoformat(), row["id"]),
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
                log.exception("vpn: сбой сэмплера трафика")

    # --- APK ---

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
        now = _now().isoformat()
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
        now = _now()
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
        if action == ACTION_APK_INFO:
            return await self._apk_info(args)
        if action == ACTION_APK_CHUNK:
            return await self._apk_chunk(args)
        if action == ACTION_APK_SET_FILE_ID:
            return await self._apk_set_file_id(args)
        # Сервер валидирует action по describe — сюда неизвестное не доходит.
        raise ValueError(f"необъявленное действие: {action}")
