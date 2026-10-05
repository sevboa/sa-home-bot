"""Статическая identity vpn-сервера: сбор, запечатывание, публикация (39.0.8(b)).

Что входит (всё, без чего пересобранный на том же IP сервер не примет старых
клиентов), одним JSON-документом ``format = IDENTITY_FORMAT``:

- ``awg``: приватный ключ серверного интерфейса (``awg show <iface> private-key``
  под уже существующим узким sudoers ``awg show *`` — ``awg0.conf`` читает только
  root) и параметры обфускации ``jc/jmin/jmax/s1/s2/h1..h4`` из ``[vpn]``
  (служба обязана держать их равными ``awg0.conf``, см. ``VpnConfig``);
- ``reality``: ``privateKey`` и ``shortIds`` из inbound-а xray (``~/.config/xray/
  config.json`` — файл принадлежит пользователю ноды, sudo не нужен);
- ``config``: секции ``[vpn]`` и ``[vpn.reality]`` из config.toml.

Пиры (``[Peer]`` в awg0.conf, клиенты xray) — динамика, подэтап (c), сюда не входят.

Публикация идёт через ``ConfigReplicator``: пакет ``vpn-identity.<нода>.toml`` в
каталоге пакетов — TOML-конверт с base64-блобом ``sealed.seal``. Блоб при каждом
запечатывании другой (эфемерный ключ), поэтому ревизия растёт только когда
изменился ОТКРЫТЫЙ текст: его sha256 лежит в сайдкаре ``….src.json``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sa_home_bot.backup import sealed
from sa_home_bot.config import Settings
from sa_home_bot.node.instances import InstanceStore, atomic_write
from sa_home_bot.vpn import protocol as vpn_protocol

log = logging.getLogger(__name__)

IDENTITY_SERVICE = "vpn-identity"
IDENTITY_FORMAT = "sa-home-bot/vpn-identity/1"
SRC_SUFFIX = ".src.json"

OBFUSCATION_FIELDS = ("jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4")


class IdentityError(Exception):
    """Не удалось собрать/разобрать identity (источник недоступен, битый пакет)."""


def collect_identity(
    settings: Settings, *, awg_private_key: str | None, xray_config: dict | None
) -> dict[str, Any]:
    """Собрать identity из конфига и прочитанных секретов (без ввода-вывода)."""
    vpn = settings.vpn
    transports = vpn.transports or [vpn_protocol.TRANSPORT_AWG]
    doc: dict[str, Any] = {"format": IDENTITY_FORMAT}
    if vpn_protocol.TRANSPORT_AWG in transports:
        if not awg_private_key:
            raise IdentityError("нет приватного ключа awg-сервера")
        doc["awg"] = {
            "interface": vpn.interface,
            "private_key": awg_private_key,
            "obfuscation": {name: getattr(vpn, name) for name in OBFUSCATION_FIELDS},
        }
    if vpn_protocol.TRANSPORT_REALITY in transports and vpn.reality is not None:
        doc["reality"] = _reality_secrets(xray_config, vpn.reality.inbound_tag)
    doc["config"] = {
        "vpn": vpn.model_dump(mode="json", exclude={"reality"}),
        "vpn_reality": vpn.reality.model_dump(mode="json") if vpn.reality is not None else None,
    }
    return doc


def _reality_secrets(xray_config: dict | None, inbound_tag: str) -> dict[str, Any]:
    for inbound in (xray_config or {}).get("inbounds", []):
        rs = (inbound.get("streamSettings") or {}).get("realitySettings")
        if rs and inbound.get("tag", inbound_tag) == inbound_tag:
            if not rs.get("privateKey"):
                break
            return {
                "inbound_tag": inbound_tag,
                "private_key": rs["privateKey"],
                "short_ids": list(rs.get("shortIds", [])),
            }
    raise IdentityError("в конфиге xray не найден Reality-inbound с privateKey")


def canonical_bytes(doc: dict[str, Any]) -> bytes:
    """Детерминированная сериализация: одинаковая identity — одинаковые байты."""
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def plain_hash(plaintext: bytes) -> str:
    return "sha256:" + hashlib.sha256(plaintext).hexdigest()


def render_package(source: str, blob: bytes) -> bytes:
    """TOML-конверт пакета репликации (пакеты едут как текст)."""
    text = (
        f'format = "{IDENTITY_FORMAT}"\n'
        f"source = {json.dumps(source)}\n"
        f'sealed = "{base64.b64encode(blob).decode("ascii")}"\n'
    )
    return text.encode("utf-8")


def parse_package(data: bytes) -> tuple[str, bytes]:
    """Пакет → (источник, запечатанный блоб)."""
    try:
        doc = tomllib.loads(data.decode("utf-8"))
        if doc.get("format") != IDENTITY_FORMAT:
            raise IdentityError(f"неизвестный формат пакета: {doc.get('format')!r}")
        return str(doc["source"]), base64.b64decode(doc["sealed"], validate=True)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, binascii.Error) as exc:
        raise IdentityError(f"пакет identity повреждён: {exc}") from exc


def open_identity(private_key: bytes, blob: bytes) -> dict[str, Any]:
    """Расшифровать блоб (на alfred) обратно в документ identity."""
    try:
        return json.loads(sealed.open_sealed(private_key, blob))
    except (sealed.SealedError, ValueError) as exc:
        raise IdentityError(f"не удалось расшифровать identity: {exc}") from exc


def read_xray_config(path: str) -> dict | None:
    try:
        return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def partner_wants(settings: Settings) -> str:
    """Id напарника, чью копию эта нода хранит; пусто — не хранит."""
    return settings.backup.partner.strip()


def publishing_enabled(settings: Settings, vpn_assigned: bool) -> bool:
    b = settings.backup
    return bool(vpn_assigned and b.recipient_public_key.strip() and b.partner.strip())


class IdentityPublisher:
    """Держит в каталоге пакетов актуальный запечатанный пакет своей identity."""

    def __init__(
        self,
        settings: Settings,
        store: InstanceStore,
        node_id: str,
        *,
        read_awg_private_key: Callable[[], Awaitable[str]],
        read_xray: Callable[[], dict | None] | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._node_id = node_id
        self._read_awg = read_awg_private_key
        self._read_xray = read_xray or (lambda: read_xray_config(settings.backup.xray_config))
        self._recipient = sealed.load_key(settings.backup.recipient_public_key)

    def _src_path(self) -> Path | None:
        pkg = self._store.package_path(IDENTITY_SERVICE, self._node_id)
        return pkg.with_name(f"{IDENTITY_SERVICE}.{self._node_id}{SRC_SUFFIX}") if pkg else None

    async def publish_if_changed(self) -> bool:
        """Собрать identity; если открытый текст изменился — перезаписать пакет.

        Ревизию поднимает уже ``InstanceStore.refresh`` (его зовёт репликатор
        сразу после этого хука) — по смене содержимого файла. True — файл
        переписан. Ошибка сбора НЕ затирает прежний пакет: частичная identity
        хуже прошлой полной.
        """
        awg_key = None
        transports = self._settings.vpn.transports or [vpn_protocol.TRANSPORT_AWG]
        try:
            if vpn_protocol.TRANSPORT_AWG in transports:
                awg_key = (await self._read_awg()).strip()
            doc = collect_identity(
                self._settings, awg_private_key=awg_key, xray_config=self._read_xray()
            )
        except IdentityError as exc:
            log.warning("Бэкап identity: %s — пакет не обновлён", exc)
            return False
        plaintext = canonical_bytes(doc)
        digest = plain_hash(plaintext)
        pkg, src = self._store.package_path(IDENTITY_SERVICE, self._node_id), self._src_path()
        if pkg is None or src is None:
            return False
        if pkg.exists() and _read_src_hash(src) == digest:
            return False
        atomic_write(pkg, render_package(self._node_id, sealed.seal(self._recipient, plaintext)))
        atomic_write(src, json.dumps({"plain_hash": digest}).encode("utf-8"))
        return True


def _read_src_hash(path: Path) -> str:
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("plain_hash", ""))
    except (OSError, ValueError):
        return ""


def replicator_hooks(
    settings: Settings, store: InstanceStore, node_id: str, *, vpn_assigned: bool
) -> dict[str, Any]:
    """Аргументы ``ConfigReplicator`` для бэкапа identity (пустой dict — выключено).

    Пассивный приём включён, если задан ``[backup].partner``: чужая копия
    принимается ТОЛЬКО от него и ложится в ``BackupStore``. Публикация своей —
    если дополнительно на ноде есть служба vpn и ``recipient_public_key``.
    """
    partner = partner_wants(settings)
    if not partner:
        return {}
    from sa_home_bot.backup.store import BackupStore, backups_dir
    from sa_home_bot.vpn.awg import RealAwgBackend

    backups = BackupStore(backups_dir(settings.node.state_path))

    def passive(service: str, instance: str) -> bool:
        return service == IDENTITY_SERVICE and instance == partner

    def on_applied(meta, data: bytes) -> None:
        if meta.service != IDENTITY_SERVICE or meta.instance != partner:
            return
        source, blob = parse_package(data)
        if source != meta.instance:
            raise IdentityError(f"пакет {meta.instance} заявляет источник {source}")
        backups.save_identity(source, blob, meta)

    hooks: dict[str, Any] = {"passive": passive, "on_applied": on_applied}
    if publishing_enabled(settings, vpn_assigned):
        try:
            publisher = IdentityPublisher(
                settings, store, node_id,
                read_awg_private_key=RealAwgBackend(settings.vpn.interface).server_private_key,
            )
        except sealed.SealedError as exc:
            log.warning("Бэкап identity выключен: [backup].recipient_public_key негоден (%s)", exc)
        else:
            hooks["before_announce"] = publisher.publish_if_changed
    return hooks
