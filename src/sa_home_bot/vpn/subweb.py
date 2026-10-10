"""Веб-сервер подписки Hiddify внутри службы vpn (подэтап 57.10).

Два адреса: ``/s/<токен>`` — страница с кнопкой «Открыть в Hiddify»,
``/sub/<токен>`` — сама подписка (``?format=vless|plain|singbox``). Всё прочее —
404, как и неизвестный/отозванный токен: снаружи не отличить «нет такого» от
«отозвано». Порт отдельный (не 443/8443 — там mtg и xray).

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
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web

from sa_home_bot.config import VpnConfig
from sa_home_bot.vpn import subscription as sub

log = logging.getLogger(__name__)

RELOAD_EVERY_S = 600.0
DEFAULT_TLS_DIR = Path("~/.config/sa-home-bot/sub-tls")

# resolve(token) -> Subscription | None
Resolver = Callable[[str], Awaitable[sub.Subscription | None]]


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
    return web.Response(
        status=404,
        text="Not found\n",
        headers={"cache-control": "no-store", "x-robots-tag": "noindex"},
    )


class SubscriptionWeb:
    def __init__(self, cfg: VpnConfig, resolve: Resolver) -> None:
        self._cfg = cfg
        self._resolve = resolve
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
        app = web.Application()
        app.router.add_get("/s/{token}", self._page)
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

    async def _page(self, request: web.Request) -> web.Response:
        found = await self._lookup(request)
        if found is None or not found.entries:
            return _not_found()
        token = request.match_info["token"]
        body = sub.render_page(
            found,
            sub_url=self.sub_url(token),
            qr_data_uri=sub.qr_data_uri(self.sub_url(token)),
            ios_url=self._hiddify_ios,
            android_url=self._hiddify_android,
            site_url=self._hiddify_site,
        )
        return web.Response(
            text=body,
            content_type="text/html",
            charset="utf-8",
            headers={
                "cache-control": "no-store",
                "x-robots-tag": "noindex, nofollow",
                "referrer-policy": "no-referrer",
            },
        )

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
        return web.Response(body=text.encode(), headers={**headers, "content-type": ctype})

    # Ссылки на приложение — из [vpn] (те же, что в мастере бота).
    @property
    def _hiddify_ios(self) -> str:
        return self._cfg.hiddify_ios_app_store_url

    @property
    def _hiddify_android(self) -> str:
        return self._cfg.hiddify_google_play_url

    @property
    def _hiddify_site(self) -> str:
        return self._cfg.hiddify_site_url
