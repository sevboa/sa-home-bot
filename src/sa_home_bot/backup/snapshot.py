from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import time
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sa_home_bot.backup import sealed, serve
from sa_home_bot.backup.hold import HOLD_HINT, publish_allowed
from sa_home_bot.backup.identity import canonical_bytes, plain_hash
from sa_home_bot.config import Settings
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ActionParam, ActionSpec, ProtoError

log = logging.getLogger(__name__)

SNAPSHOT_FORMAT = "sa-home-bot/vpn-snapshot/1"

ACTION_GET_SNAPSHOT = "backup_snapshot_get"
ACTION_SNAPSHOT_POKE = "backup_snapshot_poke"

SNAPSHOT_TABLES = (
    "vpn_peers",
    "vpn_chat_access",
    "vpn_counters",
    "vpn_peer_usage",
    "vpn_quota_state",
    "vpn_quota_grants",
    "vpn_requests",
    "proxy_state",
)

MAX_SEALED_BYTES = 640 * 1024
MAX_PLAINTEXT_BYTES = 64 * 1024 * 1024

TRIGGER_ACTIONS = frozenset(
    {
        "issue", "reissue", "revoke", "set_quota", "set_access",
        "grant_extra", "request_extra", "resolve_request",
    }
)


class SnapshotError(Exception):
    pass


async def dump_tables(conn: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for table in SNAPSHOT_TABLES:
        cur = await conn.execute(f"PRAGMA table_info({table})")
        info = await cur.fetchall()
        if not info:
            continue
        columns = [r["name"] for r in info]
        pk = [r["name"] for r in sorted(info, key=lambda r: r["pk"]) if r["pk"]]
        order = ", ".join(pk) if pk else "rowid"
        cur = await conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order}")
        out[table] = {"columns": columns, "rows": [list(r) for r in await cur.fetchall()]}
    return out


def build_document(node: str, tables: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"format": SNAPSHOT_FORMAT, "node": node, "tables": tables}


def summarize(doc: dict[str, Any]) -> dict[str, int]:
    tables = doc.get("tables", {})
    rows = {name: len(t["rows"]) for name, t in tables.items()}
    peers = tables.get("vpn_peers")
    if peers is not None and "status" in peers["columns"]:
        idx = peers["columns"].index("status")
        rows["vpn_peers_active"] = sum(1 for r in peers["rows"] if r[idx] == "active")
    return rows


def seal_document(recipient_public_key: bytes, plaintext: bytes) -> bytes:
    return sealed.seal(recipient_public_key, zlib.compress(plaintext, 9))


def open_snapshot(private_key: bytes, blob: bytes) -> dict[str, Any]:
    try:
        packed = sealed.open_sealed(private_key, blob)
        plaintext = zlib.decompressobj().decompress(packed, MAX_PLAINTEXT_BYTES)
        doc = json.loads(plaintext)
    except (sealed.SealedError, zlib.error, ValueError) as exc:
        raise SnapshotError(f"не удалось расшифровать снапшот: {exc}") from exc
    if doc.get("format") != SNAPSHOT_FORMAT:
        raise SnapshotError(f"неизвестный формат снапшота: {doc.get('format')!r}")
    return doc


@dataclass(frozen=True)
class Snapshot:
    blob: bytes
    meta: dict[str, Any]


