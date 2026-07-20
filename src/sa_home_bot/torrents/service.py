"""TorrentsService — ServiceHandler службы torrents (адаптер qBittorrent).

Единственное умение — `add`: добавить раздачу по magnet-ссылке/URL или по
содержимому .torrent-файла. Файл идёт по протоколу как base64-строка
(`ActionParam` — только string|int|float|bool, PROTOCOL.md), обычные
.torrent-метафайлы на порядки меньше лимита сообщения (`MAX_MESSAGE_BYTES`
= 1 МиБ, proto/messages.py). Директория сохранения — конечный список из
конфига (`save_dirs`), а не свободный ввод: бот строит кнопки по `choices`
действия, в systemd не ходит — только к этой службе.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import socket
from typing import Any

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

SERVICE_NAME = "torrents"
ACTION_ADD = "add"


def _is_magnet_or_url(source: str) -> bool:
    return source.startswith(("magnet:", "http://", "https://"))


class TorrentsService:
    def __init__(self, settings: Settings) -> None:
        self._cfg = settings.torrents
        self._node = socket.gethostname()

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=(ACTION_ADD,),
            actions=(
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

    def _add_sync(self, source: str, save_path: str) -> None:
        if _is_magnet_or_url(source):
            payload: dict[str, Any] = {"urls": source}
        else:
            try:
                payload = {"torrent_files": base64.b64decode(source, validate=True)}
            except (binascii.Error, ValueError) as exc:
                raise ProtoError(ERR_BAD_REQUEST, f"невалидный base64 .torrent: {exc}") from exc
        client = self._client()
        try:
            client.auth_log_in()
            client.torrents_add(save_path=save_path, **payload)
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action != ACTION_ADD:
            # Сервер валидирует action по describe — сюда неизвестное не доходит.
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
        await asyncio.to_thread(self._add_sync, source, save_path)
        return {"name": name, "save_path": save_path}
