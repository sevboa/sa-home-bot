"""Восстановление vpn-сервера из бэкапа напарника — сторона alfred (39.0.8(d)).

alfred — единственный, кто может расшифровать копии (``[backup].private_key_file``).
Он забирает запечатанные блобы у ноды-хранителя (действия ``backup_store_list`` /
``backup_store_get``, ``backup/serve.py``), расшифровывает и собирает **бандл**:

    {format, node, identity_label, snapshot_label, identity, snapshot}

— дальше бандл либо раскладывается в каталог (``--dry-run``: проверка бэкапа без
касания нод), либо уезжает на целевую ноду по ssh и применяется там
(``backup/apply.py``). Расшифрованная identity — это приватные ключи сервера, поэтому
канал — ssh (ключи, а не токен роя, который знает каждая нода), а на диске она
лежит только 0600 и удаляется после применения.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sa_home_bot.backup import identity as identity_mod
from sa_home_bot.backup import sealed
from sa_home_bot.backup import snapshot as snapshot_mod
from sa_home_bot.backup.serve import ACTION_STORE_GET, ACTION_STORE_LIST

BUNDLE_FORMAT = "sa-home-bot/vpn-restore/1"
REMOTE_NODECTL = "~/.local/bin/nodectl"

Ask = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class RestoreError(Exception):
    """Восстановление невозможно или отказано (читаемая причина — в тексте)."""


# --- забор и расшифровка --------------------------------------------------------


async def list_versions(ask: Ask, node: str) -> dict[str, Any]:
    return await ask(ACTION_STORE_LIST, {"node": node})


async def _fetch(ask: Ask, node: str, kind: str, label: str) -> tuple[bytes, dict]:
    resp = await ask(ACTION_STORE_GET, {"node": node, "kind": kind, "label": label})
    try:
        return base64.b64decode(resp["sealed"], validate=True), dict(resp.get("meta") or {})
    except (KeyError, TypeError, ValueError) as exc:
        raise RestoreError(f"ответ хранителя повреждён: {exc}") from exc


def pick_snapshot(versions: list[dict], requested: str | None) -> str | None:
    """Метка снапшота для восстановления; None — снапшотов у хранителя нет.

    По умолчанию ``latest``, но пустой ``latest`` при наличии ``last_nonempty`` —
    отказ: пересобранная нода публикует пустой снапшот, и молча «восстановить» его
    значило бы потерять всех гостей.
    """
    by_label = {v["label"]: v for v in versions}
    if requested:
        if requested not in by_label:
            have = ", ".join(by_label) or "нет"
            raise RestoreError(f"нет снапшота {requested!r} (есть: {have})")
        return requested
    latest = by_label.get("latest")
    if latest is None:
        return None
    if latest["meta"].get("empty") and "last_nonempty" in by_label:
        peers = (by_label["last_nonempty"]["meta"].get("rows") or {}).get("vpn_peers", "?")
        raise RestoreError(
            "снапшот latest пустой (без пиров), а последний непустой есть "
            f"(пиров: {peers}). Выберите явно: --snapshot last_nonempty "
            "или --snapshot latest, если пустая БД — это то, что нужно"
        )
    return "latest"


async def fetch_bundle(
    ask: Ask,
    node: str,
    private_key: bytes,
    *,
    identity_label: str = "latest",
    snapshot_label: str | None = None,
    with_snapshot: bool = True,
) -> dict[str, Any]:
    """Забрать и расшифровать identity (+ снапшот) ноды ``node`` в бандл."""
    listing = await list_versions(ask, node)
    if not any(v["label"] == identity_label for v in listing.get("identity", [])):
        raise RestoreError(f"у хранителя нет identity {identity_label!r} для {node}")
    blob, id_meta = await _fetch(ask, node, "identity", identity_label)
    try:
        identity = identity_mod.open_identity(private_key, blob)
    except identity_mod.IdentityError as exc:
        raise RestoreError(str(exc)) from exc
    snap_doc = snap_meta = None
    label = None
    if with_snapshot:
        label = pick_snapshot(listing.get("snapshot", []), snapshot_label)
        if label is not None:
            sblob, snap_meta = await _fetch(ask, node, "snapshot", label)
            try:
                snap_doc = snapshot_mod.open_snapshot(private_key, sblob)
            except snapshot_mod.SnapshotError as exc:
                raise RestoreError(str(exc)) from exc
            if snap_doc.get("node") != node:
                raise RestoreError(
                    f"снапшот принадлежит ноде {snap_doc.get('node')!r}, а не {node!r}"
                )
    return {
        "format": BUNDLE_FORMAT,
        "node": node,
        "identity_label": identity_label,
        "snapshot_label": label,
        "identity_meta": id_meta,
        "snapshot_meta": snap_meta,
        "identity": identity,
        "snapshot": snap_doc,
    }


# --- сводка (без секретов) ---------------------------------------------------------


def awg_public_key(private_key_b64: str) -> str:
    """Публичный ключ WireGuard по приватному (curve25519 — то же, что X25519)."""
    return sealed.dump_key(sealed.public_from_private(sealed.load_key(private_key_b64)))


def _fingerprint(secret: str) -> str:
    import hashlib

    return hashlib.sha256(secret.encode()).hexdigest()[:10]


def summarize_bundle(bundle: dict[str, Any]) -> str:
    """Человекочитаемая сводка: что будет восстановлено. Секреты — только отпечатки."""
    ident = bundle["identity"]
    lines = [f"Нода: {bundle['node']}", f"Identity: {bundle['identity_label']}"]
    meta = bundle.get("identity_meta") or {}
    if meta.get("updated_at"):
        lines.append(f"  собрана источником: {meta['updated_at']}")
    cfg = ident.get("config", {}).get("vpn", {})
    if cfg.get("endpoint_host"):
        lines.append(f"  endpoint: {cfg['endpoint_host']}")
    awg = ident.get("awg")
    if awg:
        pub = awg_public_key(awg["private_key"])
        obf = " ".join(f"{k}={v}" for k, v in awg["obfuscation"].items())
        lines.append(f"  awg: интерфейс {awg['interface']}, публичный ключ сервера {pub}")
        lines.append(f"       обфускация: {obf}")
    rea = ident.get("reality")
    if rea:
        pubr = (ident.get("config", {}).get("vpn_reality") or {}).get("server_public_key", "?")
        lines.append(
            f"  reality: inbound {rea['inbound_tag']}, публичный ключ {pubr}, "
            f"short_id: {', '.join(rea['short_ids']) or '—'}, "
            f"приватный (отпечаток) {_fingerprint(rea['private_key'])}"
        )
    snap = bundle.get("snapshot")
    if snap is None:
        lines.append("Снапшот БД: нет (будет восстановлена только identity)")
    else:
        smeta = bundle.get("snapshot_meta") or {}
        rows = {name: len(t["rows"]) for name, t in snap["tables"].items()}
        peers = snapshot_mod.summarize(snap)
        lines.append(
            f"Снапшот БД: {bundle['snapshot_label']} (снят {smeta.get('taken_at', '?')}), "
            f"пиров {rows.get('vpn_peers', 0)} (активных {peers.get('vpn_peers_active', 0)}), "
            f"допусков {rows.get('vpn_chat_access', 0)}"
        )
        if not rows.get("vpn_peers"):
            lines.append("  ВНИМАНИЕ: снапшот пустой — гостей не вернуть")
        skipped = ", ".join(sorted(snapshot_skipped_tables(snap)))
        lines.append(f"  по таблицам: {rows}; не переносятся: {skipped}")
    return "\n".join(lines)


def snapshot_skipped_tables(snap: dict[str, Any]) -> set[str]:
    from sa_home_bot.backup.apply import SKIP_TABLES

    return set(snap["tables"]) & SKIP_TABLES


# --- каталог (dry-run) и перенос -----------------------------------------------------


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(path, 0o600)


def _dump(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def write_dir(directory: Path, bundle: dict[str, Any]) -> list[Path]:
    """Разложить бандл: ``identity.json``, ``snapshot.json``, ``meta.json``, ``summary.txt``.

    Каталог 0700, файлы 0600. Непустой существующий каталог — отказ (не мешаем
    разные бэкапы и не затираем чужое).
    """
    directory = Path(directory).expanduser()
    if directory.exists() and any(directory.iterdir()):
        raise RestoreError(f"каталог {directory} не пуст — укажите новый или пустой")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    written = []

    def put(name: str, data: bytes) -> None:
        _write_private(directory / name, data)
        written.append(directory / name)

    put("identity.json", _dump(bundle["identity"]))
    if bundle.get("snapshot") is not None:
        put("snapshot.json", _dump(bundle["snapshot"]))
    meta = {k: bundle.get(k) for k in (
        "format", "node", "identity_label", "snapshot_label", "identity_meta", "snapshot_meta"
    )}
    put("meta.json", _dump(meta))
    put("summary.txt", (summarize_bundle(bundle) + "\n").encode("utf-8"))
    return written


def bundle_to_bytes(bundle: dict[str, Any]) -> bytes:
    return _dump(bundle)


def load_bundle(path: Path) -> dict[str, Any]:
    """Бандл из одного JSON-файла или из каталога ``write_dir``."""
    path = Path(path).expanduser()
    try:
        if path.is_dir():
            meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
            bundle = {**meta, "identity": json.loads((path / "identity.json").read_text("utf-8"))}
            snap = path / "snapshot.json"
            bundle["snapshot"] = json.loads(snap.read_text("utf-8")) if snap.exists() else None
        else:
            bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError) as exc:
        raise RestoreError(f"не удалось прочитать бандл {path}: {exc}") from exc
    if bundle.get("format") != BUNDLE_FORMAT:
        raise RestoreError(f"неизвестный формат бандла: {bundle.get('format')!r}")
    return bundle


def stage_over_ssh(host: str, payload: bytes, remote_nodectl: str = REMOTE_NODECTL) -> None:
    """Положить бандл на целевую ноду (stdin ssh → ``nodectl restore-stage``, 0600)."""
    cmd = f"{remote_nodectl} restore-stage"
    res = subprocess.run(["ssh", host, cmd], input=payload, capture_output=True)
    if res.returncode != 0:
        raise RestoreError(
            f"ssh {host}: restore-stage завершился кодом {res.returncode}: "
            f"{res.stderr.decode(errors='replace').strip()}"
        )


def apply_over_ssh(host: str, remote_nodectl: str = REMOTE_NODECTL, extra: str = "") -> int:
    """Интерактивно (нужен tty под пароль sudo): ``nodectl restore-apply --yes``."""
    cmd = f"{remote_nodectl} restore-apply --yes {extra}".strip()
    return subprocess.run(["ssh", "-t", host, cmd]).returncode
