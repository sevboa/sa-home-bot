from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import shutil
import socket
import time
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.bot import vpn_nodes
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.node import assignments, vpn_probe_state
from sa_home_bot.node.vpn_probe_state import ProbeSlot
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.utils.process import run
from sa_home_bot.utils.requirements import looks_like_sudo_denied, looks_like_sudo_message
from sa_home_bot.vpn_check.protocol import ACTION_CHECK, SERVICE_NAME

log = logging.getLogger(__name__)

_ROUTE_SENTINEL = "1.1.1.1"

_TUNNEL_READY_ATTEMPTS = 3
_TUNNEL_READY_DELAY_S = 1.0

_REALITY_STARTUP_DELAY_S = 0.5
_REALITY_STOP_GRACE_S = 5.0


async def _run(*cmd: str, timeout: float) -> tuple[int, str, str]:
    try:
        result = await run(cmd, timeout=timeout)
    except (OSError, TimeoutError) as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout_text, result.stderr.decode(errors="replace")


class VpnCheckService:
    def __init__(
        self,
        settings: Settings,
        node_link: ServiceLink,
        *,
        slots: list[ProbeSlot] | None = None,
    ) -> None:
        self._cfg = settings.vpn_check
        self._node_link = node_link
        self._node = socket.gethostname()
        self._slots = vpn_probe_state.load() if slots is None else slots
        self._is_vpn_exit = assignments.has_service(settings.node.assignments, "vpn")
        self._reality_procs: dict[str, asyncio.subprocess.Process] = {}

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
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "servers": sorted({slot.server for slot in self._slots}),
        }

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action != ACTION_CHECK:
            raise ValueError(f"необъявленное действие: {action}")
        targets = args.get("targets")
        if not isinstance(targets, list) or not targets:
            raise ProtoError(ERR_BAD_REQUEST, "targets должен быть непустым списком URL")
        targets = [str(t) for t in targets]
        server = str(args.get("server", "")).strip()
        if not server:
            raise ProtoError(ERR_BAD_REQUEST, "нужен server — кого проверяем")
        if server == self._node:
            return {"accepted": True, "server": server, "skipped": "self-check исключён"}
        slots = [s for s in self._slots if s.server == server]
        if not slots:
            return {
                "accepted": True,
                "server": server,
                "skipped": "нет локально настроенного тоннеля к этому серверу",
            }
        asyncio.create_task(self._run_and_report(server, slots, targets), name="vpn-check-run")
        return {"accepted": True, "server": server, "targets": targets}

    async def _run_and_report(
        self, server: str, slots: list[ProbeSlot], targets: list[str]
    ) -> None:
        results: list[dict[str, Any]] = []
        for slot in slots:
            results.extend(await self._check_slot(slot, targets))
        try:
            reports = await vpn_nodes.fanout(
                self._node_link, "report_check", {"node": self._node, "results": results}
            )
            if not reports:
                log.warning("vpn_check: результат некуда деть — vpn в рое сейчас не держит никто")
        except (ServiceUnavailableError, ProtoError, TimeoutError) as exc:
            log.warning("vpn_check: не удалось отправить результат в vpn: %s", exc)

    async def _check_slot(self, slot: ProbeSlot, targets: list[str]) -> list[dict[str, Any]]:
        up_err = await self._tunnel_up(slot)
        try:
            gate = up_err if up_err is not None else await self._egress_gate(slot)
            results: list[dict[str, Any]] = []
            for target in targets:
                one = (
                    {"ok": False, "ms": None, "error": gate}
                    if gate is not None
                    else (await self._check_one(slot, target))
                )
                results.append(
                    {"server": slot.server, "transport": slot.transport, "target": target, **one}
                )
            failed = [r for r in results if not r["ok"]]
            if failed:
                log.warning(
                    "vpn_check: %s %s — не прошли %d из %d: %s",
                    slot.server,
                    slot.transport,
                    len(failed),
                    len(results),
                    "; ".join(f"{r['target']}: {r.get('error') or '?'}" for r in failed),
                )
            return results
        finally:
            await self._tunnel_down(slot)

    async def _tunnel_up(self, slot: ProbeSlot) -> str | None:
        if slot.transport == "awg":
            return await self._awg_up(slot)
        if slot.transport == "reality":
            return await self._reality_up(slot)
        return f"транспорт {slot.transport} пока не поддержан этим пробником"

    async def _tunnel_down(self, slot: ProbeSlot) -> None:
        if slot.transport == "awg":
            await self._awg_down(slot)
        elif slot.transport == "reality":
            await self._reality_down(slot)

    async def _awg_up(self, slot: ProbeSlot) -> str | None:
        ip_path = shutil.which("ip") or "ip"
        awg_quick_path = shutil.which("awg-quick") or "awg-quick"
        code, _out, err = await _run(
            "sudo",
            "-n",
            ip_path,
            "netns",
            "exec",
            slot.netns,
            awg_quick_path,
            "up",
            str(slot.iface),
            timeout=self._cfg.check_timeout_s + 5.0,
        )
        if code != 0:
            if looks_like_sudo_message(err):
                return "нет прав поднять туннель пробника — выполните: nodectl fix"
            return f"awg-quick up {slot.iface} не отработал: {err.strip() or code}"
        return None

    async def _awg_down(self, slot: ProbeSlot) -> None:
        ip_path = shutil.which("ip") or "ip"
        awg_quick_path = shutil.which("awg-quick") or "awg-quick"
        code, _out, err = await _run(
            "sudo",
            "-n",
            ip_path,
            "netns",
            "exec",
            slot.netns,
            awg_quick_path,
            "down",
            str(slot.iface),
            timeout=self._cfg.check_timeout_s + 5.0,
        )
        if code != 0 and not looks_like_sudo_message(err):
            log.warning(
                "vpn_check: awg-quick down %s (netns %s) не отработал: %s",
                slot.iface,
                slot.netns,
                err.strip() or code,
            )

    async def _reality_up(self, slot: ProbeSlot) -> str | None:
        ip_path = shutil.which("ip") or "ip"
        xray_path = shutil.which("xray") or "xray"
        conf_path = vpn_probe_state.reality_conf_path(slot)
        window = int(self._cfg.check_timeout_s) + 10
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo",
                "-n",
                ip_path,
                "netns",
                "exec",
                slot.netns,
                "timeout",
                str(window),
                xray_path,
                "run",
                "-c",
                str(conf_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return f"не удалось запустить xray-пробник: {exc}"
        self._reality_procs[slot.netns] = proc
        await asyncio.sleep(_REALITY_STARTUP_DELAY_S)
        if proc.returncode is not None:
            stderr = b""
            if proc.stderr is not None:
                with contextlib.suppress(TimeoutError):
                    stderr = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
            del self._reality_procs[slot.netns]
            err = stderr.decode(errors="replace").strip()
            if looks_like_sudo_message(err):
                return "нет прав поднять xray-пробник — выполните: nodectl fix"
            return f"xray-пробник упал сразу после запуска: {err or proc.returncode}"
        return None

    async def _reality_down(self, slot: ProbeSlot) -> None:
        proc = self._reality_procs.pop(slot.netns, None)
        if proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_REALITY_STOP_GRACE_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            log.warning(
                "vpn_check: xray-пробник (netns %s) не остановился по terminate() — kill()",
                slot.netns,
            )

    def _curl_argv(self, slot: ProbeSlot, curl_args: list[str]) -> list[str]:
        ip_path = shutil.which("ip") or "ip"
        curl = ["curl"]
        if slot.transport == "reality":
            curl += ["--socks5-hostname", f"127.0.0.1:{slot.socks_port}"]
        curl += curl_args
        return ["sudo", "-n", ip_path, "netns", "exec", slot.netns, *curl]

    async def _check_one(self, slot: ProbeSlot, target: str) -> dict[str, Any]:
        result = await self._curl_code(slot, target, head=True)
        if result.get("http_code") in {"405", "501"}:
            result = await self._curl_code(slot, target, head=False)
        result.pop("http_code", None)
        return result

    async def _curl_code(self, slot: ProbeSlot, target: str, *, head: bool) -> dict[str, Any]:
        timeout_s = self._cfg.check_timeout_s
        args = ["-s", "-m", str(timeout_s), "-o", "/dev/null", "-w", "%{http_code}"]
        if head:
            args.insert(0, "-I")
        cmd = self._curl_argv(slot, [*args, target])
        started = time.monotonic()
        try:
            result = await run(cmd, timeout=timeout_s + 3.0)
        except (OSError, TimeoutError) as exc:
            return {"ok": False, "ms": None, "error": str(exc)}
        latency_ms = int((time.monotonic() - started) * 1000)
        if result.returncode != 0:
            err = result.stderr_text or f"curl exit {result.returncode}"
            if looks_like_sudo_denied(err):
                err = "нет прав на netns пробника — выполните: nodectl fix"
            return {"ok": False, "ms": latency_ms, "error": err}
        code = result.stdout_text.strip()
        ok = code.startswith(("2", "3"))
        return {
            "ok": ok,
            "ms": latency_ms,
            "error": None if ok else f"http {code or '?'}",
            "http_code": code,
        }

    async def _egress_gate(self, slot: ProbeSlot) -> str | None:
        if slot.transport == "awg":
            route_err = await self._wait_for_route(slot)
            if route_err is not None:
                return route_err
        return await self._check_exit_ip(slot)

    async def _wait_for_route(self, slot: ProbeSlot) -> str | None:
        err: str | None = None
        for attempt in range(_TUNNEL_READY_ATTEMPTS):
            err = await self._check_route(slot)
            if err is None:
                return None
            if attempt < _TUNNEL_READY_ATTEMPTS - 1:
                await asyncio.sleep(_TUNNEL_READY_DELAY_S)
        return err

    async def _check_route(self, slot: ProbeSlot) -> str | None:
        ip_path = shutil.which("ip") or "ip"
        code, out, err = await _run(
            "sudo",
            "-n",
            ip_path,
            "netns",
            "exec",
            slot.netns,
            ip_path,
            "route",
            "get",
            _ROUTE_SENTINEL,
            timeout=self._cfg.check_timeout_s + 3.0,
        )
        if code != 0:
            if looks_like_sudo_message(err):
                return "нет прав на проверку маршрута netns — выполните: nodectl fix"
            return f"netns {slot.netns}: `ip route get` не отработал: {err.strip() or code}"
        if f"dev {slot.iface}" not in out:
            first = next((ln.strip() for ln in out.splitlines() if ln.strip()), "(пусто)")
            return (
                f"пробник не в туннеле: маршрут до {_ROUTE_SENTINEL} — «{first}», "
                f"ожидался dev {slot.iface}; выполните: nodectl fix"
            )
        return None

    async def _check_exit_ip(self, slot: ProbeSlot) -> str | None:
        url = self._cfg.ip_echo_url
        if not url or self._is_vpn_exit:
            return None
        netns_ip = await self._exit_ip(slot, via_netns=True)
        if netns_ip is None:
            return None
        host_ip = await self._exit_ip(slot, via_netns=False)
        if host_ip is not None and netns_ip == host_ip:
            return (
                f"внешний IP из туннеля ({netns_ip}) совпал с IP хоста — "
                "трафик идёт мимо VPN; выполните: nodectl fix"
            )
        return None

    async def _exit_ip(self, slot: ProbeSlot, *, via_netns: bool) -> str | None:
        timeout_s = self._cfg.check_timeout_s
        if via_netns:
            cmd = self._curl_argv(slot, ["-s", "-m", str(timeout_s), self._cfg.ip_echo_url])
        else:
            cmd = ["curl", "-s", "-m", str(timeout_s), self._cfg.ip_echo_url]
        code, out, _ = await _run(*cmd, timeout=timeout_s + 3.0)
        if code != 0:
            return None
        try:
            return str(ipaddress.ip_address(out.strip()))
        except ValueError:
            return None
