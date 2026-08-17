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
(``settings.vpn_check.netns``), поднятый деплой-скриптом отдельно от
sa-home-bot (см. план — этап vpn_check в IMPLEMENTATION_PLAN.md). Эта
служба его не создаёт и не поднимает, только пользуется им, вызывая curl
внутри него — так основная маршрутизация ноды не трогается, независимо от
того, какие IP отдаёт DNS для проверяемых целей.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ActionParam,
    ActionSpec,
    Address,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn_check.protocol import ACTION_CHECK, SERVICE_NAME

log = logging.getLogger(__name__)


class VpnCheckService:
    def __init__(self, settings: Settings, node_link: ServiceLink) -> None:
        self._cfg = settings.vpn_check
        self._node_link = node_link
        self._node = socket.gethostname()

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
        results: dict[str, dict[str, Any]] = {}
        for target in targets:
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
        cmd = [
            "ip",
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
            return {"ok": False, "ms": latency_ms, "error": err}
        code = stdout.decode(errors="replace").strip()
        ok = code.startswith(("2", "3"))
        return {"ok": ok, "ms": latency_ms, "error": None if ok else f"http {code or '?'}"}
