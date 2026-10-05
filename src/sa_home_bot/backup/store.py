"""Хранилище бэкапов у напарника: запечатанные копии чужих нод, пассивно.

Раскладка (корень — ``<каталог node-state>/backups``, т.е. ``data/backups``):

- ``<нода>/identity.sealed`` — последний запечатанный блоб identity ноды-источника
  (сырой бинарный ``sealed.seal``-блоб, 0600);
- ``<нода>/identity.meta.json`` — метаданные: ``source``, ``rev``, ``hash`` (хеш
  пакета репликации, не открытого текста), ``updated_at`` (когда источник собрал),
  ``stored_at`` (когда мы приняли);
- ``<нода>/history/identity.<updated_at>.sealed`` (+ ``.meta.json``) — предыдущие
  версии, не больше ``HISTORY_KEEP``. Нужны на случай пересборки источника:
  свежий пустой сервер сразу публикует НОВУЮ identity, и без истории она
  затёрла бы ровно ту копию, ради которой бэкап и существует;
- ``<нода>/snapshots/…`` — динамика ``vpn_peers`` и квоты (подэтап (c), см. константы
  ниже и ``backup/snapshot.py``).

Расшифровать это может только alfred (приватный ключ есть лишь у него).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sa_home_bot.node.instances import InstanceMeta, atomic_write

log = logging.getLogger(__name__)

BACKUPS_DIRNAME = "backups"
IDENTITY_FILE = "identity.sealed"
IDENTITY_META_FILE = "identity.meta.json"
HISTORY_DIR = "history"
HISTORY_KEEP = 5

_SAFE = re.compile(r"[^A-Za-z0-9._-]")

# Снапшоты VPN-БД (39.0.8(c)): ``<нода>/snapshots/latest.sealed`` + ``latest.meta.json``
# (rev, hash, taken_at, stored_at, rows по таблицам, empty), ``history/snapshot.<stored_at>
# .sealed`` — предыдущие, не больше SNAPSHOT_HISTORY_KEEP; ``last_nonempty.sealed`` —
# последний снапшот С пирами, его пустые не вытесняют: пересобранная нода с пустой БД
# публикует пустой снапшот, и без этого он за SNAPSHOT_HISTORY_KEEP циклов выдавил бы
# из истории ровно ту копию, ради которой бэкап и нужен.
SNAPSHOTS_DIR = "snapshots"
SNAPSHOT_FILE = "latest.sealed"
SNAPSHOT_META_FILE = "latest.meta.json"
SNAPSHOT_NONEMPTY_FILE = "last_nonempty.sealed"
SNAPSHOT_NONEMPTY_META_FILE = "last_nonempty.meta.json"
SNAPSHOT_HISTORY_KEEP = 24


def snapshot_is_empty(meta: dict) -> bool:
    """В снапшоте нет ни одной строки vpn_peers (по счётчикам в meta)."""
    return int((meta.get("rows") or {}).get("vpn_peers", 0)) == 0


def _read_meta(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def backups_dir(state_path: str | Path) -> Path:
    """Каталог бэкапов рядом с node-state.json (``./data/backups``)."""
    return Path(state_path).parent / BACKUPS_DIRNAME


@dataclass(frozen=True)
class StoredIdentity:
    blob: bytes
    meta: dict


@dataclass(frozen=True)
class StoredSnapshot:
    blob: bytes
    meta: dict


class BackupStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def node_dir(self, node: str) -> Path:
        # Имя ноды — из пакета соседа: чистим, чтобы '../' не вышло за корень.
        return self.root / (_SAFE.sub("_", node) or "_")

    def identity_path(self, node: str) -> Path:
        return self.node_dir(node) / IDENTITY_FILE

    def meta_path(self, node: str) -> Path:
        return self.node_dir(node) / IDENTITY_META_FILE

    def save_identity(self, node: str, blob: bytes, meta: InstanceMeta) -> None:
        """Принять новую копию; прежнюю убрать в историю."""
        self._archive_current(node)
        info = {
            "source": node,
            "rev": meta.rev,
            "hash": meta.hash,
            "updated_at": meta.updated_at,
            "stored_at": datetime.now(tz=UTC).isoformat(),
        }
        atomic_write(self.identity_path(node), blob)
        atomic_write(
            self.meta_path(node), json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")
        )
        log.info("Бэкап identity ноды %s сохранён (ревизия %d)", node, meta.rev)

    def load_identity(self, node: str) -> StoredIdentity | None:
        path, meta_path = self.identity_path(node), self.meta_path(node)
        if not path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        return StoredIdentity(blob=path.read_bytes(), meta=meta)

    def history(self, node: str) -> list[Path]:
        """Архивные блобы, старые первыми."""
        hist = self.node_dir(node) / HISTORY_DIR
        return sorted(hist.glob("identity.*.sealed")) if hist.exists() else []

    # --- снапшоты VPN-БД (39.0.8(c)) ---

    def snapshots_dir(self, node: str) -> Path:
        return self.node_dir(node) / SNAPSHOTS_DIR

    def save_snapshot(self, node: str, blob: bytes, meta: dict) -> None:
        """Принять снапшот; прежний — в историю; непустой — ещё и в last_nonempty."""
        d = self.snapshots_dir(node)
        cur, cur_meta = d / SNAPSHOT_FILE, d / SNAPSHOT_META_FILE
        if cur.exists():
            stamp = _SAFE.sub("_", _read_meta(cur_meta).get("stored_at", ""))
            stamp = stamp or datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%f")
            hist = d / HISTORY_DIR
            atomic_write(hist / f"snapshot.{stamp}.sealed", cur.read_bytes())
            if cur_meta.exists():
                atomic_write(hist / f"snapshot.{stamp}.meta.json", cur_meta.read_bytes())
            for old in self.snapshot_history(node)[:-SNAPSHOT_HISTORY_KEEP]:
                old.unlink(missing_ok=True)
                old.with_name(old.name[: -len(".sealed")] + ".meta.json").unlink(missing_ok=True)
        empty = snapshot_is_empty(meta)
        info = {**meta, "stored_at": datetime.now(tz=UTC).isoformat(), "empty": empty}
        raw = json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")
        atomic_write(cur, blob)
        atomic_write(cur_meta, raw)
        if not empty:
            atomic_write(d / SNAPSHOT_NONEMPTY_FILE, blob)
            atomic_write(d / SNAPSHOT_NONEMPTY_META_FILE, raw)
        log.info(
            "Бэкап снапшота БД ноды %s сохранён (ревизия %s, пиров %s)",
            node, meta.get("rev"), (meta.get("rows") or {}).get("vpn_peers"),
        )

    def load_snapshot(self, node: str) -> StoredSnapshot | None:
        d = self.snapshots_dir(node)
        if not (d / SNAPSHOT_FILE).exists():
            return None
        return StoredSnapshot((d / SNAPSHOT_FILE).read_bytes(), _read_meta(d / SNAPSHOT_META_FILE))

    def load_last_nonempty(self, node: str) -> StoredSnapshot | None:
        d = self.snapshots_dir(node)
        if not (d / SNAPSHOT_NONEMPTY_FILE).exists():
            return None
        return StoredSnapshot(
            (d / SNAPSHOT_NONEMPTY_FILE).read_bytes(),
            _read_meta(d / SNAPSHOT_NONEMPTY_META_FILE),
        )

    def snapshot_history(self, node: str) -> list[Path]:
        """Архивные снапшоты, старые первыми."""
        hist = self.snapshots_dir(node) / HISTORY_DIR
        return sorted(hist.glob("snapshot.*.sealed")) if hist.exists() else []

    # --- выдача версий для восстановления (39.0.8(d)) ---

    @staticmethod
    def _stamp(path: Path, prefix: str) -> str:
        return path.name[len(prefix) + 1 : -len(".sealed")]

    def identity_versions(self, node: str) -> list[dict]:
        """Версии identity, новые первыми: ``{label, meta}``; ``latest`` — текущая."""
        out: list[dict] = []
        if self.identity_path(node).exists():
            out.append({"label": "latest", "meta": _read_meta(self.meta_path(node))})
        for path in reversed(self.history(node)):
            meta = _read_meta(path.with_name(path.name[: -len(".sealed")] + ".meta.json"))
            out.append({"label": self._stamp(path, "identity"), "meta": meta})
        return out

    def snapshot_versions(self, node: str) -> list[dict]:
        """Версии снапшота: ``latest``, ``last_nonempty``, затем history (новые первыми)."""
        d = self.snapshots_dir(node)
        out: list[dict] = []
        if (d / SNAPSHOT_FILE).exists():
            out.append({"label": "latest", "meta": _read_meta(d / SNAPSHOT_META_FILE)})
        if (d / SNAPSHOT_NONEMPTY_FILE).exists():
            out.append(
                {"label": "last_nonempty", "meta": _read_meta(d / SNAPSHOT_NONEMPTY_META_FILE)}
            )
        for path in reversed(self.snapshot_history(node)):
            meta = _read_meta(path.with_name(path.name[: -len(".sealed")] + ".meta.json"))
            meta.setdefault("empty", snapshot_is_empty(meta))
            out.append({"label": self._stamp(path, "snapshot"), "meta": meta})
        return out

    def read_identity(self, node: str, label: str = "latest") -> StoredIdentity | None:
        """Блоб по метке. Метка сверяется со списком файлов, а не склеивается в путь."""
        if label == "latest":
            return self.load_identity(node)
        for path in self.history(node):
            if self._stamp(path, "identity") == label:
                meta = _read_meta(path.with_name(path.name[: -len(".sealed")] + ".meta.json"))
                return StoredIdentity(path.read_bytes(), meta)
        return None

    def read_snapshot(self, node: str, label: str = "latest") -> StoredSnapshot | None:
        if label == "latest":
            return self.load_snapshot(node)
        if label == "last_nonempty":
            return self.load_last_nonempty(node)
        for path in self.snapshot_history(node):
            if self._stamp(path, "snapshot") == label:
                meta = _read_meta(path.with_name(path.name[: -len(".sealed")] + ".meta.json"))
                meta.setdefault("empty", snapshot_is_empty(meta))
                return StoredSnapshot(path.read_bytes(), meta)
        return None

    def _archive_current(self, node: str) -> None:
        cur, cur_meta = self.identity_path(node), self.meta_path(node)
        if not cur.exists():
            return
        try:
            stamp = json.loads(cur_meta.read_text(encoding="utf-8")).get("stored_at", "")
        except (OSError, ValueError):
            stamp = ""
        stamp = _SAFE.sub("_", stamp) or datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%f")
        hist = self.node_dir(node) / HISTORY_DIR
        hist.mkdir(parents=True, exist_ok=True)
        atomic_write(hist / f"identity.{stamp}.sealed", cur.read_bytes())
        if cur_meta.exists():
            atomic_write(hist / f"identity.{stamp}.meta.json", cur_meta.read_bytes())
        for old in self.history(node)[:-HISTORY_KEEP]:
            old.unlink(missing_ok=True)
            old.with_name(old.name[: -len(".sealed")] + ".meta.json").unlink(missing_ok=True)
