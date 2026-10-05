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
- подэтап (c) кладёт сюда же ``<нода>/snapshots/…`` (динамика ``vpn_peers``).

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


def backups_dir(state_path: str | Path) -> Path:
    """Каталог бэкапов рядом с node-state.json (``./data/backups``)."""
    return Path(state_path).parent / BACKUPS_DIRNAME


@dataclass(frozen=True)
class StoredIdentity:
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
