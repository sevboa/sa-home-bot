"""Веб-сервер подписки Hiddify внутри службы vpn (подэтап 57.10).

Адреса: ``/s/<токен>`` — хаб устройства (57.11: «VPN включён?», Hiddify,
страны и остаток, AmneziaWG), ``POST /s/<токен>/awg`` — выдача AmneziaWG (только
форма со страницы: Origin/Referer + одноразовый nonce + ограничение частоты),
``/sub/<токен>`` — сама подписка (``?format=vless|plain|singbox``). Всё прочее —
голый 404, как и неизвестный/отозванный токен: снаружи не отличить «нет такого»
от «отозвано». Заголовок ``Server`` убран. Порт отдельный (не 443/8443 — там
mtg и xray). IP посетителей не пишем никуда.

TLS: если на диске есть сертификат и ключ — https, иначе http (при
``sub_allow_http``). Сертификат Let's Encrypt на IP короткоживущий (~6 суток),
продлевает его внешний таймер (``deploy/sub-cert.sh``); здесь раз в
``RELOAD_EVERY_S`` проверяется mtime файлов и сертификат перечитывается на
том же SSLContext без остановки сервера, а появление сертификата у
работавшего по http сервера переключает его на https.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import ssl
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from sa_home_bot.config import VpnConfig
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn import subscription as sub

log = logging.getLogger(__name__)

RELOAD_EVERY_S = 600.0
DEFAULT_TLS_DIR = Path("~/.config/sa-home-bot/sub-tls")

# resolve(token) -> Subscription | None
Resolver = Callable[[str], Awaitable[sub.Subscription | None]]
# issue_awg(chat_id, device_label, node, replace=...) -> ответ issue/reissue службы
AwgIssuer = Callable[..., Awaitable[dict]]

NONCE_TTL_S = 1800.0
NONCE_MAX = 4000
AWG_PER_COUNTRY_S = 60.0  # не чаще раза в минуту на устройство и страну
AWG_PER_TOKEN = (6, 3600.0)  # и не больше 6 в час на устройство
AWG_GLOBAL = (30, 3600.0)  # и 30 в час на всю ноду

_COMMON_HEADERS = {
    "cache-control": "no-store",
    "x-robots-tag": "noindex, nofollow",
    "referrer-policy": "same-origin",
    "x-content-type-options": "nosniff",
    "content-security-policy": (
        "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; "
        "frame-ancestors 'none'"
    ),
}


class _Response(web.Response):
    """Ответ без заголовка ``Server`` (aiohttp добавляет его при записи заголовков)."""

    async def _write_headers(self) -> None:
        self._headers.pop("Server", None)
        await super()._write_headers()


def tls_paths(cfg: VpnConfig) -> tuple[Path, Path]:
    cert = Path(cfg.sub_tls_cert).expanduser() if cfg.sub_tls_cert else None
    key = Path(cfg.sub_tls_key).expanduser() if cfg.sub_tls_key else None
    base = DEFAULT_TLS_DIR.expanduser()
    return cert or base / "fullchain.pem", key or base / "privkey.pem"


def public_host(cfg: VpnConfig) -> str:
    if cfg.sub_public_host:
        return cfg.sub_public_host
    if cfg.reality is not None and cfg.reality.endpoint_host:
        return cfg.reality.endpoint_host
    return cfg.endpoint_host


def _not_found() -> web.Response:
    return _Response(status=404, text="Not found\n")


@web.middleware
async def _hardening(request: web.Request, handler) -> web.StreamResponse:
    """Любая ошибка/чужой путь/чужой метод — тот же голый 404; общие заголовки."""
    try:
        resp = await handler(request)
    except web.HTTPException:
        resp = _not_found()
    except Exception:
        log.exception("vpn: страница подписки упала")
        resp = _not_found()
    for key, value in _COMMON_HEADERS.items():
        resp.headers.setdefault(key, value)
    return resp


class SubscriptionWeb:
    def __init__(
        self, cfg: VpnConfig, resolve: Resolver, issue_awg: AwgIssuer | None = None
    ) -> None:
        self._cfg = cfg
        self._resolve = resolve
        self._issue_awg = issue_awg
        # nonce -> (истекает, токен, нода): форма AmneziaWG одноразовая.
        self._nonces: dict[str, tuple[float, str, str]] = {}
        self._awg_last: dict[tuple[str, str], float] = {}
        self._awg_by_token: dict[str, list[float]] = {}
        self._awg_all: list[float] = []
        self._runner: web.AppRunner | None = None
        self._ctx: ssl.SSLContext | None = None
        self._mtimes: tuple[float, float] | None = None
        self._reload_task: asyncio.Task | None = None
        self.tls = False
        self.error: str | None = None
        # Не больше N одновременных разборов токена: каждый может ходить к нодам.
        self._sem = asyncio.Semaphore(8)

    @property
    def listening(self) -> bool:
        return self._runner is not None

    # --- адреса ---

    @property
    def base_url(self) -> str:
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{public_host(self._cfg)}:{self._cfg.sub_port}"

    def page_url(self, token: str) -> str:
        return f"{self.base_url}/s/{token}"

    def sub_url(self, token: str) -> str:
        return f"{self.base_url}/sub/{token}"

    # --- запуск ---

    def _tls_ready(self) -> bool:
        cert, key = tls_paths(self._cfg)
        return cert.is_file() and key.is_file()

    def _build_ctx(self) -> ssl.SSLContext | None:
        cert, key = tls_paths(self._cfg)
        if not (cert.is_file() and key.is_file()):
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(cert), str(key))
        self._mtimes = (cert.stat().st_mtime, key.stat().st_mtime)
        return ctx

    def _app(self) -> web.Application:
        app = web.Application(middlewares=[_hardening], client_max_size=4096)
        app.router.add_get("/s/{token}", self._page)
        app.router.add_post("/s/{token}/awg", self._awg_post)
        app.router.add_get("/sub/{token}", self._sub)
        return app

    async def start(self) -> bool:
        """Поднять сервер. ``False`` — выключен или не вышло (служба живёт дальше)."""
        if self._cfg.sub_port <= 0:
            return False
        await self._listen()
        if self._runner is not None:
            self._reload_task = asyncio.create_task(self._reload_loop(), name="vpn-sub-tls-reload")
        return self._runner is not None

    async def _listen(self) -> None:
        try:
            ctx = self._build_ctx()
        except (ssl.SSLError, OSError) as exc:
            log.warning("vpn: сертификат подписки не читается (%s)", exc)
            ctx = None
        if ctx is None and not self._cfg.sub_allow_http:
            self.error = "нет сертификата, а http запрещён ([vpn].sub_allow_http)"
            log.warning("vpn: страница подписки выключена: %s", self.error)
            return
        runner = web.AppRunner(self._app(), access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self._cfg.sub_bind, self._cfg.sub_port, ssl_context=ctx)
            await site.start()
        except OSError as exc:
            await runner.cleanup()
            self.error = str(exc)
            log.warning("vpn: порт подписки %s не открылся: %s", self._cfg.sub_port, exc)
            return
        self._runner, self._ctx, self.tls, self.error = runner, ctx, ctx is not None, None
        log.info(
            "vpn: подписка Hiddify слушает %s (%s)", self.base_url, "https" if self.tls else "http"
        )

    async def stop(self) -> None:
        if self._reload_task is not None:
            self._reload_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reload_task
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def check_tls(self) -> None:
        """Одна итерация слежения за сертификатом (вынесена для тестов)."""
        ready = self._tls_ready()
        if ready and not self.tls:
            # Был http, сертификат появился: пересоздаём сайт уже с TLS.
            log.info("vpn: появился сертификат подписки — переключаюсь на https")
            old = self._runner
            self._runner = None
            if old is not None:
                await old.cleanup()
            await self._listen()
            return
        if ready and self.tls and self._ctx is not None:
            cert, key = tls_paths(self._cfg)
            stamp = (cert.stat().st_mtime, key.stat().st_mtime)
            if stamp != self._mtimes:
                try:
                    self._ctx.load_cert_chain(str(cert), str(key))
                except (ssl.SSLError, OSError) as exc:
                    log.warning("vpn: новый сертификат подписки не принят: %s", exc)
                    return
                self._mtimes = stamp
                log.info("vpn: сертификат подписки перечитан")

    async def _reload_loop(self) -> None:
        while True:
            await asyncio.sleep(RELOAD_EVERY_S)
            try:
                await self.check_tls()
            except Exception:
                log.exception("vpn: слежение за сертификатом подписки упало")

    # --- обработчики ---

    async def _lookup(self, request: web.Request) -> sub.Subscription | None:
        token = request.match_info["token"]
        if not sub.valid_token_shape(token):
            return None
        async with self._sem:
            try:
                return await self._resolve(token)
            except Exception:
                log.exception("vpn: сборка подписки упала")
                return None

    # --- nonce и частота ---

    def _new_nonce(self, token: str, node: str) -> str:
        now = time.monotonic()
        if len(self._nonces) >= NONCE_MAX:
            self._nonces = {k: v for k, v in self._nonces.items() if v[0] > now}
            if len(self._nonces) >= NONCE_MAX:
                self._nonces.clear()
        nonce = secrets.token_urlsafe(18)
        self._nonces[nonce] = (now + NONCE_TTL_S, token, node)
        return nonce

    def _take_nonce(self, nonce: str, token: str, node: str) -> bool:
        entry = self._nonces.pop(nonce, None)
        return entry is not None and entry[0] > time.monotonic() and entry[1:] == (token, node)

    def _rate_ok(self, token: str, node: str) -> bool:
        """Проверка и учёт попытки выдачи AmneziaWG (в памяти, без адресов)."""
        now = time.monotonic()
        last = self._awg_last.get((token, node))
        mine = [t for t in self._awg_by_token.get(token, []) if now - t < AWG_PER_TOKEN[1]]
        self._awg_all = [t for t in self._awg_all if now - t < AWG_GLOBAL[1]]
        if (
            (last is not None and now - last < AWG_PER_COUNTRY_S)
            or len(mine) >= AWG_PER_TOKEN[0]
            or len(self._awg_all) >= AWG_GLOBAL[0]
        ):
            return False
        self._awg_last[(token, node)] = now
        self._awg_by_token[token] = [*mine, now]
        self._awg_all.append(now)
        if len(self._awg_last) > 4000:
            self._awg_last.clear()
            self._awg_by_token.clear()
        return True

    def _same_origin(self, request: web.Request) -> bool:
        """POST только со своей страницы: Origin (или, если его нет, Referer) —
        тот же хост, что в Host. Без обоих — отказ."""
        source = request.headers.get("Origin") or request.headers.get("Referer") or ""
        if not source or source == "null":
            return False
        return urlsplit(source).netloc == request.host

    # --- обработчики ---

    @property
    def _links(self) -> sub.PageLinks:
        c = self._cfg
        return sub.PageLinks(
            hiddify_ios=c.hiddify_ios_app_store_url,
            hiddify_android=c.hiddify_google_play_url,
            hiddify_site=c.hiddify_site_url,
            amnezia_ios=c.amneziavpn_ios_app_store_url,
            amnezia_android=c.amneziavpn_google_play_url,
            amnezia_site=c.official_download_url,
        )

    @staticmethod
    def _html(text: str, status: int = 200) -> web.Response:
        return _Response(text=text, status=status, content_type="text/html", charset="utf-8")

    async def _page(self, request: web.Request) -> web.Response:
        found = await self._lookup(request)
        if found is None or not found.entries:
            return _not_found()
        token = request.match_info["token"]
        status = sub.detect_status(request.remote or "", found.nodes, self._cfg.subnet)
        forms: dict[str, str] = {}
        if self._issue_awg is not None:
            have = {entry.node for entry in found.entries}
            forms = {
                n.node: self._new_nonce(token, n.node)
                for n in found.nodes
                if n.awg and n.node in have
            }
        body = sub.render_page(
            found,
            sub_url=self.sub_url(token),
            qr_data_uri=sub.qr_data_uri(self.sub_url(token)),
            links=self._links,
            status=status,
            awg_forms=forms,
            path=f"/s/{token}",
        )
        return self._html(body)

    async def _awg_post(self, request: web.Request) -> web.Response:
        """Выдача AmneziaWG: Origin/Referer, одноразовый nonce, частота, для
        существующего ключа — явное подтверждение замены. Результат — один раз."""
        if self._issue_awg is None or not self._same_origin(request):
            return _not_found()
        found = await self._lookup(request)
        if found is None or not found.entries or not found.chat_id:
            return _not_found()
        token = request.match_info["token"]
        path = f"/s/{token}"
        form = await request.post()
        nonce, node_id = str(form.get("nonce") or ""), str(form.get("node") or "")
        confirm = str(form.get("confirm") or "") == "1"
        node = next((n for n in found.nodes if n.node == node_id), None)
        have = {entry.node for entry in found.entries}
        if node is None or not node.awg or node.node not in have:
            return _not_found()
        if not self._take_nonce(nonce, token, node_id):
            text = sub.render_notice(
                "Страница устарела", "Обновите страницу и нажмите кнопку ещё раз.", path=path
            )
            return self._html(text, 400)
        if node.awg_key and not confirm:
            return self._html(
                sub.render_awg_confirm(found, node, self._new_nonce(token, node_id), path)
            )
        if not self._rate_ok(token, node_id):
            text = sub.render_notice(
                "Слишком часто", "Подождите минуту и попробуйте ещё раз.", path=path
            )
            return self._html(text, 429)
        try:
            result = await self._issue_awg(
                found.chat_id, found.device_label, node_id, replace=node.awg_key
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("vpn: выдача AmneziaWG со страницы не удалась: %s", exc)
            text = sub.render_notice(
                "Не получилось",
                "Не получилось выпустить настройки — попробуйте чуть позже.",
                path=path,
            )
            return self._html(text, 502)
        conf = str(result.get("config_text") or "")
        if not conf:
            return _not_found()
        filename = vpn_protocol.secret_filename(
            vpn_protocol.TRANSPORT_AWG, found.device_label, str(result.get("location") or node.name)
        )
        body = sub.render_awg_result(
            found,
            node,
            filename=filename,
            conf_text=conf,
            qr_data_uri=sub.qr_data_uri(conf),
            links=self._links,
            path=path,
        )
        return self._html(body)

    async def _sub(self, request: web.Request) -> web.Response:
        found = await self._lookup(request)
        if found is None or not found.entries:
            return _not_found()
        fmt = request.query.get("format", sub.FORMAT_VLESS)
        if fmt not in sub.FORMATS:
            fmt = sub.FORMAT_VLESS
        token = request.match_info["token"]
        text, ctype = sub.render_body(found, fmt)
        headers = sub.render_headers(
            found,
            page_url=self.page_url(token),
            update_interval_h=self._cfg.sub_update_interval_h,
            support_url=self._cfg.sub_support_url,
            fmt=fmt,
        )
        return _Response(body=text.encode(), headers={**headers, "content-type": ctype})