class SnapshotSource:
    def __init__(
        self,
        settings: Settings,
        conn_getter: Callable[[], Any],
        node_id: str,
        *,
        notify: Callable[[], Awaitable[object]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        b = settings.backup
        self._settings = settings
        self._conn_getter = conn_getter
        self._node_id = node_id
        self._recipient = sealed.load_key(b.recipient_public_key)
        self._interval = b.snapshot_interval_s
        self._debounce = b.snapshot_debounce_s
        self._notify = notify
        self._clock = clock
        self._cached: Snapshot | None = None
        self._built_at = 0.0
        self._rev = 0
        self._dirty = False
        self._lock = asyncio.Lock()
        self._pending: asyncio.Task | None = None

    def _due(self) -> bool:
        if self._cached is None:
            return True
        age = self._clock() - self._built_at
        return age >= self._interval or (self._dirty and age >= self._debounce)

    async def current(self) -> Snapshot | None:
        async with self._lock:
            if self._due():
                try:
                    await self._rebuild()
                except Exception:  # noqa: BLE001
                    log.exception("Бэкап снапшота vpn: сбой сборки")
            return self._cached

    async def _rebuild(self) -> None:
        doc = build_document(self._node_id, await dump_tables(self._conn_getter()))
        plaintext = canonical_bytes(doc)
        digest = plain_hash(plaintext)
        self._built_at = self._clock()
        self._dirty = False
        if self._cached is not None and self._cached.meta["hash"] == digest:
            return
        blob = seal_document(self._recipient, plaintext)
        if len(blob) > MAX_SEALED_BYTES:
            raise SnapshotError(f"снапшот {len(blob)} Б не лезет в сообщение протокола")
        self._rev += 1
        self._cached = Snapshot(
            blob=blob,
            meta={
                "source": self._node_id,
                "format": SNAPSHOT_FORMAT,
                "rev": self._rev,
                "hash": digest,
                "taken_at": datetime.now(tz=UTC).isoformat(),
                "rows": summarize(doc),
            },
        )

    def touch(self) -> None:
        self._dirty = True
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._flush(), name="vpn-snapshot-flush")

    async def _flush(self) -> None:
        await asyncio.sleep(self._debounce)
        snap = await self.current()
        if snap is not None and self._notify is not None:
            try:
                await self._notify()
            except Exception as exc:  # noqa: BLE001
                log.debug("Бэкап снапшота vpn: напарник не уведомлён (%s)", exc)

    async def handle_get(self, args: dict[str, Any]) -> dict[str, Any]:
        if not publish_allowed(self._settings):
            raise SnapshotError(HOLD_HINT)
        snap = await self.current()
        if snap is None:
            raise SnapshotError("снапшот ещё не собран")
        if args.get("have_hash") == snap.meta["hash"]:
            return {"unchanged": True, "hash": snap.meta["hash"]}
        return {
            "hash": snap.meta["hash"],
            "meta": snap.meta,
            "sealed": base64.b64encode(snap.blob).decode("ascii"),
        }

    async def stop(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pending
            self._pending = None


Ask = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class SnapshotReceiver:
    def __init__(self, settings: Settings, store: Any, ask: Ask) -> None:
        self._partner = settings.backup.partner.strip()
        self._poll = settings.backup.snapshot_poll_s
        self._store = store
        self._ask = ask
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def poke(self) -> None:
        self._wake.set()

    async def pull_once(self) -> bool:
        stored = self._store.load_snapshot(self._partner)
        have = stored.meta.get("hash") if stored else None
        resp = await self._ask(ACTION_GET_SNAPSHOT, {"have_hash": have})
        if resp.get("unchanged"):
            return False
        try:
            blob = base64.b64decode(resp["sealed"], validate=True)
            meta = dict(resp["meta"])
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise SnapshotError(f"ответ напарника повреждён: {exc}") from exc
        if meta.get("source") != self._partner or meta.get("format") != SNAPSHOT_FORMAT:
            raise SnapshotError(f"снапшот от {meta.get('source')!r}, ждали {self._partner!r}")
        if not meta.get("hash") or meta["hash"] == have:
            return False
        self._store.save_snapshot(self._partner, blob, meta)
        return True

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="vpn-snapshot-pull")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._poll)
            self._wake.clear()
            try:
                await self.pull_once()
            except Exception as exc:  # noqa: BLE001
                log.debug("Бэкап снапшота vpn: не удалось забрать у %s (%s)", self._partner, exc)


def snapshot_enabled(settings: Settings) -> bool:
    b = settings.backup
    return bool(b.recipient_public_key.strip() and b.partner.strip())


class SnapshotBackup:
    def __init__(
        self,
        source: SnapshotSource | None,
        receiver: SnapshotReceiver | None,
        *,
        store: Any = None,
        partner: str = "",
    ) -> None:
        self.source = source
        self.receiver = receiver
        self.store = store
        self.partner = partner

    def action_specs(self) -> list[ActionSpec]:
        specs: list[ActionSpec] = []
        if self.source is not None:
            specs.append(
                ActionSpec(
                    id=ACTION_GET_SNAPSHOT,
                    title="💾 Запечатанный снапшот БД",
                    params=(
                        ActionParam(
                            name="have_hash", type="string", required=False,
                            title="Хеш у запросившего",
                        ),
                    ),
                )
            )
        if self.receiver is not None:
            specs.append(ActionSpec(id=ACTION_SNAPSHOT_POKE, title="💾 Есть новый снапшот"))
        if self.store is not None:
            specs.extend(serve.action_specs())
        return specs

    def touch(self) -> None:
        if self.source is not None:
            self.source.touch()

    async def handle(self, action: str, args: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return await self._handle(action, args)
        except (SnapshotError, serve.ServeError) as exc:
            raise ProtoError(ERR_BAD_REQUEST, str(exc)) from exc

    async def _handle(self, action: str, args: dict[str, Any]) -> dict[str, Any] | None:
        if action == ACTION_GET_SNAPSHOT and self.source is not None:
            return await self.source.handle_get(args)
        if action == ACTION_SNAPSHOT_POKE and self.receiver is not None:
            self.receiver.poke()
            return {"ok": True}
        if self.store is not None:
            return serve.handle(self.store, self.partner, action, args)
        return None

    async def start(self) -> None:
        if self.receiver is not None:
            await self.receiver.start()

    async def stop(self) -> None:
        if self.source is not None:
            await self.source.stop()
        if self.receiver is not None:
            await self.receiver.stop()


def build_backup(
    settings: Settings,
    conn_getter: Callable[[], Any],
    node_id: str,
    ask_partner: Ask,
) -> SnapshotBackup | None:
    from sa_home_bot.backup.store import BackupStore, backups_dir

    if not settings.backup.partner.strip():
        return None
    source = None
    if snapshot_enabled(settings):
        try:

            async def poke() -> object:
                return await ask_partner(ACTION_SNAPSHOT_POKE, {})

            source = SnapshotSource(settings, conn_getter, node_id, notify=poke)
        except sealed.SealedError as exc:
            log.warning("Бэкап снапшота выключен: [backup].recipient_public_key негоден (%s)", exc)
    store = BackupStore(backups_dir(settings.node.state_path))
    return SnapshotBackup(
        source,
        SnapshotReceiver(settings, store, ask_partner),
        store=store,
        partner=settings.backup.partner.strip(),
    )
