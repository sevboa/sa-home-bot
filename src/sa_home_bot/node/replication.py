"""Репликация пакетов настроек по рою.

Задача одна: у всех нод, несущих один и тот же инстанс службы-синглтона,
пакет настроек должен быть одинаковым — и на активной, и на резервной. Иначе
резерв бесполезен: он поднимется на настройках, устаревших ровно настолько,
насколько давно их правили.

Как это устроено (без выделенного хранилища и без выборов координатора):

- **Правка замечается там, где сделана.** Владелец правит пакет как обычный
  файл; нода видит изменение содержимого, поднимает ревизию и объявляет её
  рою событием ``instance_config_changed``. Ревизия — сайдкар рядом с файлом
  (`node/instances.py`), сам пакет нода не переписывает.
- **Тянет тот, кому нужно.** Событие несёт только «есть ревизия N у ноды X»;
  сам пакет забирает уже заинтересованная нода — та, которой этот инстанс
  назначен, всё равно в какой роли. Секреты не летят в broadcast, а едут
  адресным ответом.
- **Догон вместо гарантий доставки.** События доставляются at-most-once, а
  нода могла быть выключена, когда настройки правили. Поэтому раз в цикл
  нода просто спрашивает живых соседей, какие у них ревизии, и подтягивает
  всё, что новее. Пропущенное событие перестаёт что-либо значить.
- **Активная служба перезапускается.** Конфиг иммутабелен по построению
  (ARCHITECTURE §9 п. 7) — применить новые настройки можно только рестартом.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sa_home_bot.node.assignments import Assignment
from sa_home_bot.node.instances import (
    InstanceMeta,
    InstanceStore,
    base_instance,
    is_satellite,
    slot_key,
)
from sa_home_bot.node.peers import NodeRouter
from sa_home_bot.node.supervisor import EventEmitter, Supervisor
from sa_home_bot.proto.messages import (
    MSG_COMMAND,
    MSG_GET_STATE,
    Address,
    Envelope,
    ProtoError,
    make_request,
)

log = logging.getLogger(__name__)

EVENT_INSTANCE_CONFIG_CHANGED = "instance_config_changed"

ACTION_LIST_INSTANCES = "list_instances"
ACTION_GET_INSTANCE_CONFIG = "get_instance_config"

POLL_INTERVAL_S = 30.0
PEER_TIMEOUT_S = 10.0

NODE_SERVICE = "node"


def _encode_package(data: bytes) -> str:
    """Пакет в JSON-конверт — как текст: TOML и есть текст, а base64 сделал бы
    отладочный вывод протокола нечитаемым."""
    return data.decode("utf-8")


def _decode_package(text: str) -> bytes:
    return text.encode("utf-8")


class ConfigReplicator:
    """Держит пакеты назначенных инстансов в актуальном состоянии."""

    def __init__(
        self,
        node_id: str,
        store: InstanceStore,
        *,
        supervisor: Supervisor,
        router: NodeRouter | None,
        emit: EventEmitter,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._node_id = node_id
        self._store = store
        self._supervisor = supervisor
        self._router = router
        self._emit = emit
        self._poll_interval_s = poll_interval_s
        self._task: asyncio.Task | None = None

    # --- что нас касается ---------------------------------------------------

    def _my_instances(self) -> list[Assignment]:
        """Инстансы, назначенные этой ноде, — активные и резервные наравне:
        резерв обязан быть готов, а значит и настройки ему нужны те же."""
        return [
            svc.assignment
            for svc in self._supervisor.services.values()
            if svc.assignment.instance
        ]

    def _cares_about(self, service: str, instance: str) -> bool:
        """Наш ли это пакет. Сателлит (``<инстанс>.guests``) принадлежит
        своему хозяину: назначен инстанс — значит нужны и его спутники."""
        base = base_instance(instance)
        return any(a.service == service and a.instance == base for a in self._my_instances())

    def local_revisions(self) -> list[dict]:
        """Ревизии наших пакетов — этим нода отвечает на get_state."""
        return [m.to_dict() for m in self._store.known()]

    # --- локальные правки ---------------------------------------------------

    async def announce_local_changes(self) -> list[InstanceMeta]:
        """Заметить правки владельца и объявить их рою."""
        changed = self._store.refresh_all()
        for meta in changed:
            await self._emit(EVENT_INSTANCE_CONFIG_CHANGED, meta.to_dict())
            # Сателлит правит сама служба (гостевые подписки — бот), и своё
            # изменение она уже держит в памяти — рестарт ей ничего не даст,
            # зато оборвал бы разговор ровно в тот момент, когда гостя только
            # что впустили. Правку сателлита руками рестартом тоже не ловим:
            # файл заявлен как ведомый службой (subscriptions/guests.py),
            # человеку туда лезть незачем. Реплика от соседа — другое дело
            # (см. _pull): там наша копия действительно отстала.
            if not is_satellite(meta.instance):
                await self._apply_to_running_service(meta)
        return changed

    # --- приём -------------------------------------------------------------

    async def on_config_changed(self, env: Envelope) -> None:
        """Сосед объявил новую ревизию — забрать её, если инстанс наш."""
        data = env.payload.get("data", {})
        meta = InstanceMeta.from_dict(data)
        if not meta.service or not meta.instance:
            return
        if not self._cares_about(meta.service, meta.instance):
            return
        local = self._store.read_meta(meta.service, meta.instance)
        if not meta.wins_over(local):
            return
        source = meta.origin_node or (env.src.node if env.src else "")
        if not source or source == self._node_id:
            return
        await self._pull(meta, source)

    async def sync_from_peers(self) -> None:
        """Догон: спросить живых соседей их ревизии и подтянуть, что новее.

        Нужен там, где события бессильны: нода была выключена, когда пакет
        правили, — событие до неё просто не доехало.
        """
        if self._router is None:
            return
        wanted = self._my_instances()
        if not wanted:
            return
        for peer_id, link in list(self._router.peers.items()):
            if not link.alive:
                continue
            state = await self._peer_state(peer_id, link)
            if state is None:
                continue
            for raw in state.get("instances", []):
                meta = InstanceMeta.from_dict(raw)
                if not self._cares_about(meta.service, meta.instance):
                    continue
                local = self._store.read_meta(meta.service, meta.instance)
                if meta.wins_over(local):
                    await self._pull(meta, peer_id)

    async def _peer_state(self, peer_id: str, link) -> dict | None:
        request = make_request(
            MSG_GET_STATE,
            src=Address(node=self._node_id, service=NODE_SERVICE),
            dst=Address(node=peer_id, service=NODE_SERVICE),
            timeout_s=PEER_TIMEOUT_S,
        )
        try:
            response = await asyncio.wait_for(link.forward(request), PEER_TIMEOUT_S)
        except (ProtoError, TimeoutError, ConnectionError, OSError) as exc:
            log.debug("Репликация: нода %s не ответила о своих пакетах (%s)", peer_id, exc)
            return None
        return response.payload if response.ok else None

    async def _pull(self, meta: InstanceMeta, source: str) -> None:
        """Забрать пакет у ноды-источника и применить."""
        if self._router is None:
            return
        link = self._router.peers.get(source)
        if link is None or not link.alive:
            return
        request = make_request(
            MSG_COMMAND,
            {
                "action": ACTION_GET_INSTANCE_CONFIG,
                "args": {"service": meta.service, "instance": meta.instance},
            },
            src=Address(node=self._node_id, service=NODE_SERVICE),
            dst=Address(node=source, service=NODE_SERVICE),
            timeout_s=PEER_TIMEOUT_S,
        )
        try:
            response = await asyncio.wait_for(link.forward(request), PEER_TIMEOUT_S)
        except (ProtoError, TimeoutError, ConnectionError, OSError) as exc:
            log.warning(
                "Репликация: не удалось забрать пакет %s с ноды %s (%s) — попробую позже",
                slot_key(meta.service, meta.instance), source, exc,
            )
            return
        if not response.ok:
            log.warning(
                "Репликация: нода %s отказала в пакете %s (%s)",
                source, slot_key(meta.service, meta.instance),
                (response.error or {}).get("message", "?"),
            )
            return
        payload = response.payload
        content = payload.get("content")
        if not isinstance(content, str):
            return
        remote_meta = InstanceMeta.from_dict(payload.get("meta", meta.to_dict()))
        if self._store.apply(remote_meta, _decode_package(content)):
            await self._apply_to_running_service(remote_meta)

    async def _apply_to_running_service(self, meta: InstanceMeta) -> None:
        """Новый пакет вступает в силу только рестартом — конфиг читается
        один раз при старте и дальше иммутабелен (ARCHITECTURE §9 п. 7).
        Резервную службу не трогаем: она и не запущена.

        Слот ищем по хозяину: у сателлита своего слота нет — он часть
        настроек той же службы, что и основной пакет."""
        slot = self._supervisor.get(slot_key(meta.service, base_instance(meta.instance)))
        if slot is None or slot.assignment.standby:
            return
        if slot.status != "running":
            return
        log.info(
            "Пакет %s обновлён до ревизии %d — перезапускаю службу",
            slot.name, meta.rev,
        )
        await slot.restart()

    # --- ответы на запросы соседей ------------------------------------------

    def package_payload(self, service: str, instance: str) -> dict | None:
        """Содержимое пакета + его ревизия — ответ на get_instance_config."""
        data = self._store.read_package(service, instance)
        meta = self._store.read_meta(service, instance)
        if data is None or meta is None:
            return None
        return {"meta": meta.to_dict(), "content": _encode_package(data)}

    # --- цикл ---------------------------------------------------------------

    async def tick(self) -> None:
        await self.announce_local_changes()
        await self.sync_from_peers()

    async def start(self) -> None:
        # Первый проход сразу: нода могла проспать все правки, пока была
        # выключена, и поднимать службу на устаревшем пакете незачем.
        with contextlib.suppress(Exception):
            await self.tick()
        self._task = asyncio.create_task(self._run(), name="config-replicator")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval_s)
            try:
                await self.tick()
            except Exception:  # репликация не должна ронять ноду
                log.exception("Репликация: ошибка цикла синхронизации")
