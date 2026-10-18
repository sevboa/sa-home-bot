from __future__ import annotations

import asyncio
import base64
import binascii
import html
import logging
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import qbittorrentapi

from sa_home_bot import __version__
from sa_home_bot.config import Settings
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)

log = logging.getLogger(__name__)

SERVICE_NAME = "torrents"
ACTION_ADD = "add"
ACTION_LIST = "list"
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"
ACTION_SPACE = "space"
ACTION_SEARCH = "search"
ACTION_SEARCH_SMART = "search_smart"
ACTION_DETAILS = "details"
ACTION_SPEED_LIMIT = "speed_limit"

MAX_SPEED_LIMIT_MBPS = 5

SEARCH_LIMIT = 10
SEARCH_SMART_FETCH_LIMIT = 200
SEARCH_WAIT_S = 45.0
SEARCH_POLL_S = 0.5
SEARCH_RUNNING = "Running"
PLUGIN_FETCH_TIMEOUT_S = 60.0
DETAILS_HEAD_CHARS = 700
DETAILS_TECH_CHARS = 900
DETAILS_TIMEOUT_S = 20.0
DETAILS_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"

DEFAULT_NOVA_DIR = "~/.local/share/qBittorrent/nova3"

LIST_LIMIT = 20

MIN_FREE_BYTES = 2 * 1024**3

ALL_SELECTORS = frozenset({"*", "all", "все", "всё", "все закачки", "всё сразу"})


def _is_magnet_or_url(source: str) -> bool:
    return source.startswith(("magnet:", "http://", "https://"))


_QUERY_NOISE_RE = re.compile(
    r"\b(?:"
    r"19\d{2}|20\d{2}"
    r"|[sS]\d{1,2}(?:[eE]\d{1,3})?"
    r"|\d{3,4}[pi]"
    r"|web-?dl(?:rip)?|web-?rip|hd-?rip|bd-?rip|dvd-?rip|blu-?ray|hdtv|remux"
    r"|hevc|avc|x26[45]|h\.?26[45]"
    r"|сериал|сезон|series|season"
    r")\b",
    re.IGNORECASE,
)
_PUNCT_ONLY_RE = re.compile(r"^[\W_]+$")
MAX_QUERY_ATTEMPTS = 5


def _clean_query(query: str) -> str:
    words = [w for w in _QUERY_NOISE_RE.sub(" ", query).split() if not _PUNCT_ONLY_RE.match(w)]
    return " ".join(words)


def _query_ladder(query: str) -> list[str]:
    attempts = [query.strip()]
    cleaned = _clean_query(query)
    words = cleaned.split()
    for candidate in (cleaned, *(" ".join(words[:n]) for n in (4, 3, 2))):
        if candidate and candidate not in attempts:
            attempts.append(candidate)
    return attempts[:MAX_QUERY_ATTEMPTS]


_PAGE_DROP_TAGS_RE = re.compile(r"(?is)<(script|style|head|nav|noscript)\b.*?</\1>")
_PAGE_DROP_BLOCKS_RE = re.compile(
    r'(?is)<(div|table|ul)[^>]*(?:id|class)="[^"]*'
    r'(?:menu|logo|sidebar|footer|header|news_table)[^"]*".*?</\1>'
)
_PAGE_BREAK_RE = re.compile(r"(?i)<br\s*/?>|</(?:p|div|tr|li|h\d)>")
_PAGE_TAG_RE = re.compile(r"(?s)<[^>]+>")
_PAGE_MAGNET_RE = re.compile(r'href="(magnet:[^"]+)"', re.IGNORECASE)
_MEANINGFUL_LINE_CHARS = 25
_PAGE_TECH_RE = re.compile(
    r"(?im)^\s*(?:качество|формат|видео|аудио|перевод|продолжительность|субтитры|размер)\b"
)


