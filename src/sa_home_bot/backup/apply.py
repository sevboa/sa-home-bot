"""Применение бэкапа на пересобранной ноде — сторона целевого сервера (39.0.8(d)).

Бандл (``backup/restore.py``) приходит с alfred по ssh; здесь он превращается в
состояние ноды. Запускается ЛОКАЛЬНО (``nodectl restore-apply``), как и
``nodectl fix``: привилегированные шаги идут через интерактивный ``sudo`` — новых
sudoers-правил не нужно, root ноде по-прежнему не отдаётся.

Что делается (всё идемпотентно; повторный прогон приводит к тому же состоянию):

1. ``awg0.conf`` (root, 0600): ``PrivateKey`` и ``Jc/Jmin/Jmax/S1/S2/H1..H4`` в
   ``[Interface]``. Файла нет (ещё не запускали ``deploy/setup-awg-jeeves.sh``) —
   создаётся минимальный: скрипт при запуске **сохраняет** ключ и обфускацию из
   существующего файла и дописывает остальное, т.е. не перегенерирует. ``nodectl
   fix`` ключей сервера не генерирует вообще — только sudoers/пакеты.
2. Reality: ``/etc/sa-home-reality/reality.env`` (root; так же сохраняется
   ``deploy/setup-reality-server.sh``) и ``privateKey``/``shortIds`` в xray
   ``config.json``, если он уже есть (клиенты в нём не трогаем — их вернёт
   ``reconcile()``).
3. ``config.toml``: таблицы ``[vpn]`` и ``[vpn.reality]`` заменяются на
   сохранённые (локальные пути ``socket``/``db_path``/``apk_cache_dir`` остаются
   прежними); прежний файл сохраняется как ``config.toml.pre-restore``.
4. ``vpn.sqlite``: миграции, затем вставка строк снапшота по ИМЕНАМ колонок
   (неизвестные колонки пропускаются, ``id`` сохраняется — на ``vpn_peers.id``
   ссылается ``vpn_peer_usage.peer_id``). ``vpn_counters`` не переносятся:
   счётчики интерфейса после пересборки нулевые, сэмплер начнёт с нуля.
5. Рестарт того, что уже запущено: ``awg-quick@<iface>`` (sudo systemctl) и
   ``xray.service`` (user-юнит). Службу vpn/ноду НЕ трогаем — их запускает человек
   после проверки; ``reconcile()`` при старте пересоздаст пиров awg и клиентов
   xray из ``vpn_peers``.
6. Маркер ``backup-publish.ok`` (``backup/hold.py``) — только теперь нода
   вправе публиковать свою identity/снапшот напарнику.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from sa_home_bot.backup.hold import allow_publish
from sa_home_bot.backup.snapshot import SNAPSHOT_TABLES
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations

AWG_DIR = Path("/etc/amnezia/amneziawg")
REALITY_ENV = Path("/etc/sa-home-reality/reality.env")
XRAY_UNIT = "xray.service"

# Не переносим: счётчики интерфейса после пересборки нулевые (sample_once считает
# отрицательную дельту от нуля — подставленные старые значения исказили бы учёт).
SKIP_TABLES = frozenset({"vpn_counters"})

# Локальная раскладка каталогов ноды — из восстановленного [vpn] не берём.
LOCAL_VPN_KEYS = ("socket", "db_path", "apk_cache_dir")

_AWG_OBF_KEYS = {
    "jc": "Jc", "jmin": "Jmin", "jmax": "Jmax", "s1": "S1", "s2": "S2",
    "h1": "H1", "h2": "H2", "h3": "H3", "h4": "H4",
}


class ApplyError(Exception):
    """Применить нельзя (чужая нода, БД не пуста, нет конфига…) — ничего не сломано."""


# --- привилегированный ввод-вывод ---------------------------------------------------


class PrivilegedIO(Protocol):
    def read_root(self, path: Path) -> str | None: ...
    def write_root(self, path: Path, text: str) -> None: ...
    def unit_active(self, unit: str, *, user: bool = False) -> bool: ...
    def restart(self, unit: str, *, user: bool = False) -> None: ...


class SudoIO:
    """Реальные операции: интерактивный sudo, пароль нигде не хранится."""

    def read_root(self, path: Path) -> str | None:
        from sa_home_bot.node.fixups import _privileged_exists, _read_privileged

        return _read_privileged(path) if _privileged_exists(path) else None

    def write_root(self, path: Path, text: str) -> None:
        from sa_home_bot.node.fixups import _sudo

        with tempfile.NamedTemporaryFile("w", suffix=".restore", delete=False) as tmp:
            tmp.write(text)
            tmp_path = Path(tmp.name)
        try:
            _sudo(["install", "-D", "-m", "0600", "-o", "root", "-g", "root",
                   str(tmp_path), str(path)])
        finally:
            tmp_path.unlink(missing_ok=True)

    def unit_active(self, unit: str, *, user: bool = False) -> bool:
        argv = ["systemctl", *(["--user"] if user else []), "is-active", "--quiet", unit]
        return subprocess.run(argv).returncode == 0

    def restart(self, unit: str, *, user: bool = False) -> None:
        from sa_home_bot.node.fixups import _run, _sudo

        if user:
            _run(["systemctl", "--user", "restart", unit])
        else:
            _sudo(["systemctl", "restart", unit])


# --- чистые преобразования текста ----------------------------------------------------


def patch_awg_conf(text: str, awg: dict[str, Any]) -> str:
    """Подставить ``PrivateKey`` и обфускацию в ``[Interface]``; остальное не трогать."""
    values = {"PrivateKey": awg["private_key"]}
    values.update({_AWG_OBF_KEYS[k]: v for k, v in awg["obfuscation"].items()})
    lines = text.splitlines() or ["[Interface]"]
    # Границы секции [Interface]
    start = next((i for i, ln in enumerate(lines) if ln.strip().lower() == "[interface]"), None)
    if start is None:
        lines = ["[Interface]", *lines]
        start = 0
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines)
    )
    pending = dict(values)
    for i in range(start + 1, end):
        m = re.match(r"\s*([A-Za-z0-9]+)\s*=", lines[i])
        if m and m.group(1) in pending:
            key = m.group(1)
            lines[i] = f"{key} = {pending.pop(key)}"
    insert = [f"{k} = {v}" for k, v in pending.items()]
    lines[end:end] = insert
    return "\n".join(lines) + "\n"


def reality_env_text(identity: dict[str, Any]) -> str:
    rea = identity["reality"]
    cfg = (identity.get("config", {}).get("vpn_reality")) or {}
    pub = cfg.get("server_public_key") or ""
    short_ids = rea.get("short_ids") or []
    short = short_ids[0] if short_ids else cfg.get("short_id", "")
    if not pub:
        raise ApplyError("в identity нет публичного ключа Reality — reality.env не собрать")
    return (
        f"REALITY_PRIVATE_KEY={rea['private_key']}\n"
        f"REALITY_PUBLIC_KEY={pub}\n"
        f"REALITY_SHORT_ID={short}\n"
    )


def patch_xray_config(doc: dict[str, Any], reality: dict[str, Any]) -> bool:
    """Подставить ``privateKey``/``shortIds`` в Reality-inbound (на месте); False — inbound нет."""
    for inbound in doc.get("inbounds", []):
        rs = (inbound.get("streamSettings") or {}).get("realitySettings")
        if rs is not None and inbound.get("tag", reality["inbound_tag"]) == reality["inbound_tag"]:
            rs["privateKey"] = reality["private_key"]
            rs["shortIds"] = list(reality["short_ids"])
            return True
    return False


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)  # JSON-строка — валидная TOML-строка


def render_table(name: str, values: dict[str, Any]) -> str:
    body = [f"{k} = {_toml_value(v)}" for k, v in values.items() if v is not None]
    return "\n".join([f"[{name}]", *body]) + "\n"


_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def patch_config_toml(text: str, identity: dict[str, Any]) -> str:
    """Заменить таблицы ``[vpn]`` и ``[vpn.reality]`` восстановленными."""
    cfg = identity.get("config") or {}
    vpn = dict(cfg.get("vpn") or {})
    old_vpn = (tomllib.loads(text).get("vpn") or {}) if text.strip() else {}
    for key in LOCAL_VPN_KEYS:
        if key in old_vpn:
            vpn[key] = old_vpn[key]
        else:
            vpn.pop(key, None)
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            skipping = m.group(1) in ("vpn", "vpn.reality")
        if not skipping:
            kept.append(line)
    out = "\n".join(kept).rstrip("\n")
    parts = [render_table("vpn", vpn)]
    if cfg.get("vpn_reality"):
        parts.append(render_table("vpn.reality", cfg["vpn_reality"]))
    return (out + "\n\n" if out else "") + "\n".join(parts)


# --- БД ----------------------------------------------------------------------------------


async def count_peers(conn: Any) -> int:
    cur = await conn.execute("SELECT name FROM sqlite_master WHERE name = 'vpn_peers'")
    if await cur.fetchone() is None:
        return 0
    cur = await conn.execute("SELECT COUNT(*) FROM vpn_peers")
    return int((await cur.fetchone())[0])


async def import_snapshot(conn: Any, snapshot: dict[str, Any], *, wipe: bool) -> dict[str, int]:
    """Вставить строки снапшота (по именам колонок). ``wipe`` — сначала очистить таблицы.

    Вызывать внутри транзакции: при ошибке откатится всё. Неизвестные колонки
    (снапшот новее схемы) пропускаются, недостающие берут DEFAULT.
    """
    done: dict[str, int] = {}
    tables = snapshot.get("tables", {})
    for table in SNAPSHOT_TABLES:
        if table in SKIP_TABLES or table not in tables:
            continue
        cur = await conn.execute(f"PRAGMA table_info({table})")
        known = {r["name"] for r in await cur.fetchall()}
        if not known:
            continue
        columns = tables[table]["columns"]
        idx = [i for i, c in enumerate(columns) if c in known]
        names = [columns[i] for i in idx]
        if wipe:
            await conn.execute(f"DELETE FROM {table}")
        sql = (
            f"INSERT OR REPLACE INTO {table} ({', '.join(names)}) "
            f"VALUES ({', '.join('?' for _ in names)})"
        )
        for row in tables[table]["rows"]:
            await conn.execute(sql, [row[i] for i in idx])
        done[table] = len(tables[table]["rows"])
    return done


# --- оркестрация ------------------------------------------------------------------------


def check_bundle(bundle: dict[str, Any], settings: Settings) -> None:
    if bundle.get("node") != settings.node.id:
        raise ApplyError(
            f"бандл для ноды {bundle.get('node')!r}, а здесь [node].id = {settings.node.id!r}"
        )
    if not bundle.get("identity", {}).get("format"):
        raise ApplyError("в бандле нет identity")


async def apply_bundle(
    bundle: dict[str, Any],
    settings: Settings,
    *,
    config_path: Path,
    io: PrivilegedIO,
    log: Callable[[str], None] = print,
    wipe_db: bool = False,
    awg_conf: Path | None = None,
    reality_env: Path = REALITY_ENV,
) -> None:
    """Применить бандл на этой ноде. Предусловия проверяются ДО первой записи."""
    check_bundle(bundle, settings)
    ident = bundle["identity"]
    snapshot = bundle.get("snapshot")
    config_path = Path(config_path)
    if not config_path.exists():
        raise ApplyError(f"нет конфига ноды {config_path}")
    xray_path = Path(settings.backup.xray_config).expanduser()

    db = Database(settings.vpn.db_path)
    await db.open()
    try:
        await apply_migrations(db)
        have = await count_peers(db.conn)
        if snapshot is not None and have and not wipe_db:
            raise ApplyError(
                f"в {settings.vpn.db_path} уже {have} пиров — не затираю. Если это "
                "пересобранная нода с тестовыми данными, повторите с --wipe-db"
            )

        # 1. awg0.conf
        awg_reload = False
        if ident.get("awg"):
            path = awg_conf or AWG_DIR / f"{ident['awg']['interface']}.conf"
            old = io.read_root(path)
            io.write_root(path, patch_awg_conf(old or "", ident["awg"]))
            log(f"awg: ключ и обфускация записаны в {path}" + ("" if old else " (файл создан)"))
            awg_reload = old is not None
        # 2. Reality
        xray_reload = False
        if ident.get("reality"):
            io.write_root(reality_env, reality_env_text(ident))
            log(f"reality: ключи записаны в {reality_env}")
            if xray_path.exists():
                doc = json.loads(xray_path.read_text(encoding="utf-8"))
                if patch_xray_config(doc, ident["reality"]):
                    shutil.copy2(xray_path, xray_path.with_name(xray_path.name + ".pre-restore"))
                    tmp = xray_path.with_name(xray_path.name + ".tmp")
                    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
                    tmp.chmod(0o600)
                    tmp.replace(xray_path)
                    xray_reload = True
                    log(f"reality: privateKey/shortIds подставлены в {xray_path}")
            else:
                log(f"reality: {xray_path} ещё нет — setup-reality-server.sh возьмёт ключи из env")
        # 3. config.toml
        text = config_path.read_text(encoding="utf-8")
        new_text = patch_config_toml(text, ident)
        if new_text != text:
            shutil.copy2(config_path, config_path.with_name(config_path.name + ".pre-restore"))
            config_path.write_text(new_text, encoding="utf-8")
            log(f"config: [vpn]/[vpn.reality] восстановлены в {config_path}")
        # 4. БД
        if snapshot is not None:
            async with db.transaction() as conn:
                done = await import_snapshot(conn, snapshot, wipe=wipe_db)
            log("БД: " + ", ".join(f"{t}={n}" for t, n in done.items()))
    finally:
        await db.close()

    # 5. рестарт уже запущенного
    if awg_reload:
        unit = f"awg-quick@{ident['awg']['interface']}"
        if io.unit_active(unit):
            io.restart(unit)
            log(f"{unit} перезапущен")
        else:
            log(f"{unit} не запущен — поднимите: sudo systemctl enable --now {unit}")
    if xray_reload and io.unit_active(XRAY_UNIT, user=True):
        io.restart(XRAY_UNIT, user=True)
        log(f"{XRAY_UNIT} перезапущен")
    # 6. публикация снова разрешена
    marker = allow_publish(settings, f"restore из бандла {bundle.get('identity_label')}")
    log(f"маркер публикации бэкапа: {marker}")
