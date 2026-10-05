"""Динамический снапшот VPN-БД: запечатанная копия у напарника (39.0.8(c)).

Статическая identity (``backup/identity.py``) едет через ``ConfigReplicator``.
Динамика — растущая SQLite — ему не подходит, поэтому здесь отдельный, но
устроенный по тому же принципу канал: **pull со стороны напарника**.

Источник (служба ``vpn`` ноды-владельца БД):

- выгружает строки таблиц ``SNAPSHOT_TABLES`` в канонический JSON
  (``format = SNAPSHOT_FORMAT``; для каждой таблицы — список колонок, т.е.
  «схема», и строки в порядке первичного ключа), сжимает zlib и запечатывает
  ключом получателя (``sealed.seal``) — прочесть может только alfred;
- пересобирает не чаще ``snapshot_interval_s`` и только если изменился хеш
  ОТКРЫТОГО текста (блоб при каждом запечатывании другой — эфемерный ключ);
  не сменился хеш — отдаётся прежний блоб;
- после изменяющих действий (``touch()``: issue/revoke/grant_extra/…) через
  ``snapshot_debounce_s`` пересобирает вне очереди и «дёргает» напарника
  (``ACTION_SNAPSHOT_POKE``) — тот сразу идёт за новым блобом.

Напарник (служба ``vpn`` ноды с ``[backup].partner``): раз в ``snapshot_poll_s``
или по poke спрашивает ``ACTION_GET_SNAPSHOT {have_hash}`` и, если хеш
отличается, кладёт блоб в ``BackupStore`` (``<нода>/snapshots/…``). Poke — лишь
ускоритель: потерянное событие ничего не значит, догонит плановый опрос.

Что в снапшоте и чего нет — см. ``SNAPSHOT_TABLES``. Пиры awg (``[Peer]``) и
клиенты xray сюда НЕ кладутся: ``VpnService.reconcile()`` пересоздаёт их из
``vpn_peers`` (+ допуск ``vpn_chat_access`` и квоты) при каждом старте службы.
"""

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

from sa_home_bot.backup import sealed
from sa_home_bot.backup.identity import canonical_bytes, plain_hash
from sa_home_bot.config import Settings
from sa_home_bot.proto.messages import ActionParam, ActionSpec

log = logging.getLogger(__name__)

SNAPSHOT_FORMAT = "sa-home-bot/vpn-snapshot/1"

ACTION_GET_SNAPSHOT = "backup_snapshot_get"  # {have_hash?} → {unchanged} | {hash, meta, sealed}
ACTION_SNAPSHOT_POKE = "backup_snapshot_poke"  # «у источника новый снапшот» — идти за ним

# Что нужно, чтобы пересобранная нода на том же IP вернула гостям рабочий доступ
# и не потеряла учёт:
#  - vpn_peers: публичные ключи/UUID, адреса, метки, статус — из них reconcile()
#    пересоздаёт [Peer] awg и клиентов xray;
#  - vpn_chat_access: допуск и личная база — без него reconcile() снимет всех;
#  - vpn_quota_state / vpn_quota_grants / vpn_peer_usage / vpn_counters: квоты,
#    добавки, накопленный расход месяца, последние счётчики сэмплера;
#  - vpn_requests: заявки на трафик (мелочь, но иначе ждущая заявка пропадёт);
#  - proxy_state: секрет mtg — без него ссылки прокси у гостей перестанут работать.
# Не берём: vpn_check_states (оперативное состояние, пересоздаётся за цикл проверок)
# и vpn_apk (кэш, скачивается заново).
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

# Потолок сообщения протокола — 1 МиБ (proto/messages.py); блоб идёт base64.
MAX_SEALED_BYTES = 640 * 1024
MAX_PLAINTEXT_BYTES = 64 * 1024 * 1024

# Команды vpn, после которых БД могла измениться.
TRIGGER_ACTIONS = frozenset(
    {
        "issue", "reissue", "revoke", "set_quota", "set_access",
        "grant_extra", "request_extra", "resolve_request",
    }
)


class SnapshotError(Exception):
    """Снапшот не собрать/не разобрать."""


# --- выгрузка и формат --------------------------------------------------------