def _page_text(page: str) -> str:
    text = _PAGE_DROP_BLOCKS_RE.sub(" ", _PAGE_DROP_TAGS_RE.sub(" ", page))
    text = _PAGE_TAG_RE.sub(" ", _PAGE_BREAK_RE.sub("\n", text))
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    first = next((i for i, line in enumerate(lines) if len(line) >= _MEANINGFUL_LINE_CHARS), 0)
    return "\n".join(lines[first:])


def _page_summary(text: str) -> str:
    head = text[:DETAILS_HEAD_CHARS]
    match = _PAGE_TECH_RE.search(text, len(head))
    if match is None:
        return head
    tech = text[match.start() : match.start() + DETAILS_TECH_CHARS]
    return f"{head}\n…\n{tech}"


_ENGINE_ERROR_MARK = "[Error]"


def _is_engine_error(result: dict[str, Any]) -> bool:
    return _ENGINE_ERROR_MARK in str(result.get("fileName", "")) or str(
        result.get("fileUrl", "")
    ).endswith("/error")


def _engine_error_text(result: dict[str, Any]) -> str:
    name = str(result.get("fileName", ""))
    _, _, tail = name.partition(_ENGINE_ERROR_MARK)
    return (tail.lstrip(": ") or name).strip() or "плагин не сказал, что именно"


def _disk_usage(path: str) -> tuple[int | None, int | None]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None, None
    return usage.free, usage.total


QBIT_ETA_INFINITY_SECONDS = 8640000


def _eta_or_none(raw: Any) -> int | None:
    try:
        eta = int(raw)
    except (TypeError, ValueError):
        return None
    return None if eta < 0 or eta >= QBIT_ETA_INFINITY_SECONDS else eta


