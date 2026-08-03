"""Клиент GitHub Releases для официального APK AmneziaWG — сетевой слой
кэша, который держит vpn/service.py.

Только ``urllib`` + ``asyncio.to_thread`` (как net/searxng.py и llm/ollama.py
— в проекте сознательно нет httpx/aiohttp). Свежесть проверяется через API
на КАЖДЫЙ запрос гостя (с коротким memo в самой службе — см.
VpnService._apk_info), а не по расписанию: кэш на jeeves — это ускоритель
доставки, а не источник правды о версии.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "sa-home-bot", "Accept": "application/vnd.github+json"}


class ApkFetchError(RuntimeError):
    """GitHub недоступен/ответил не тем — не значит, что кэша нет вовсе."""


def _get_json_sync(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — GitHub API, https
        return json.loads(resp.read())


def latest_release_sync(repo: str, timeout: float) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        payload = _get_json_sync(url, timeout)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ApkFetchError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise ApkFetchError("неожиданный ответ GitHub (не объект JSON)")
    return payload


def pick_apk_asset(assets: list[Any]) -> dict[str, Any] | None:
    for asset in assets:
        if isinstance(asset, dict) and str(asset.get("name") or "").lower().endswith(".apk"):
            return asset
    return None


def asset_sha256(asset: dict[str, Any]) -> str | None:
    """sha256 из поля ``digest`` (``"sha256:...."``) — GitHub его отдаёт не
    для всех релизов; отсутствие не ошибка, тогда сверяем только размер."""
    digest = asset.get("digest")
    if isinstance(digest, str) and digest.startswith("sha256:"):
        return digest.removeprefix("sha256:")
    return None


def download_sync(url: str, dest: Path, timeout: float) -> None:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — GitHub, https
        with open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_chunk(path: Path, offset: int, length: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(length)
