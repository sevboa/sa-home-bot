"""VpnCheckService — ServiceHandler службы vpn_check: пробные запросы через
локальные VPN-клиентские туннели. Минимальная служба без БД и планировщика
(по образцу net/service.py) — реактивная, не таймерная.

Команда ``ACTION_CHECK`` приходит через fan-out от node-сервиса
(``node/service.py::ACTION_TRIGGER_PEERS``, инициируется ``vpn/service.py``
на каждой vpn-ноде раз в ``[vpn].check_interval_s`` или по ``check_now``).
Сама проверка идёт в фоне (не блокирует ответ на команду — на несколько
целей с таймаутами это может занять секунды), а результат служба сама
пушит обратно в vpn отдельным вызовом ``report_check`` — vpn/service.py не
ждёт синхронно ответа на исходный fan-out, только копит то, что приходит.
Фанаут идёт на ВСЕ живые vpn-инстансы (``bot/vpn_nodes.fanout``), не в
один — иначе результат оседает в БД ровно одной ноды.

С 39.0.7(d) туннелей может быть НЕСКОЛЬКО — по одному на каждую пару
(сервер, транспорт), которую эта нода реально проверяет
(``node/vpn_probe_state.py::load()``, список пишет ``node/fixups.py`` при
``nodectl fix`` — автообнаружение всех живых vpn-серверов кроме себя, см.
``bot/vpn_nodes.py::probe_targets``). Диспетчер (``vpn/service.py``) шлёт
запрос по ОДНОМУ имени сервера («проверьте jeeves»), не по паре — если у
этой ноды к серверу настроено больше одного транспорта (awg И reality),
проверяются ВСЕ, и в один ``report_check`` уходит по строке результата на
каждую пару (сервер, транспорт, цель).

Сам netns + veth-пара + NAT на хосте — вне этого процесса, заводит
``node/fixups.py`` (``nodectl fix``), переживает ребут (без этого у netns
нет ни одного физического интерфейса и WireGuard-хендшейку/xray решительно
некуда уйти — живая находка 2026-08-17). А вот САМ туннель (awg-quick /
xray-клиент) — эфемерный: эта служба поднимает его перед пачкой проверок
конкретного слота и гасит сразу после (компромисс ради слабого железа —
решение владельца 2026-09-18, «полная проверка, но не грузить машины
вечно висящими процессами»). Одна и та же схема работает единообразно на
любой ноде роя, включая ноду, где крутится сам VPN-сервер (self-check
исключён отдельно, ниже).
"""

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
from sa_home_bot.utils.requirements import looks_like_permission_error
from sa_home_bot.vpn_check.protocol import ACTION_CHECK, SERVICE_NAME

log = logging.getLogger(__name__)

# Куда бьём `ip route get`, чтобы убедиться, что дефолтный путь из netns
# пробника идёт через туннель, а не мимо (через veth на хост). Литерал, не
# из целей проверки — цели могут резолвиться в разные IP.
_ROUTE_SENTINEL = "1.1.1.1"

# После `awg-quick up` хендшейк не всегда мгновенен (реальная сеть, не
# localhost) — даём маршруту несколько попыток устояться, прежде чем
# считать тоннель мёртвым. Общее время ожидания укладывается в
# check_timeout_s с запасом, не превращая эфемерный подъём в вечное
# ожидание при по-настоящему упавшем сервере.
_TUNNEL_READY_ATTEMPTS = 3
_TUNNEL_READY_DELAY_S = 1.0

