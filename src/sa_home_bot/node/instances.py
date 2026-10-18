from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

PACKAGE_SUFFIX = ".toml"
META_SUFFIX = ".rev.json"
INSTANCES_DIRNAME = "instances"

SATELLITE_GUESTS = ".guests"
SATELLITE_SUFFIXES = (SATELLITE_GUESTS,)


def slot_key(service: str, instance: str) -> str:
    return f"{service}@{instance}" if instance else service


def base_instance(instance: str) -> str:
    for suffix in SATELLITE_SUFFIXES:
        if instance.endswith(suffix):
            return instance[: -len(suffix)]
    return instance


def is_satellite(instance: str) -> bool:
    return base_instance(instance) != instance


def guests_package_path(package_path: Path) -> Path:
    return package_path.with_name(
        package_path.name[: -len(PACKAGE_SUFFIX)] + SATELLITE_GUESTS + PACKAGE_SUFFIX
    )


@dataclass(frozen=True)
class InstanceMeta:
    service: str
    instance: str
    rev: int
    hash: str
    updated_at: str
    origin_node: str

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "instance": self.instance,
            "rev": self.rev,
            "hash": self.hash,
            "updated_at": self.updated_at,
            "origin_node": self.origin_node,
        }

    @classmethod
    def from_dict(cls, data: dict) -> InstanceMeta:
        return cls(
            service=str(data.get("service", "")),
            instance=str(data.get("instance", "")),
            rev=int(data.get("rev", 0)),
            hash=str(data.get("hash", "")),
            updated_at=str(data.get("updated_at", "")),
            origin_node=str(data.get("origin_node", "")),
        )

    def wins_over(self, other: InstanceMeta | None) -> bool:
        if other is None:
            return True
        if self.rev != other.rev:
            return self.rev > other.rev
        if self.hash == other.hash:
            return False
        return (self.updated_at, self.origin_node) > (other.updated_at, other.origin_node)


def content_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def instances_dir(config_path: str | Path | None, override: str = "") -> Path | None:
    if override:
        return Path(override)
    if config_path is None:
        return None
    return Path(config_path).parent / INSTANCES_DIRNAME


class InstanceStore:
    def __init__(self, directory: str | Path | None, node_id: str) -> None:
        self.dir = Path(directory) if directory is not None else None
        self._node_id = node_id


    def package_path(self, service: str, instance: str) -> Path | None:
        if self.dir is None:
            return None
        return self.dir / f"{service}.{instance}{PACKAGE_SUFFIX}"

    def meta_path(self, service: str, instance: str) -> Path | None:
        if self.dir is None:
            return None
        return self.dir / f"{service}.{instance}{META_SUFFIX}"


    def read_package(self, service: str, instance: str) -> bytes | None:
        path = self.package_path(service, instance)
        if path is None or not path.exists():
            return None
        return path.read_bytes()

    def read_meta(self, service: str, instance: str) -> InstanceMeta | None:
        path = self.meta_path(service, instance)
        if path is None or not path.exists():
            return None
        try:
            return InstanceMeta.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError) as exc:
            log.warning("Пакет %s: повреждён сайдкар ревизии (%s) — считаю его отсутствующим",
                        slot_key(service, instance), exc)
            return None

    def known(self) -> list[InstanceMeta]:
        if self.dir is None or not self.dir.exists():
            return []
        metas = []
        for path in sorted(self.dir.glob(f"*{PACKAGE_SUFFIX}")):
            if path.name.endswith(META_SUFFIX):
                continue
            service, _, instance = path.name[: -len(PACKAGE_SUFFIX)].partition(".")
            if not instance:
                log.warning(
                    "Пакет %s: имя не вида '<служба>.<инстанс>.toml' — пропускаю", path.name
                )
                continue
            meta = self.read_meta(service, instance)
            if meta is not None:
                metas.append(meta)
        return metas


    def refresh(self, service: str, instance: str) -> InstanceMeta | None:
        data = self.read_package(service, instance)
        if data is None:
            return None
        current = content_hash(data)
        meta = self.read_meta(service, instance)
        if meta is not None and meta.hash == current:
            return None
        new_meta = InstanceMeta(
            service=service,
            instance=instance,
            rev=(meta.rev + 1) if meta is not None else 1,
            hash=current,
            updated_at=datetime.now(tz=UTC).isoformat(),
            origin_node=self._node_id,
        )
        self._write_meta(new_meta)
        log.info(
            "Пакет %s изменён локально — ревизия %d",
            slot_key(service, instance),
            new_meta.rev,
        )
        return new_meta

    def refresh_all(self) -> list[InstanceMeta]:
        if self.dir is None or not self.dir.exists():
            return []
        changed = []
        for path in sorted(self.dir.glob(f"*{PACKAGE_SUFFIX}")):
            service, _, instance = path.name[: -len(PACKAGE_SUFFIX)].partition(".")
            if not instance:
                continue
            meta = self.refresh(service, instance)
            if meta is not None:
                changed.append(meta)
        return changed


    def apply(self, meta: InstanceMeta, data: bytes, *, force: bool = False) -> bool:
        actual = content_hash(data)
        if actual != meta.hash:
            log.warning(
                "Пакет %s: хеш содержимого (%s) не сходится с заявленным (%s) — отвергаю",
                slot_key(meta.service, meta.instance), actual, meta.hash,
            )
            return False
        local = self.read_meta(meta.service, meta.instance)
        if not force and not meta.wins_over(local):
            return False
        if local is not None and local.rev == meta.rev and local.hash != meta.hash:
            log.warning(
                "Пакет %s: расхождение на одной ревизии %d (правили в двух местах) — "
                "принимаю версию ноды %s от %s",
                slot_key(meta.service, meta.instance), meta.rev,
                meta.origin_node, meta.updated_at,
            )
        package = self.package_path(meta.service, meta.instance)
        if package is None:
            return False
        atomic_write(package, data)
        self._write_meta(meta)
        log.info(
            "Пакет %s обновлён с ноды %s — ревизия %d",
            slot_key(meta.service, meta.instance), meta.origin_node, meta.rev,
        )
        return True

    def _write_meta(self, meta: InstanceMeta) -> None:
        path = self.meta_path(meta.service, meta.instance)
        if path is None:
            return
        atomic_write(
            path,
            json.dumps(meta.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"),
        )