async def dump_tables(conn: Any) -> dict[str, dict[str, Any]]:
    """Строки таблиц: ``{таблица: {"columns": [...], "rows": [[...], ...]}}``.

    Отсутствующая таблица пропускается (старая БД до миграции). Порядок строк —
    по первичному ключу (иначе по rowid): одинаковое содержимое даёт одинаковые
    байты, а значит и хеш.
    """
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
    """Число строк по таблицам (+ активные пиры) — в meta, без расшифровки."""
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
    """Расшифровать блоб (на alfred) в документ снапшота."""
    try:
        packed = sealed.open_sealed(private_key, blob)
        plaintext = zlib.decompressobj().decompress(packed, MAX_PLAINTEXT_BYTES)
        doc = json.loads(plaintext)
    except (sealed.SealedError, zlib.error, ValueError) as exc:
        raise SnapshotError(f"не удалось расшифровать снапшот: {exc}") from exc
    if doc.get("format") != SNAPSHOT_FORMAT:
        raise SnapshotError(f"неизвестный формат снапшота: {doc.get('format')!r}")
    return doc


# --- источник ---------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    blob: bytes
    meta: dict[str, Any]


class SnapshotSource:
    """Собирает и кэширует запечатанный снапшот БД своей ноды."""

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
        """Актуальный снапшот; пересобирает по расписанию. None — собрать не вышло."""
        async with self._lock:
            if self._due():
                try:
                    await self._rebuild()
                except Exception:  # noqa: BLE001 — прежний снапшот лучше никакого
                    log.exception("Бэкап снапшота vpn: сбой сборки")
            return self._cached

    async def _rebuild(self) -> None:
        doc = build_document(self._node_id, await dump_tables(self._conn_getter()))
        plaintext = canonical_bytes(doc)
        digest = plain_hash(plaintext)
        self._built_at = self._clock()
        self._dirty = False
        if self._cached is not None and self._cached.meta["hash"] == digest:
            return  # то же содержимое — прежний блоб, напарнику нечего тянуть
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
        """БД изменена: пересобрать вне плановой очереди и позвать напарника."""
        self._dirty = True
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._flush(), name="vpn-snapshot-flush")

    async def _flush(self) -> None:
        await asyncio.sleep(self._debounce)
        snap = await self.current()
        if snap is not None and self._notify is not None:
            try:
                await self._notify()
            except Exception as exc:  # noqa: BLE001 — напарник догонит плановым опросом
                log.debug("Бэкап снапшота vpn: напарник не уведомлён (%s)", exc)

    async def handle_get(self, args: dict[str, Any]) -> dict[str, Any]:
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


# --- приёмник (напарник) -------------------------------------------------------


Ask = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class SnapshotReceiver:
    """Тянет снапшот у напарника и кладёт в ``BackupStore``. Больше ни от кого."""

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
        """True — принята новая копия."""
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
            except Exception as exc:  # noqa: BLE001 — сосед мог быть недоступен
                log.debug("Бэкап снапшота vpn: не удалось забрать у %s (%s)", self._partner, exc)


# --- связка для службы vpn -----------------------------------------------------


def snapshot_enabled(settings: Settings) -> bool:
    """Источник: есть ключ получателя и напарник. Приёмник — достаточно напарника."""
    b = settings.backup
    return bool(b.recipient_public_key.strip() and b.partner.strip())


class SnapshotBackup:
    """То, что служба vpn держит для бэкапа БД: источник и/или приёмник."""

    def __init__(
        self, source: SnapshotSource | None, receiver: SnapshotReceiver | None
    ) -> None:
        self.source = source
        self.receiver = receiver

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
        return specs

    def touch(self) -> None:
        if self.source is not None:
            self.source.touch()

    async def handle(self, action: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """None — действие не наше."""
        if action == ACTION_GET_SNAPSHOT and self.source is not None:
            return await self.source.handle_get(args)
        if action == ACTION_SNAPSHOT_POKE and self.receiver is not None:
            self.receiver.poke()
            return {"ok": True}
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
    """Бэкап снапшота для службы vpn; None — выключено ([backup].partner пуст)."""
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
    return SnapshotBackup(source, SnapshotReceiver(settings, store, ask_partner))
