from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sa_home_bot.node.instances import InstanceMeta
from sa_home_bot.utils.files import atomic_write

log = logging.getLogger(__name__)

BACKUPS_DIRNAME = "backups"
IDENTITY_FILE = "identity.sealed"
IDENTITY_META_FILE = "identity.meta.json"
HISTORY_DIR = "history"
HISTORY_KEEP = 5

_NON_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")

SNAPSHOTS_DIR = "snapshots"
SNAPSHOT_FILE = "latest.sealed"
SNAPSHOT_META_FILE = "latest.meta.json"
SNAPSHOT_NONEMPTY_FILE = "last_nonempty.sealed"
SNAPSHOT_NONEMPTY_META_FILE = "last_nonempty.meta.json"
SNAPSHOT_HISTORY_KEEP = 24


def snapshot_is_empty(meta: dict) -> bool:
    return int((meta.get("rows") or {}).get("vpn_peers", 0)) == 0


def _read_meta(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _meta_json(info: dict) -> bytes:
    return json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")


def backups_dir(state_path: str | Path) -> Path:
    return Path(state_path).parent / BACKUPS_DIRNAME


@dataclass(frozen=True)
class StoredIdentity:
    blob: bytes
    meta: dict


@dataclass(frozen=True)
class StoredSnapshot:
    blob: bytes
    meta: dict


def _meta_path(blob_path: Path) -> Path:
    return blob_path.with_name(blob_path.name[: -len(".sealed")] + ".meta.json")


def _history_label(blob_path: Path, prefix: str) -> str:
    return blob_path.name[len(prefix) + 1 : -len(".sealed")]


@dataclass(frozen=True)
class _VersionedSlot:
    directory: Path
    prefix: str
    latest_file: str
    latest_meta_file: str
    keep: int

    @property
    def latest(self) -> Path:
        return self.directory / self.latest_file

    @property
    def latest_meta(self) -> Path:
        return self.directory / self.latest_meta_file

    def history(self) -> list[Path]:
        history_dir = self.directory / HISTORY_DIR
        return sorted(history_dir.glob(f"{self.prefix}.*.sealed")) if history_dir.exists() else []

    def archive_latest(self) -> None:
        if not self.latest.exists():
            return
        stamp = _NON_FILENAME_CHARS.sub("_", _read_meta(self.latest_meta).get("stored_at", ""))
        stamp = stamp or datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%f")
        history_dir = self.directory / HISTORY_DIR
        atomic_write(history_dir / f"{self.prefix}.{stamp}.sealed", self.latest.read_bytes())
        if self.latest_meta.exists():
            atomic_write(
                history_dir / f"{self.prefix}.{stamp}.meta.json", self.latest_meta.read_bytes()
            )
        for old in self.history()[: -self.keep]:
            old.unlink(missing_ok=True)
            _meta_path(old).unlink(missing_ok=True)

    def history_versions(self, with_empty_flag: bool) -> list[dict]:
        versions = []
        for path in reversed(self.history()):
            meta = _read_meta(_meta_path(path))
            if with_empty_flag:
                meta.setdefault("empty", snapshot_is_empty(meta))
            versions.append({"label": _history_label(path, self.prefix), "meta": meta})
        return versions

    def read_history(self, label: str, with_empty_flag: bool) -> tuple[bytes, dict] | None:
        for path in self.history():
            if _history_label(path, self.prefix) == label:
                meta = _read_meta(_meta_path(path))
                if with_empty_flag:
                    meta.setdefault("empty", snapshot_is_empty(meta))
                return path.read_bytes(), meta
        return None


class BackupStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def node_dir(self, node: str) -> Path:
        return self.root / (_NON_FILENAME_CHARS.sub("_", node) or "_")

    def _identity_slot(self, node: str) -> _VersionedSlot:
        return _VersionedSlot(
            self.node_dir(node), "identity", IDENTITY_FILE, IDENTITY_META_FILE, HISTORY_KEEP
        )

    def _snapshot_slot(self, node: str) -> _VersionedSlot:
        return _VersionedSlot(
            self.snapshots_dir(node),
            "snapshot",
            SNAPSHOT_FILE,
            SNAPSHOT_META_FILE,
            SNAPSHOT_HISTORY_KEEP,
        )

    def snapshots_dir(self, node: str) -> Path:
        return self.node_dir(node) / SNAPSHOTS_DIR

    def save_identity(self, node: str, blob: bytes, meta: InstanceMeta) -> None:
        slot = self._identity_slot(node)
        slot.archive_latest()
        info = {
            "source": node,
            "rev": meta.rev,
            "hash": meta.hash,
            "updated_at": meta.updated_at,
            "stored_at": datetime.now(tz=UTC).isoformat(),
        }
        atomic_write(slot.latest, blob)
        atomic_write(slot.latest_meta, _meta_json(info))
        log.info("Бэкап identity ноды %s сохранён (ревизия %d)", node, meta.rev)

    def load_identity(self, node: str) -> StoredIdentity | None:
        slot = self._identity_slot(node)
        if not slot.latest.exists():
            return None
        return StoredIdentity(blob=slot.latest.read_bytes(), meta=_read_meta(slot.latest_meta))

    def history(self, node: str) -> list[Path]:
        return self._identity_slot(node).history()

    def save_snapshot(self, node: str, blob: bytes, meta: dict) -> None:
        slot = self._snapshot_slot(node)
        slot.archive_latest()
        empty = snapshot_is_empty(meta)
        info = {**meta, "stored_at": datetime.now(tz=UTC).isoformat(), "empty": empty}
        raw = _meta_json(info)
        atomic_write(slot.latest, blob)
        atomic_write(slot.latest_meta, raw)
        if not empty:
            atomic_write(slot.directory / SNAPSHOT_NONEMPTY_FILE, blob)
            atomic_write(slot.directory / SNAPSHOT_NONEMPTY_META_FILE, raw)
        log.info(
            "Бэкап снапшота БД ноды %s сохранён (ревизия %s, пиров %s)",
            node,
            meta.get("rev"),
            (meta.get("rows") or {}).get("vpn_peers"),
        )

    def load_snapshot(self, node: str) -> StoredSnapshot | None:
        return self._load_snapshot_file(node, SNAPSHOT_FILE, SNAPSHOT_META_FILE)

    def load_last_nonempty(self, node: str) -> StoredSnapshot | None:
        return self._load_snapshot_file(node, SNAPSHOT_NONEMPTY_FILE, SNAPSHOT_NONEMPTY_META_FILE)

    def _load_snapshot_file(
        self, node: str, blob_file: str, meta_file: str
    ) -> StoredSnapshot | None:
        directory = self.snapshots_dir(node)
        if not (directory / blob_file).exists():
            return None
        return StoredSnapshot(
            (directory / blob_file).read_bytes(), _read_meta(directory / meta_file)
        )

    def snapshot_history(self, node: str) -> list[Path]:
        return self._snapshot_slot(node).history()

    def identity_versions(self, node: str) -> list[dict]:
        slot = self._identity_slot(node)
        versions = []
        if slot.latest.exists():
            versions.append({"label": "latest", "meta": _read_meta(slot.latest_meta)})
        return versions + slot.history_versions(with_empty_flag=False)

    def snapshot_versions(self, node: str) -> list[dict]:
        slot = self._snapshot_slot(node)
        versions = []
        if slot.latest.exists():
            versions.append({"label": "latest", "meta": _read_meta(slot.latest_meta)})
        nonempty = slot.directory / SNAPSHOT_NONEMPTY_FILE
        if nonempty.exists():
            versions.append(
                {
                    "label": "last_nonempty",
                    "meta": _read_meta(slot.directory / SNAPSHOT_NONEMPTY_META_FILE),
                }
            )
        return versions + slot.history_versions(with_empty_flag=True)

    def read_identity(self, node: str, label: str = "latest") -> StoredIdentity | None:
        if label == "latest":
            return self.load_identity(node)
        found = self._identity_slot(node).read_history(label, with_empty_flag=False)
        return StoredIdentity(*found) if found else None

    def read_snapshot(self, node: str, label: str = "latest") -> StoredSnapshot | None:
        if label == "latest":
            return self.load_snapshot(node)
        if label == "last_nonempty":
            return self.load_last_nonempty(node)
        found = self._snapshot_slot(node).read_history(label, with_empty_flag=True)
        return StoredSnapshot(*found) if found else None
