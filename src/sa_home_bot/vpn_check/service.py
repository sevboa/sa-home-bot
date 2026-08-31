"""VpnCheckService — ServiceHandler службы vpn_check: пробные запросы через
локальный VPN-клиентский туннель. Минимальная служба без БД и планировщика
(по образцу net/service.py) — реактивная, не таймерная.

Команда ``ACTION_CHECK`` приходит через fan-out от node-сервиса
(``node/service.py::ACTION_TRIGGER_PEERS``, инициируется ``vpn/service.py``
на jeeves раз в ``[vpn].check_interval_s`` или по ``check_now``). Сама
проверка идёт в фоне (не блокирует ответ на команду — на несколько целей
с таймаутами это может занять секунды), а результат служба сама пушит
обратно в vpn отдельным вызовом: ``node_link.command("report_check", ...,
dst=Address(node=vpn_protocol.NODE_ID, service=vpn_protocol.SERVICE_NAME))``
— vpn/service.py не ждёт синхронно ответа на исходный fan-out, только
копит то, что приходит.

Сам туннель — вне этого процесса: отдельный network namespace
(``settings.vpn_check.netns``), поднятый node/fixups.py::
make_vpn_probe_tunnel_fixup (``nodectl fix``, включая veth-пару + NAT на
хосте — без них у netns нет ни одного физического интерфейса и WireGuard-
хендшейку решительно некуда уйти, живая находка 2026-08-17). Эта служба
netns не создаёт и не поднимает, только пользуется им, вызывая curl внутри
него — так основная маршрутизация ноды не трогается, независимо от того,
какие IP отдаёт DNS для проверяемых целей, и одна и та же схема работает
единообразно на любой ноде роя (включая ноду, где крутится сам VPN-сервер).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import shutil
import socket
import time
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.node import assignments
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ActionParam,
    ActionSpec,
    Address,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.utils.requirements import looks_like_permission_error
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn_check.protocol import ACTION_CHECK, SERVICE_NAME

log = logging.getLogger(__name__)

# Куда бьём `ip route get`, чтобы убедиться, что дефолтный путь из netns
# пробника идёт через туннель, а не мимо (через veth на хост). Литерал, не
# из целей проверки — цели могут резолвиться в разные IP.
_ROUTE_SENTINEL = "1.1.1.1"


def _looks_like_needs_password(err: str) -> bool:
    low = err.strip().lower()
    # ``sudo -n`` без права печатает локализованное сообщение («a password is
    # required» / «требуется указать пароль» / …) — но всегда с префиксом
    # ``sudo:``. На него и опираемся, чтобы не зависеть от локали ноды.
    return looks_like_permission_error(err) or low.startswith("sudo:")


async def _run(*cmd: str, timeout: float) -> tuple[int, str, str]:
    """(код, stdout, stderr). Сетевые/OS-сбои сведены к коду 1 — вызывающему
    важно только «получилось / нет»."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (OSError, TimeoutError) as exc:
        return 1, "", str(exc)
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


