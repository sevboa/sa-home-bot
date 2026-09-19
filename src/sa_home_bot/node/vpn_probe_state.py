"""Что из матрицы (сервер, транспорт) реально настроено на ЭТОЙ ноде — файл
пишет ``node/fixups.py`` (root, `nodectl fix`), читает ``vpn_check/service.py``
(обычный пользователь, без sudo).

До 39.0.7(d) это был единственный ``[vpn_check] probe_server``/
`probe_transport` в config.toml, правившийся вручную. Список целей теперь
автообнаруживается (``bot/vpn_nodes.py::probe_targets``) и может меняться
без участия человека — состояние, а не конфиг, поэтому не TOML и не
переиспользует ``VpnCheckConfig``.

Файл НЕ секрет (только имена netns/veth/iface и номер порта — ни ключей, ни
UUID) — 0644, читается напрямую, без ``sudo -n cat`` (в отличие от
``/etc/amnezia/amneziawg/*.conf``, где лежит приватный ключ)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

STATE_PATH = Path("/etc/sa-home-bot/vpn-probe-state.json")
# xray-клиентские конфиги пробников reality (39.0.7(e)) — несут UUID гостя
# (тут: пробника) и параметры сервера, поэтому 0600, не 0644, как остальное
# в этом модуле. Каталог, не файл: один JSON на слот, имя — по netns
# (уникален как и сам слот).
REALITY_CONF_DIR = Path("/etc/sa-home-bot/vpn-probe")


class ProbeSlot(BaseModel):
    """Один заведённый на этой ноде netns-пробник к паре (сервер,
    транспорт). ``iface``/``socks_port`` — ровно один из двух заполнен,
    в зависимости от ``transport`` (awg использует интерфейс, reality —
    локальный SOCKS-порт xray-клиента)."""

    server: str
    transport: str
    netns: str
    veth_host: str
    veth_ns: str
    veth_host_addr: str
    veth_ns_addr: str
    subnet: str
    iface: str | None = None
    socks_port: int | None = None


class ProbeState(BaseModel):
    slots: list[ProbeSlot] = Field(default_factory=list)


def reality_conf_path(slot: ProbeSlot) -> Path:
    return REALITY_CONF_DIR / f"{slot.netns}.json"


def render(slots: list[ProbeSlot]) -> str:
    """Сериализовать для записи (`node/fixups.py` кладёт результат через
    ``install`` под root, см. ``make_vpn_probe_state_fixup``)."""
    return ProbeState(slots=slots).model_dump_json(indent=2) + "\n"


def parse(text: str) -> list[ProbeSlot]:
    return ProbeState.model_validate_json(text).slots


def load(path: Path = STATE_PATH) -> list[ProbeSlot]:
    """Пусто, если файла нет — служба ничего не проверяет, пока `nodectl
    fix` не отработал ни разу (безопасный дефолт: молчание лучше кривых
    данных, тот же принцип, что был у одиночного ``probe_server=""``)."""
    if not path.exists():
        return []
    return parse(path.read_text())