class TorrentsService:
    def __init__(self, settings: Settings) -> None:
        self._cfg = settings.torrents
        self._node = socket.gethostname()

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=(
                ACTION_ADD,
                ACTION_LIST,
                ACTION_PAUSE,
                ACTION_RESUME,
                ACTION_SPACE,
                ACTION_SEARCH,
                ACTION_SEARCH_SMART,
                ACTION_DETAILS,
                ACTION_SPEED_LIMIT,
            ),
            actions=(
                ActionSpec(
                    id=ACTION_LIST,
                    title="📋 Что качается",
                ),
                ActionSpec(
                    id=ACTION_SEARCH,
                    title="🔎 Поиск по трекерам",
                    params=(ActionParam(name="query", type="string", title="Что ищем"),),
                ),
                ActionSpec(
                    id=ACTION_SEARCH_SMART,
                    title="🧠 Умный поиск (фраза + слова)",
                    params=(
                        ActionParam(name="phrase", type="string", title="Точная фраза (название)"),
                        ActionParam(
                            name="words",
                            type="string",
                            required=False,
                            title="Доп. слова через пробел (год, качество…)",
                        ),
                    ),
                ),
                ActionSpec(
                    id=ACTION_DETAILS,
                    title="📖 Карточка раздачи",
                    params=(ActionParam(name="page", type="string", title="Ссылка на страницу"),),
                ),
                ActionSpec(
                    id=ACTION_SPACE,
                    title="💾 Сколько места",
                ),
                ActionSpec(
                    id=ACTION_SPEED_LIMIT,
                    title="🐢 Ограничить скорость",
                    params=(
                        ActionParam(
                            name="mbps",
                            type="int",
                            title=(
                                "Лимит, МБ/с (0 — без ограничения, "
                                f"максимум {MAX_SPEED_LIMIT_MBPS})"
                            ),
                        ),
                    ),
                ),
                ActionSpec(
                    id=ACTION_PAUSE,
                    title="⏸ Остановить закачку",
                    params=(ActionParam(name="name", type="string", title="Раздача"),),
                ),
                ActionSpec(
                    id=ACTION_RESUME,
                    title="▶️ Продолжить закачку",
                    params=(ActionParam(name="name", type="string", title="Раздача"),),
                ),
                ActionSpec(
                    id=ACTION_ADD,
                    title="🧲 Добавить торрент",
                    params=(
                        ActionParam(name="source", type="string", title="Magnet или файл"),
                        ActionParam(name="name", type="string", required=False, title="Имя"),
                        ActionParam(
                            name="save_path",
                            type="string",
                            title="Куда сохранить",
                            choices=tuple(self._cfg.save_dirs),
                        ),
                    ),
                ),
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "save_dirs": list(self._cfg.save_dirs),
        }

    def _client(self) -> qbittorrentapi.Client:
        return qbittorrentapi.Client(
            host=self._cfg.qbittorrent_url,
            username=self._cfg.qbittorrent_user,
            password=self._cfg.qbittorrent_password,
        )

    def _nova_dir(self) -> Path:
        raw = self._cfg.search_engine_dir or DEFAULT_NOVA_DIR
        return Path(raw).expanduser()

    @staticmethod
    def _plugin_for(plugins: list[Any], url: str) -> str | None:
        host = urlsplit(url).netloc.lower()
        if not host:
            return None
        for plugin in plugins:
            if not plugin.get("enabled"):
                continue
            site = urlsplit(str(plugin.get("url", ""))).netloc.lower()
            if site and (host == site or host.endswith(f".{site}")):
                return str(plugin.get("name"))
        return None

    def _fetch_via_plugin(self, plugin: str, url: str) -> bytes:
        nova = self._nova_dir() / "nova2dl.py"
        if not nova.exists():
            raise ProtoError(
                ERR_INTERNAL,
                f"поисковый движок qBittorrent не найден: нет {nova} "
                "(проверьте [torrents].search_engine_dir)",
            )
        try:
            proc = subprocess.run(  # noqa: S603
                [sys.executable, str(nova), plugin, url],
                capture_output=True,
                timeout=PLUGIN_FETCH_TIMEOUT_S,
                check=False,
                cwd=str(self._nova_dir()),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProtoError(ERR_INTERNAL, f"плагин {plugin} не отдал файл: {exc}") from exc
        out = proc.stdout.decode(errors="replace").strip()
        path = Path(out.split(" ", 1)[0]) if out else None
        if path is None or not path.is_file():
            log.warning(
                "nova2dl %s: rc=%s out=%r err=%r", plugin, proc.returncode, out, proc.stderr
            )
            raise ProtoError(
                ERR_INTERNAL,
                f"плагин {plugin} не отдал .torrent — вероятно, не настроен логин к трекеру",
            )
        try:
            return path.read_bytes()
        finally:
            path.unlink(missing_ok=True)

    def _run_search_job(
        self, client: Any, pattern: str, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        job = client.search_start(pattern=pattern, plugins="enabled", category="all")
        search_id = job["id"]
        try:
            deadline = time.monotonic() + SEARCH_WAIT_S
            while time.monotonic() < deadline:
                statuses = client.search_status(search_id=search_id)
                if not statuses or statuses[0].get("status") != SEARCH_RUNNING:
                    break
                time.sleep(SEARCH_POLL_S)
            raw = client.search_results(search_id=search_id, limit=limit)
        finally:
            client.search_delete(search_id=search_id)
        results = list(raw.get("results", []))
        return results, int(raw.get("total", len(results)) or 0)

    def _details_sync(self, page: str) -> dict[str, Any]:
        client = self._client()
        try:
            client.auth_log_in()
            plugin = self._plugin_for(list(client.search_plugins()), page)
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()
        if plugin is None:
            raise ProtoError(
                ERR_BAD_REQUEST,
                "открываю только страницы трекеров, для которых установлен "
                "поисковый плагин — этот сайт не из них",
            )

        request = urllib.request.Request(page, headers={"User-Agent": DETAILS_UA})
        try:
            with urllib.request.urlopen(request, timeout=DETAILS_TIMEOUT_S) as resp:  # noqa: S310
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProtoError(ERR_INTERNAL, f"страница не открылась: {exc}") from exc
        body = raw.decode(charset, errors="replace")

        magnet = _PAGE_MAGNET_RE.search(body)
        result: dict[str, Any] = {"page": page, "text": _page_summary(_page_text(body))}
        if magnet is not None:
            result["magnet"] = html.unescape(magnet.group(1))
        return result

    @staticmethod
    def _map_result(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": r.get("fileName", "?"),
            "size_bytes": int(r.get("fileSize", 0) or 0),
            "seeders": max(0, int(r.get("nbSeeders", 0) or 0)),
            "leechers": max(0, int(r.get("nbLeechers", 0) or 0)),
            "source": r.get("fileUrl", ""),
            "page": r.get("descrLink", ""),
        }

    def _search_sync(self, query: str, limit: int) -> dict[str, Any]:
        client = self._client()
        try:
            client.auth_log_in()
            if not [p for p in client.search_plugins() if p.get("enabled")]:
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    "поиск по трекерам не настроен: в qBittorrent нет ни одного "
                    "включённого поискового плагина",
                )
            attempts = _query_ladder(query)
            usable: list[dict[str, Any]] = []
            broken: list[dict[str, Any]] = []
            total = 0
            used_query = attempts[0]
            for attempt in attempts:
                raw, total = self._run_search_job(client, attempt, limit)
                broken = [r for r in raw if _is_engine_error(r)]
                usable = [r for r in raw if not _is_engine_error(r)]
                used_query = attempt
                if usable or broken:
                    break
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()

        if broken and not usable:
            raise ProtoError(ERR_INTERNAL, f"поиск не отработал: {_engine_error_text(broken[0])}")

        results = [self._map_result(r) for r in usable]
        results.sort(key=lambda r: r["seeders"], reverse=True)
        shown = results[:limit]
        payload: dict[str, Any] = {
            "results": shown,
            "count": len(shown),
            "query": query,
        }
        if total > len(shown):
            payload["total"] = total
        if used_query != query.strip():
            payload["used_query"] = used_query
        return payload

    def _search_smart_sync(
        self, phrase: str, words: list[str], limit: int
    ) -> dict[str, Any]:
        client = self._client()
        try:
            client.auth_log_in()
            if not [p for p in client.search_plugins() if p.get("enabled")]:
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    "поиск по трекерам не настроен: в qBittorrent нет ни одного "
                    "включённого поискового плагина",
                )
            raw, _total = self._run_search_job(client, phrase, SEARCH_SMART_FETCH_LIMIT)
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()

        broken = [r for r in raw if _is_engine_error(r)]
        usable = [r for r in raw if not _is_engine_error(r)]
        if broken and not usable:
            raise ProtoError(ERR_INTERNAL, f"поиск не отработал: {_engine_error_text(broken[0])}")

        needles = [w.casefold() for w in words]
        matched = [
            r for r in usable if all(n in str(r.get("fileName", "")).casefold() for n in needles)
        ]

        results = [self._map_result(r) for r in matched]
        results.sort(key=lambda r: r["seeders"], reverse=True)
        shown = results[:limit]
        payload: dict[str, Any] = {
            "results": shown,
            "count": len(shown),
            "phrase": phrase,
        }
        if words:
            payload["words"] = words
        if len(results) > len(shown):
            payload["total"] = len(results)
        return payload

    def _add_sync(self, source: str, save_path: str) -> int | None:
        payload: dict[str, Any] = {}
        plugin: str | None = None
        if source.startswith("magnet:"):
            payload = {"urls": source}
        elif not _is_magnet_or_url(source):
            try:
                payload = {"torrent_files": base64.b64decode(source, validate=True)}
            except (binascii.Error, ValueError) as exc:
                raise ProtoError(ERR_BAD_REQUEST, f"невалидный base64 .torrent: {exc}") from exc
        free, _ = _disk_usage(save_path)
        if free is not None and free < MIN_FREE_BYTES:
            raise ProtoError(
                ERR_BAD_REQUEST,
                f"мало места в {save_path}: свободно {free / 1024**3:.1f} ГиБ "
                f"(минимум {MIN_FREE_BYTES / 1024**3:.0f} ГиБ) — освободите место "
                "или выберите другую директорию",
            )
        client = self._client()
        try:
            client.auth_log_in()
            if not payload:
                plugin = self._plugin_for(list(client.search_plugins()), source)
                if plugin is None:
                    raise ProtoError(
                        ERR_BAD_REQUEST,
                        "ссылку на .torrent беру только с трекеров, для которых "
                        "установлен поисковый плагин (иначе нужна magnet-ссылка "
                        "или сам файл)",
                    )
                payload = {"torrent_files": self._fetch_via_plugin(plugin, source)}
            client.torrents_add(save_path=save_path, **payload)
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()
        return free

    def _list_sync(self, *, with_hash: bool = False) -> tuple[list[dict[str, Any]], int]:
        client = self._client()
        try:
            client.auth_log_in()
            raw = client.torrents_info(sort="dlspeed", reverse=True, limit=LIST_LIMIT)
            limit_bytes = client.transfer_download_limit()
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()
        torrents = []
        for t in raw:
            item: dict[str, Any] = {
                "name": t.get("name", "?"),
                "state": t.get("state", "?"),
                "progress_pct": round(float(t.get("progress", 0.0)) * 100),
                "dlspeed_bytes_s": int(t.get("dlspeed", 0)),
                "upspeed_bytes_s": int(t.get("upspeed", 0)),
                "paused": str(t.get("state", "")).startswith(("paused", "stopped")),
                "eta_s": _eta_or_none(t.get("eta")),
                "seeds": max(0, int(t.get("num_seeds", 0) or 0)),
                "peers": max(0, int(t.get("num_leechs", 0) or 0)),
                "added_on": int(t.get("added_on", 0) or 0),
            }
            if with_hash:
                item["hash"] = str(t.get("hash", ""))
            torrents.append(item)
        limit_mbps = int(limit_bytes) // (1024 * 1024) if limit_bytes else 0
        return torrents, limit_mbps

    def _set_speed_limit_sync(self, mbps: int) -> None:
        limit_bytes = mbps * 1024 * 1024
        client = self._client()
        try:
            client.auth_log_in()
            client.transfer_set_download_limit(limit=limit_bytes)
            client.transfer_set_upload_limit(limit=limit_bytes)
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()

    @staticmethod
    def _select(raw: list[Any], selector: str) -> list[dict[str, Any]]:
        needle = selector.strip().lower()
        if needle in ALL_SELECTORS:
            return list(raw)
        exact = [t for t in raw if str(t.get("name", "")).lower() == needle]
        if exact:
            return exact
        by_hash = [t for t in raw if str(t.get("hash", "")).lower() == needle]
        if by_hash:
            return by_hash
        return [t for t in raw if needle in str(t.get("name", "")).lower()]

    def _switch_sync(self, selector: str, *, pause: bool) -> list[str]:
        client = self._client()
        try:
            client.auth_log_in()
            raw = list(client.torrents_info())
            picked = self._select(raw, selector)
            if not picked:
                known = ", ".join(str(t.get("name", "?")) for t in raw[:LIST_LIMIT])
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    f"не нашёл раздачу «{selector}» (сейчас есть: {known or 'ни одной'})",
                )
            if len(picked) > 1 and selector.strip().lower() not in ALL_SELECTORS:
                names = ", ".join(str(t.get("name", "?")) for t in picked)
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    f"под «{selector}» подходит несколько раздач ({names}) — "
                    "назовите одну точнее или скажите «все»",
                )
            hashes = [str(t.get("hash", "")) for t in picked]
            if pause:
                client.torrents_pause(torrent_hashes=hashes)
            else:
                client.torrents_resume(torrent_hashes=hashes)
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()
        return [str(t.get("name", "?")) for t in picked]

    def _space_sync(self) -> dict[str, Any]:
        dirs = []
        for path in self._cfg.save_dirs:
            free, total = _disk_usage(path)
            entry: dict[str, Any] = {"path": path, "free_bytes": free, "total_bytes": total}
            if free is None:
                entry["error"] = "путь недоступен (диск не смонтирован?)"
            dirs.append(entry)
        left: int | None = None
        try:
            client = self._client()
            try:
                client.auth_log_in()
                left = sum(int(t.get("amount_left", 0) or 0) for t in client.torrents_info())
            finally:
                client.auth_log_out()
        except qbittorrentapi.APIError:
            left = None
        return {"dirs": dirs, "downloading_left_bytes": left, "min_free_bytes": MIN_FREE_BYTES}

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_LIST:
            torrents, limit_mbps = await asyncio.to_thread(
                self._list_sync, with_hash=bool(args.get("with_hash"))
            )
            return {
                "torrents": torrents,
                "count": len(torrents),
                "limit": LIST_LIMIT,
                "speed_limit_mbps": limit_mbps,
            }
        if action == ACTION_SPEED_LIMIT:
            mbps = int(args.get("mbps") or 0)
            if not 0 <= mbps <= MAX_SPEED_LIMIT_MBPS:
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    f"лимит — от 0 (без ограничения) до {MAX_SPEED_LIMIT_MBPS} МБ/с",
                )
            await asyncio.to_thread(self._set_speed_limit_sync, mbps)
            return {"speed_limit_mbps": mbps}
        if action == ACTION_SPACE:
            return await asyncio.to_thread(self._space_sync)
        if action == ACTION_DETAILS:
            page = str(args.get("page") or "").strip()
            if not page.startswith(("http://", "https://")):
                raise ProtoError(ERR_BAD_REQUEST, "не указана ссылка на страницу раздачи (page)")
            return await asyncio.to_thread(self._details_sync, page)
        if action == ACTION_SEARCH:
            query = str(args.get("query") or "").strip()
            if not query:
                raise ProtoError(ERR_BAD_REQUEST, "не указан поисковый запрос (query)")
            limit = min(int(args.get("limit") or SEARCH_LIMIT), SEARCH_LIMIT)
            return await asyncio.to_thread(self._search_sync, query, limit)
        if action == ACTION_SEARCH_SMART:
            phrase = str(args.get("phrase") or "").strip()
            if not phrase:
                raise ProtoError(ERR_BAD_REQUEST, "не указана точная фраза для поиска (phrase)")
            words = str(args.get("words") or "").split()
            limit = min(int(args.get("limit") or SEARCH_LIMIT), SEARCH_LIMIT)
            return await asyncio.to_thread(self._search_smart_sync, phrase, words, limit)
        if action in (ACTION_PAUSE, ACTION_RESUME):
            selector = str(args.get("name") or "").strip()
            if not selector:
                raise ProtoError(ERR_BAD_REQUEST, "не указано имя раздачи (name)")
            names = await asyncio.to_thread(
                self._switch_sync, selector, pause=action == ACTION_PAUSE
            )
            key = "paused" if action == ACTION_PAUSE else "resumed"
            return {key: names, "count": len(names)}
        if action != ACTION_ADD:
            raise ValueError(f"необъявленное действие: {action}")
        source = str(args.get("source") or "")
        name = str(args.get("name") or "торрент")
        save_path = str(args.get("save_path") or "")
        if not source:
            raise ProtoError(ERR_BAD_REQUEST, "не указан source (magnet-ссылка или файл)")
        if save_path not in self._cfg.save_dirs:
            known = ", ".join(self._cfg.save_dirs) or "нет доступных директорий"
            raise ProtoError(
                ERR_BAD_REQUEST, f"недопустимая директория: {save_path!r} (есть: {known})"
            )
        free = await asyncio.to_thread(self._add_sync, source, save_path)
        return {"name": name, "save_path": save_path, "free_bytes": free}