class VpnCheckService:
    def __init__(self, settings: Settings, node_link: ServiceLink) -> None:
        self._cfg = settings.vpn_check
        self._node_link = node_link
        self._node = socket.gethostname()
        # На самой VPN-ноде (есть назначение "vpn") внешний IP из туннеля
        # неизбежно совпадает с IP хоста — эндпоинт пробника это же железо.
        # Сверку exit-IP там не делаем, опираемся только на маршрут.
        self._is_vpn_exit = assignments.has_service(settings.node.assignments, "vpn")

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=(ACTION_CHECK,),
            actions=(
                ActionSpec(
                    id=ACTION_CHECK,
                    title="📡 Проверить доступность через VPN",
                    params=(ActionParam(name="targets", title="Цели (список URL)"),),
                ),
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        return {"node": self._node, "service": SERVICE_NAME, "netns": self._cfg.netns}

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action != ACTION_CHECK:
            # Сервер валидирует action по describe — сюда неизвестное не доходит.
            raise ValueError(f"необъявленное действие: {action}")
        targets = args.get("targets")
        if not isinstance(targets, list) or not targets:
            raise ProtoError(ERR_BAD_REQUEST, "targets должен быть непустым списком URL")
        targets = [str(t) for t in targets]
        asyncio.create_task(self._run_and_report(targets), name="vpn-check-run")
        return {"accepted": True, "targets": targets}

    async def _run_and_report(self, targets: list[str]) -> None:
        # Сначала убеждаемся, что пробник ВООБЩЕ ходит через туннель —
        # иначе curl к целям может успешно отвечать мимо VPN, и проверка
        # тихо зеленеет (инцидент 2026-08-31). Провал гейта → все цели
        # помечаем одной и той же внятной ошибкой, а не ложным ok.
        gate = await self._egress_gate()
        results: dict[str, dict[str, Any]] = {}
        for target in targets:
            if gate is not None:
                results[target] = {"ok": False, "ms": None, "error": gate}
            else:
                results[target] = await self._check_one(target)
        try:
            await self._node_link.command(
                "report_check",
                {"node": self._node, "results": results},
                dst=Address(node=vpn_protocol.NODE_ID, service=vpn_protocol.SERVICE_NAME),
                timeout=10.0,
            )
        except (ServiceUnavailableError, ProtoError, TimeoutError) as exc:
            log.warning("vpn_check: не удалось отправить результат в vpn: %s", exc)

    async def _check_one(self, target: str) -> dict[str, Any]:
        timeout_s = self._cfg.check_timeout_s
        # Заход в чужой netns требует root — узкий sudoers-снипет ставит
        # nodectl fix (node/fixups.py::make_vpn_probe_sudoers_fixup), тот же
        # приём («резолвим путь при каждом вызове, не кэшируем, чтобы fix,
        # применённый после старта службы, подхватился без рестарта»), что
        # уже использует vpn/awg.py::RealAwgBackend._sudo_awg. Резолвим
        # только `ip` (прямая цель sudo) — "curl" внутри netns exec остаётся
        # литералом, ровно как в самом sudoers-правиле.
        ip_path = shutil.which("ip") or "ip"
        cmd = [
            "sudo",
            "-n",
            ip_path,
            "netns",
            "exec",
            self._cfg.netns,
            "curl",
            "-s",
            "-m",
            str(timeout_s),
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            target,
        ]
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 3.0)
        except (OSError, TimeoutError) as exc:
            return {"ok": False, "ms": None, "error": str(exc)}
        latency_ms = int((time.monotonic() - started) * 1000)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip() or f"curl exit {proc.returncode}"
            if looks_like_permission_error(err) or "a password is required" in err.lower():
                err = "нет прав на netns пробника — выполните: nodectl fix"
            return {"ok": False, "ms": latency_ms, "error": err}
        code = stdout.decode(errors="replace").strip()
        ok = code.startswith(("2", "3"))
        return {"ok": ok, "ms": latency_ms, "error": None if ok else f"http {code or '?'}"}

    async def _egress_gate(self) -> str | None:
        """None — пробник реально гонит трафик через VPN-туннель. Иначе —
        строка-ошибка (ей помечаются все цели). Две независимые проверки:
        1) дефолтный маршрут из netns идёт через `iface`; 2) внешний IP из
        netns не совпадает с IP хоста (кроме самой VPN-ноды)."""
        route_err = await self._check_route()
        if route_err is not None:
            return route_err
        return await self._check_exit_ip()

    async def _check_route(self) -> str | None:
        ip_path = shutil.which("ip") or "ip"
        code, out, err = await _run(
            "sudo", "-n", ip_path, "netns", "exec", self._cfg.netns,
            ip_path, "route", "get", _ROUTE_SENTINEL,
            timeout=self._cfg.check_timeout_s + 3.0,
        )
        if code != 0:
            if _looks_like_needs_password(err):
                return "нет прав на проверку маршрута netns — выполните: nodectl fix"
            return f"netns {self._cfg.netns}: `ip route get` не отработал: {err.strip() or code}"
        if f"dev {self._cfg.iface}" not in out:
            first = next((ln.strip() for ln in out.splitlines() if ln.strip()), "(пусто)")
            return (
                f"пробник не в туннеле: маршрут до {_ROUTE_SENTINEL} — «{first}», "
                f"ожидался dev {self._cfg.iface}; выполните: nodectl fix"
            )
        return None

    async def _check_exit_ip(self) -> str | None:
        url = self._cfg.ip_echo_url
        if not url or self._is_vpn_exit:
            return None
        netns_ip = await self._exit_ip(via_netns=True)
        if netns_ip is None:
            # Не смогли узнать — не тема этой проверки, реальные цели
            # покажут настоящий сбой.
            return None
        host_ip = await self._exit_ip(via_netns=False)
        if host_ip is not None and netns_ip == host_ip:
            return (
                f"внешний IP из туннеля ({netns_ip}) совпал с IP хоста — "
                "трафик идёт мимо VPN; выполните: nodectl fix"
            )
        return None

    async def _exit_ip(self, *, via_netns: bool) -> str | None:
        timeout_s = self._cfg.check_timeout_s
        curl = ["curl", "-s", "-m", str(timeout_s), self._cfg.ip_echo_url]
        if via_netns:
            ip_path = shutil.which("ip") or "ip"
            cmd = ["sudo", "-n", ip_path, "netns", "exec", self._cfg.netns, *curl]
        else:
            cmd = curl
        code, out, _ = await _run(*cmd, timeout=timeout_s + 3.0)
        if code != 0:
            return None
        try:
            return str(ipaddress.ip_address(out.strip()))
        except ValueError:
            return None
