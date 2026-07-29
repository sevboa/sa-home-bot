"""TorrentsService — ServiceHandler службы torrents (адаптер qBittorrent).

`add` — добавить раздачу по magnet-ссылке/URL или по
содержимому .torrent-файла. Файл идёт по протоколу как base64-строка
(`ActionParam` — только string|int|float|bool, PROTOCOL.md), обычные
.torrent-метафайлы на порядки меньше лимита сообщения (`MAX_MESSAGE_BYTES`
= 1 МиБ, proto/messages.py). Директория сохранения — конечный список из
конфига (`save_dirs`), а не свободный ввод: бот строит кнопки по `choices`
действия, в systemd не ходит — только к этой службе.

`list` — read-only состояние закачек. Появилось 2026-07-27 вместе с тулом
swarm_status: до него служба вообще не умела рассказать, что качается
(get_state отдавал только список директорий), и спросить Альфреда про
торренты было не у кого. Отдаёт узкий срез (имя, прогресс, состояние,
скорость, оценка) — путей на диске в нём нет: они ничего не добавляют к
ответу на вопрос «что там качается», а знать их этому потребителю незачем.

`pause`/`resume` — остановить и снова запустить раздачу. Адресуются
**по имени** (`name`), не по хэшу: имена — это то, что уже видит потребитель
в `list`, а сорокасимвольный хэш пришлось бы гонять в контекст модели
целиком и надеяться, что она перепишет его без опечатки. Разрешение имени в
хэш делает сама служба (`_select`): точное совпадение → хэш → узнаваемая
часть имени; неоднозначность — не «остановим на всякий случай все похожие»,
а честная ошибка со списком кандидатов (`все` — отдельное явное слово).

`space` — сколько места на дисках директорий сохранения. Свободное место
считается локально (`shutil.disk_usage`) — служба живёт на той же машине,
что и qBittorrent, спрашивать его Web API про чужие ФС незачем; сам
qBittorrent нужен только ради `downloading_left_bytes` (сколько ещё
предстоит докачать уже принятым раздачам), и если он молчит — это поле
просто `null`, а место всё равно отдаётся: на вопрос «хватает ли места»
клиент торрентов не нужен вовсе.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import shutil
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
ACTION_LIST = "list"
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"
ACTION_SPACE = "space"

# Сколько раздач отдавать за раз. Ответ читает и человек, и модель (тул
# torrents) — полный список на сотню раздач бесполезен обоим, а в контексте
# модели это ещё и заметные токены. Сортировка — по убыванию активности.
LIST_LIMIT = 20

# Ниже этого порога свободного места служба новую раздачу не принимает вовсе.
# Проверка именно здесь, а не только в подсказке модели: размер раздачи по
# magnet-ссылке заранее неизвестен (метаданные качаются уже после добавления),
# так что «влезет ли ИМЕННО ЭТО» не проверить в принципе — но забить диск
# досуха, когда там уже нечего занимать, можно и нужно не дать. Порог общий
# для всех директорий: на sdb (расходная «помойка») и на /mnt/data одинаково
# незачем доводить ФС до нуля.
MIN_FREE_BYTES = 2 * 1024**3

# «Останови всё» — отдельное явное слово, а не побочный эффект того, что
# пустая/короткая подстрока подошла ко всем раздачам сразу (см. _select).
ALL_SELECTORS = frozenset({"*", "all", "все", "всё", "все закачки", "всё сразу"})


def _is_magnet_or_url(source: str) -> bool:
    return source.startswith(("magnet:", "http://", "https://"))


def _disk_usage(path: str) -> tuple[int | None, int | None]:
    """(свободно, всего) в байтах, либо (None, None) — путь недоступен
    (не смонтирован внешний диск, опечатка в конфиге)."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None, None
    return usage.free, usage.total


# qBittorrent отдаёт eta = 8640000 (100 суток) как «неизвестно/бесконечность»
# для стоящих и раздающихся торрентов, а не как реальную оценку.
_ETA_UNKNOWN_S = 8640000