# Reality: xray-клиент — долгоживущий процесс (не oneshot вроде awg-quick),
# запускается ФОНОМ на время чек-цикла. Пауза после спавна — дать SOCKS5-
# инбаунду реально начать слушать порт, прежде чем curl по нему стучится
# (первый прогон после деплоя может нуждаться в подстройке значения по
# живым данным — оценка "сколько реально нужно" ещё не проверена вживую).
_REALITY_STARTUP_DELAY_S = 0.5
# Подстраховка на случай, если `sudo`/`timeout` не форвардят SIGTERM до
# самого xray (не все реализации sudo одинаково прозрачны для сигналов) —
# `timeout <окно>` внутри netns убьёт процесс сам, даже если наш terminate()
# не пробьётся. Держим xray живым не дольше самой пачки проверок с запасом.
_REALITY_STOP_GRACE_S = 5.0


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
        # Слоты — что из матрицы (сервер, транспорт) реально настроено на
        # ЭТОЙ ноде (пишет node/fixups.py::make_vpn_probe_state_fixup).
        # ``slots=`` явным аргументом — только для тестов; в проде всегда
        # читается с диска (пусто — nodectl fix ещё не применялся ни разу,
        # служба ничего не проверяет, безопасный дефолт).
        self._slots = vpn_probe_state.load() if slots is None else slots
        # На самой VPN-ноде (есть назначение "vpn") внешний IP из туннеля
        # неизбежно совпадает с IP хоста — эндпоинт пробника это же железо.
        # Сверку exit-IP там не делаем, опираемся только на маршрут/SOCKS.
        self._is_vpn_exit = assignments.has_service(settings.node.assignments, "vpn")
        # xray-клиенты reality, запущенные ФОНОМ на время текущего
        # чек-цикла — по netns, чтобы _tunnel_down нашёл СВОЙ процесс (два
        # слота разных серверов никогда не делят netns, см. node/fixups.py).
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
            # Сервер валидирует action по describe — сюда неизвестное не доходит.
            raise ValueError(f"необъявленное действие: {action}")
        targets = args.get("targets")
        if not isinstance(targets, list) or not targets:
            raise ProtoError(ERR_BAD_REQUEST, "targets должен быть непустым списком URL")
        targets = [str(t) for t in targets]
        server = str(args.get("server", "")).strip()
        if not server:
            raise ProtoError(ERR_BAD_REQUEST, "нужен server — кого проверяем")
        # Self-check исключён (39.0.7, решение владельца 2026-09-18): сервер
        # не проверяет сам себя через собственный туннель — это не даёт
        # сигнала о видимости извне, а видимость извне и есть смысл всей
        # этой службы. Здоровье собственного процесса и так видно через
        # monitor/get_state.
        if server == self._node:
            return {"accepted": True, "server": server, "skipped": "self-check исключён"}
        # Локальные тоннели этой ноды к запрошенному серверу — может быть
        # НОЛЬ (ничего не настроено, скипаем), ОДИН (типичный случай) или
        # НЕСКОЛЬКО (сервер несёт оба транспорта, и оба тут провижинены) —
        # проверяем каждый, репортим одним пакетом.
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
        # Слоты одного сервера идут ПОСЛЕДОВАТЕЛЬНО, не параллельно —
        # каждый эфемерный подъём уже сам по себе всплеск нагрузки, слабое
        # железо не должно тянуть несколько сразу (решение владельца
        # 2026-09-18 про «не грузить машины»).
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
        """Поднять эфемерный туннель под ОДИН слот, прогнать пачку целей,
        погасить туннель — независимо от исхода (``finally``), чтобы
        неудачный/зависший чек не оставлял процесс висеть до следующего
        цикла."""
        up_err = await self._tunnel_up(slot)
        try:
            # Сначала убеждаемся, что пробник ВООБЩЕ ходит через туннель —
            # иначе curl к целям может успешно отвечать мимо VPN, и
            # проверка тихо зеленеет (инцидент 2026-08-31). Провал гейта →
            # все цели помечаем одной и той же внятной ошибкой, а не
            # ложным ok.
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
            return results
        finally:
            await self._tunnel_down(slot)

    async def _tunnel_up(self, slot: ProbeSlot) -> str | None:
        """Поднять сам процесс туннеля (сеть/netns/veth уже подняты
        node/fixups.py заранее, вечно). ``None`` — получилось (гейт решит
        дальше, готов ли реально маршрут/SOCKS); строка — сразу ошибка, и
        гасить нечего (``up`` не прошёл, ``_tunnel_down`` всё равно
        best-effort вызывается вызывающим кодом — на случай частичного
        подъёма)."""
        if slot.transport == "awg":
            return await self._awg_up(slot)
        if slot.transport == "reality":
            return await self._reality_up(slot)
        return f"транспорт {slot.transport} пока не поддержан этим пробником"

    async def _tunnel_down(self, slot: ProbeSlot) -> None:
        """Best-effort — ошибки останова только логируем, вызывается из
        ``finally`` и падать здесь незачем."""
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
            if _looks_like_needs_password(err):
                return "нет прав поднять туннель пробника — выполните: nodectl fix"
            return f"awg-quick up {slot.iface} не отработал: {err.strip() or code}"
        return None

    async def _awg_down(self, slot: ProbeSlot) -> None:
        # Висящий netns без поднятого интерфейса безвреден (сам интерфейс
        # уже мог не подняться вовсе) — ошибки останова только логируем.
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
        if code != 0 and not _looks_like_needs_password(err):
            log.warning(
                "vpn_check: awg-quick down %s (netns %s) не отработал: %s",
                slot.iface,
                slot.netns,
                err.strip() or code,
            )

    async def _reality_up(self, slot: ProbeSlot) -> str | None:
        """xray-клиент — долгоживущий процесс, не oneshot вроде awg-quick:
        запускаем ФОНОМ (не await'им завершение) под ``timeout`` — подстраховка
        от утечки, если ``terminate()`` в ``_reality_down`` не пробьётся через
        sudo (см. комментарий у ``_REALITY_STOP_GRACE_S``). SOCKS-инбаунд
        слушает ТОЛЬКО внутри netns (127.0.0.1 изолирован), curl достаёт его
        оттуда же — см. ``_curl_argv``."""
        ip_path = shutil.which("ip") or "ip"
        xray_path = shutil.which("xray") or "xray"
        conf_path = vpn_probe_state.reality_conf_path(slot)
        # Запас над обычной длительностью чек-цикла — `timeout` не должен
        # срубить xray раньше, чем `_reality_down` сама его остановит.
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
            # Умер мгновенно — обычно кривой конфиг или занятый порт.
            stderr = b""
            if proc.stderr is not None:
                with contextlib.suppress(TimeoutError):
                    stderr = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
            del self._reality_procs[slot.netns]
            err = stderr.decode(errors="replace").strip()
            if _looks_like_needs_password(err):
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
        """``sudo -n ip netns exec <netns> curl ...`` — заход в чужой netns
        требует root, узкий sudoers-снипет ставит nodectl fix
        (node/fixups.py::make_vpn_probe_sudoers_fixup), тот же приём
        («резолвим путь при каждом вызове, не кэшируем, чтобы fix,
        применённый после старта службы, подхватился без рестарта»), что
        уже использует vpn/awg.py::RealAwgBackend._sudo_awg. Резолвим
        только `ip` (прямая цель sudo) — "curl" внутри netns exec остаётся
        литералом, ровно как в самом sudoers-правиле.

        Reality-слоты идут через ``--socks5`` на локальный порт xray-клиента
        (запущенного в ЭТОМ ЖЕ netns) — маршрут не переписан (в отличие от
        awg, где default route внутри netns строит сам awg-quick), см.
        IMPLEMENTATION_PLAN.md 39.0.7 про ``_egress_gate`` для reality."""
        ip_path = shutil.which("ip") or "ip"
        curl = ["curl"]
        if slot.transport == "reality":
            curl += ["--socks5", f"127.0.0.1:{slot.socks_port}"]
        curl += curl_args
        return ["sudo", "-n", ip_path, "netns", "exec", slot.netns, *curl]

    async def _check_one(self, slot: ProbeSlot, target: str) -> dict[str, Any]:
        timeout_s = self._cfg.check_timeout_s
        cmd = self._curl_argv(
            slot,
            ["-s", "-m", str(timeout_s), "-o", "/dev/null", "-w", "%{http_code}", target],
        )
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

    async def _egress_gate(self, slot: ProbeSlot) -> str | None:
        """None — пробник реально гонит трафик через VPN-туннель. Иначе —
        строка-ошибка (ей помечаются все цели).

        awg: две независимые проверки — 1) дефолтный маршрут из netns идёт
        через `iface` (с несколькими попытками — хендшейк после
        ``awg-quick up`` не всегда мгновенен); 2) внешний IP из netns не
        совпадает с IP хоста (кроме самой VPN-ноды).

        reality: маршрут не переписан (трафик идёт через явный
        ``--socks5``, не через default route) — единственная проверка
        такая же, как второй шаг у awg: сверка exit-IP через сам SOCKS."""
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
            if _looks_like_needs_password(err):
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
            # Не смогли узнать — не тема этой проверки, реальные цели
            # покажут настоящий сбой.
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
