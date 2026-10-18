"""Наблюдатель за присутствием соседей: алерт о пропаже ноды, обязанной быть в сети.

Правило роя п. 4 (ARCHITECTURE §11) — «спящая не-24/7 нода это норма, а не
алерт». Но обратное тоже верно: если пропал сервер или VDS, которые обязаны
быть включены всегда, молчать нельзя. Отличить одно от другого позволяет тип
машины (`node/kind.py`), приезжающий от соседа в его ``hello``.

Два решения, которые стоит держать в голове при чтении:

- **Объявляет один.** Событие ``node_down`` рождается на той живой ноде, чей
  ``node_id`` лексикографически наименьший среди всех, кто сейчас на связи.
  Дедуп ``SeenEvents`` (`node/app.py`) спасает от повторной ретрансляции
  ОДНОГО события, но не от трёх РАЗНЫХ событий об одной пропаже — поэтому
  объявитель выбирается детерминированно и без переговоров.
- **Тип помнится.** Тип соседа нужен ровно тогда, когда сосед недоступен и
  спросить его нельзя. Поэтому узнанный из ``hello`` тип оседает в
  персистентном состоянии ноды и переживает рестарт.
"""

from __future__ import annotations

import asyncio
import logging

from sa_home_bot.node.kind import traits_for
from sa_home_bot.node.peers import NodeRouter
from sa_home_bot.node.state import NodeState
from sa_home_bot.node.supervisor import EventEmitter
from sa_home_bot.utils.lifespan import cancel_task

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 30.0

# Авария: нода, обязанная быть в сети, молчит дольше down_after_s.
EVENT_NODE_DOWN = "node_down"
# Конец аварии: та же нода снова на связи (только после node_down).
EVENT_NODE_UP = "node_up"
# Штатный уход: нода сама предупреждает соседей перед остановкой (эмитит
# run_node при завершении, этап 23). НЕ рождается вотчером — сама уходящая
# нода и есть источник, дедуп по env.id делает остальное.
EVENT_NODE_LEAVING = "node_leaving"
# Возвращение ноды после недоступности, которая НЕ была аварией (штатный
# уход, сон рабочей станции): информационно, для любого типа машины.
# До этапа 23 возвращение workstation не объявлялось вообще (node_up
# требовал предшествующего node_down, который для неё не рождается).
EVENT_NODE_RETURNED = "node_returned"


class PresenceWatcher:
    """Следит за пирами: копит их тип в состоянии, шлёт node_down/node_up."""

    def __init__(
        self,
        node_id: str,
        router: NodeRouter,
        *,
        emit: EventEmitter,
        state: NodeState | None = None,
        state_path: str | None = None,
        down_after_s: float = 300.0,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._node_id = node_id
        self._router = router
        self._emit = emit
        self._state = state
        self._state_path = state_path
        self._down_after_s = down_after_s
        self._poll_interval_s = poll_interval_s
        # Пиры, о падении которых мы уже сообщили — чтобы не повторять алерт
        # каждый тик и чтобы знать, о чьём возвращении сообщить.
        self._reported_down: set[str] = set()
        # Пиры, замеченные недоступными хотя бы на одном тике (любой тип
        # машины): по этому набору объявляется возвращение. Тик как порог —
        # естественный дебаунс: моргание линка короче интервала опроса не
        # порождает «вернулась».
        self._seen_down: set[str] = set()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="presence-watcher")

    async def stop(self) -> None:
        await cancel_task(self._task)
        self._task = None

    def _is_announcer(self) -> bool:
        """Объявляю ли пропажи я. Наименьший id среди себя и живых соседей —
        детерминированный выбор без переговоров: у всех, кто видит одну и ту
        же картину, ответ совпадает, а значит объявитель ровно один."""
        alive = [link.name for link in self._router.peers.values() if link.alive]
        return all(self._node_id < name for name in alive)

    def _remember_kinds(self) -> None:
        if self._state is None or self._state_path is None:
            return
        changed = False
        for link in self._router.peers.values():
            if link.node_kind and self._state.record_peer_kind(
                link.name, str(link.endpoint), link.endpoints, link.node_kind
            ):
                changed = True
        if changed:
            self._state.save(self._state_path)

    def _known_kind(self, peer_id: str) -> str:
        link = self._router.peers.get(peer_id)
        if link is not None and link.node_kind:
            return link.node_kind
        if self._state is not None:
            return self._state.peer_kind(peer_id)
        return ""

    async def check_once(self) -> None:
        self._remember_kinds()
        announcer = self._is_announcer()
        for link in self._router.peers.values():
            down_s = link.downtime_s()
            if down_s is None:  # на связи
                was_down = link.name in self._seen_down
                self._seen_down.discard(link.name)
                if link.name in self._reported_down:
                    # Конец аварии — node_up, как и раньше.
                    self._reported_down.discard(link.name)
                    if announcer:
                        await self._emit(
                            EVENT_NODE_UP,
                            {"node": link.name, "kind": self._known_kind(link.name)},
                        )
                elif was_down and announcer:
                    # Недоступность не была аварией (штатный уход, сон
                    # рабочей станции) — возвращение всё равно осмысленно
                    # для ЛЮБОГО типа машины (этап 23 п. 3).
                    await self._emit(
                        EVENT_NODE_RETURNED,
                        {"node": link.name, "kind": self._known_kind(link.name)},
                    )
                continue
            self._seen_down.add(link.name)
            if link.name in self._reported_down:
                continue
            if down_s < self._down_after_s:
                continue
            if link.left:
                continue  # предупредила об уходе — недоступность не авария
            if not traits_for(self._known_kind(link.name)).always_on:
                continue  # машина, которая штатно бывает выключена — молчим
            # Помечаем пропавшей независимо от того, объявляем ли мы: иначе
            # после смены объявителя посыпались бы запоздалые дубли.
            self._reported_down.add(link.name)
            if announcer:
                await self._emit(
                    EVENT_NODE_DOWN,
                    {
                        "node": link.name,
                        "kind": self._known_kind(link.name),
                        "down_s": round(down_s),
                    },
                )

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval_s)
            try:
                await self.check_once()
            except Exception:  # наблюдатель не должен ронять ноду
                log.exception("PresenceWatcher: ошибка проверки присутствия")