def _eta_or_none(raw: Any) -> int | None:
    try:
        eta = int(raw)
    except (TypeError, ValueError):
        return None
    return None if eta < 0 or eta >= _ETA_UNKNOWN_S else eta


class TorrentsService:
    def __init__(self, settings: Settings) -> None:
        self._cfg = settings.torrents
        self._node = socket.gethostname()

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=(ACTION_ADD, ACTION_LIST, ACTION_PAUSE, ACTION_RESUME, ACTION_SPACE),
            actions=(
                ActionSpec(
                    id=ACTION_LIST,
                    title="📋 Что качается",
                ),
                ActionSpec(
                    id=ACTION_SPACE,
                    title="💾 Сколько места",
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

    def _add_sync(self, source: str, save_path: str) -> int | None:
        """Свободные байты в целевой директории после приёма раздачи (или
        None — путь недоступен). Раздача не принимается вовсе, если места и
        так почти нет (MIN_FREE_BYTES)."""
        if _is_magnet_or_url(source):
            payload: dict[str, Any] = {"urls": source}
        else:
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
            client.torrents_add(save_path=save_path, **payload)
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()
        return free

    def _list_sync(self) -> list[dict[str, Any]]:
        client = self._client()
        try:
            client.auth_log_in()
            # sort/limit делает сам qBittorrent — тащить сотню раздач по сети,
            # чтобы выбросить хвост здесь, незачем.
            raw = client.torrents_info(sort="dlspeed", reverse=True, limit=LIST_LIMIT)
        except qbittorrentapi.APIError as exc:
            raise ProtoError(ERR_INTERNAL, f"qBittorrent отклонил запрос: {exc}") from exc
        finally:
            client.auth_log_out()
        return [
            {
                "name": t.get("name", "?"),
                "state": t.get("state", "?"),
                # progress приходит долей 0..1 — в процентах читают и человек,
                # и модель, а округление до целых убирает шум вида 0.9999.
                "progress_pct": round(float(t.get("progress", 0.0)) * 100),
                "dlspeed_bytes_s": int(t.get("dlspeed", 0)),
                # Сырое state — внутренние строки qBittorrent («stalledUP»,
                # «stoppedDL»), по которым не человеку, не модели не очевидно,
                # стоит раздача или нет; а решение «ставить на паузу или,
                # наоборот, запускать» именно на этом и держится. Префиксов
                # два: qBittorrent 5 переименовал paused* в stopped*.
                "paused": str(t.get("state", "")).startswith(("paused", "stopped")),
                # -1/огромное значение у qBittorrent означает "неизвестно"
                # (раздача стоит или сидируется) — отдаём None, чтобы модель не
                # пересказывала 8640000 секунд как реальную оценку.
                "eta_s": _eta_or_none(t.get("eta")),
            }
            for t in raw
        ]

    @staticmethod
    def _select(raw: list[Any], selector: str) -> list[dict[str, Any]]:
        """Раздачи, которые имел в виду вызывающий (см. докстринг модуля).

        Порядок проб — от самого однозначного к самому свободному: явное
        «все» → точное имя → хэш → узнаваемая часть имени. Без этого порядка
        точное имя, оказавшееся подстрокой другого («Foo» при живом
        «Foo.S02»), считалось бы неоднозначным, хотя вызывающий назвал его
        целиком.
        """
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
            # torrents_pause/_resume — версионно-безопасные псевдонимы: в
            # qBittorrent 5 эти эндпойнты называются stop/start, и библиотека
            # сама выбирает нужный по app_web_api_version.
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
        # Сколько ещё предстоит докачать уже принятым раздачам — лучший
        # ответ на «а хватит ли места»: свободное место само по себе не
        # учитывает того, что уже обещано текущим закачкам. Best-effort:
        # молчащий qBittorrent не должен отнимать ответ про сами диски.
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
            torrents = await asyncio.to_thread(self._list_sync)
            return {"torrents": torrents, "count": len(torrents), "limit": LIST_LIMIT}
        if action == ACTION_SPACE:
            return await asyncio.to_thread(self._space_sync)
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
        free = await asyncio.to_thread(self._add_sync, source, save_path)
        return {"name": name, "save_path": save_path, "free_bytes": free}
