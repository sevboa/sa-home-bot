from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.chat_scope import require_chat_id
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.graph_memory.people import (
    ClaimError,
    build_cards,
    normalize_claim,
    strength_for,
)
from sa_home_bot.graph_memory.protocol import (
    ACTION_ADD_EPISODE,
    ACTION_PERSON_CARDS,
    ACTION_PERSON_CLAIM,
    ACTION_QUEUE_STATUS,
    ACTION_SEARCH,
    EPISODE_SOURCE_MEMORY_FACT,
    MAX_EPISODE_CHARS,
    PEOPLE_GROUP_ID,
    SERVICE_NAME,
)
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_UNAVAILABLE,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.utils.clock import utc_now

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
IDLE_POLL_S = 5.0
RETRY_BACKOFF_S = 15.0
DEFAULT_SEARCH_RESULTS = 5
MAX_SEARCH_RESULTS = 10
MAX_PERSON_CARDS = 50
_CYPHER_PERSON_CLAIM = (
    "MERGE (c:PersonClaim {group_id: $group_id, subject_id: $subject_id, "
    "field: $field, value: $value, by_id: $by_id}) "
    "SET c.strength = $strength, c.at = $at"
)
_CYPHER_PERSON_CARDS = (
    "MATCH (c:PersonClaim {group_id: $group_id}) WHERE c.subject_id IN $ids "
    "RETURN c.subject_id AS subject_id, c.field AS field, c.value AS value, "
    "c.by_id AS by_id, c.strength AS strength, c.at AS at"
)
EXTRACTION_REASONING_EFFORT = "none"
EXTRACTION_MAX_TOKENS = 4096
EXTRACTION_TEMPERATURE = 0.0


class GraphMemoryService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self._cfg = settings.graph_memory
        self._settings = settings
        self._db = db
        self._node = socket.gethostname()
        self._graphiti: Any = None
        self._graphiti_lock = asyncio.Lock()

    def _extraction_model(self) -> str:
        return self._cfg.extraction_model or self._settings.llm.model

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=(
                ACTION_ADD_EPISODE,
                ACTION_SEARCH,
                ACTION_QUEUE_STATUS,
                ACTION_PERSON_CLAIM,
                ACTION_PERSON_CARDS,
            ),
            actions=(
                ActionSpec(
                    id=ACTION_ADD_EPISODE,
                    title="🕸️ Добавить эпизод в граф",
                    params=(
                        ActionParam(name="text", type="string", title="Текст эпизода"),
                        ActionParam(name="chat_id", type="int", title="Чей это эпизод"),
                        ActionParam(
                            name="source",
                            type="string",
                            required=False,
                            title="Источник (по умолчанию memory_fact)",
                        ),
                    ),
                ),
                ActionSpec(
                    id=ACTION_SEARCH,
                    title="🔎 Поиск по графу",
                    params=(
                        ActionParam(name="query", type="string", title="О чём"),
                        ActionParam(name="chat_id", type="int", title="Чей граф"),
                        ActionParam(
                            name="limit", type="int", required=False, title="Сколько фактов"
                        ),
                    ),
                ),
                ActionSpec(id=ACTION_QUEUE_STATUS, title="📊 Состояние очереди"),
                ActionSpec(
                    id=ACTION_PERSON_CLAIM,
                    title="🪪 Утверждение о человеке",
                    params=(
                        ActionParam(name="subject_id", type="int", title="О ком (Telegram id)"),
                        ActionParam(name="field", type="string", title="gender / name / alias"),
                        ActionParam(name="value", type="string", title="Значение"),
                        ActionParam(name="by_id", type="int", title="Кто сказал (Telegram id)"),
                    ),
                ),
                ActionSpec(
                    id=ACTION_PERSON_CARDS,
                    title="🪪 Карточки людей",
                    params=(
                        ActionParam(name="ids", type="string", title="Telegram id через запятую"),
                    ),
                ),
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT status, count(*) AS n FROM graph_episodes GROUP BY status"
        )
        counts = {row["status"]: row["n"] for row in await cur.fetchall()}
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "queue_pending": counts.get("pending", 0) + counts.get("processing", 0),
            "queue_failed": counts.get("failed", 0),
            "queue_done": counts.get("done", 0),
            "graphiti_ready": self._graphiti is not None,
            "extraction_model": self._extraction_model(),
        }

    async def _get_graphiti(self) -> Any:
        if self._graphiti is not None:
            return self._graphiti
        async with self._graphiti_lock:
            if self._graphiti is not None:
                return self._graphiti
            try:
                self._graphiti = await self._build_graphiti()
            except Exception as exc:  # noqa: BLE001
                raise ProtoError(
                    ERR_UNAVAILABLE, f"graph_memory: Neo4j/Ollama недоступны: {exc}"
                ) from exc
            return self._graphiti

    async def _build_graphiti(self) -> Any:
        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.llm_client import LLMConfig
        from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

        class _ExtractionClient(OpenAIGenericClient):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                completions = self.client.chat.completions
                create = completions.create

                async def create_no_thinking(**params: Any) -> Any:
                    params.setdefault("reasoning_effort", EXTRACTION_REASONING_EFFORT)
                    return await create(**params)

                completions.create = create_no_thinking

            async def _generate_response(
                self,
                messages: Any,
                response_model: Any = None,
                max_tokens: int = EXTRACTION_MAX_TOKENS,
                *args: Any,
                **kwargs: Any,
            ) -> dict[str, Any]:
                return await super()._generate_response(
                    messages,
                    response_model,
                    min(max_tokens, EXTRACTION_MAX_TOKENS),
                    *args,
                    **kwargs,
                )

        base_url = "http://127.0.0.1:11434/v1"
        model = self._extraction_model()
        llm_client = _ExtractionClient(
            config=LLMConfig(
                api_key="ollama",
                model=model,
                small_model=model,
                base_url=base_url,
                temperature=EXTRACTION_TEMPERATURE,
            ),
            max_tokens=EXTRACTION_MAX_TOKENS,
        )
        embedder = OpenAIEmbedder(
            config=OpenAIEmbedderConfig(
                api_key="ollama",
                embedding_model=self._cfg.embedding_model,
                embedding_dim=768,
                base_url=base_url,
            )
        )
        reranker = OpenAIRerankerClient(
            config=LLMConfig(
                api_key="ollama",
                model=model,
                small_model=model,
                base_url=base_url,
            )
        )
        graphiti = Graphiti(
            self._cfg.uri,
            self._cfg.user,
            self._cfg.password,
            llm_client=llm_client,
            embedder=embedder,
            cross_encoder=reranker,
        )
        await graphiti.build_indices_and_constraints()
        return graphiti

    @staticmethod
    def _chat_id(args: dict[str, Any]) -> int:
        return require_chat_id(args, "граф у каждого чата свой")

    async def _add_episode(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        text = str(args.get("text") or "").strip()
        if not text:
            raise ProtoError(ERR_BAD_REQUEST, "нечего добавлять (text пуст)")
        if len(text) > MAX_EPISODE_CHARS:
            raise ProtoError(
                ERR_BAD_REQUEST,
                f"слишком длинно ({len(text)} знаков, максимум {MAX_EPISODE_CHARS})",
            )
        source = str(args.get("source") or EPISODE_SOURCE_MEMORY_FACT).strip()
        now = utc_now().isoformat()
        cur = await self._db.conn.execute(
            "INSERT INTO graph_episodes (chat_id, text, source, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (chat_id, text, source, now),
        )
        await self._db.conn.commit()
        return {"queued": True, "id": cur.lastrowid}

    async def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        query = str(args.get("query") or "").strip()
        if not query:
            raise ProtoError(ERR_BAD_REQUEST, "не указан query")
        limit = min(int(args.get("limit") or DEFAULT_SEARCH_RESULTS), MAX_SEARCH_RESULTS)
        graphiti = await self._get_graphiti()
        try:
            edges = await asyncio.wait_for(
                graphiti.search(query, group_ids=[str(chat_id)], num_results=limit),
                timeout=self._cfg.search_timeout_s,
            )
        except TimeoutError as exc:
            raise ProtoError(ERR_UNAVAILABLE, "graph_memory: поиск не уложился в таймаут") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProtoError(ERR_UNAVAILABLE, f"graph_memory: сбой поиска: {exc}") from exc
        facts = [edge.fact for edge in edges]
        return {"facts": facts, "count": len(facts)}

    @staticmethod
    def _person_id(args: dict[str, Any], key: str) -> int:
        raw = args.get(key)
        try:
            return int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ProtoError(ERR_BAD_REQUEST, f"{key} должен быть Telegram id: {raw!r}") from exc

    @staticmethod
    def _person_ids(args: dict[str, Any]) -> list[int]:
        raw = args.get("ids")
        if isinstance(raw, int):
            raw = str(raw)
        items = raw.split(",") if isinstance(raw, str) else list(raw or [])
        try:
            ids = sorted({int(str(item).strip()) for item in items if str(item).strip()})
        except ValueError as exc:
            raise ProtoError(ERR_BAD_REQUEST, f"ids — Telegram id через запятую: {raw!r}") from exc
        if len(ids) > MAX_PERSON_CARDS:
            raise ProtoError(ERR_BAD_REQUEST, f"не больше {MAX_PERSON_CARDS} карточек за раз")
        return ids

    async def _people_query(self, cypher: str, params: dict[str, Any]) -> Any:
        graphiti = await self._get_graphiti()
        try:
            return await asyncio.wait_for(
                graphiti.driver.execute_query(
                    cypher, params={"group_id": PEOPLE_GROUP_ID, **params}
                ),
                timeout=self._cfg.search_timeout_s,
            )
        except TimeoutError as exc:
            raise ProtoError(ERR_UNAVAILABLE, "graph_memory: Neo4j не ответил вовремя") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProtoError(ERR_UNAVAILABLE, f"graph_memory: сбой Neo4j: {exc}") from exc

    async def _person_claim(self, args: dict[str, Any]) -> dict[str, Any]:
        subject_id = self._person_id(args, "subject_id")
        by_id = self._person_id(args, "by_id")
        try:
            field, value = normalize_claim(args.get("field"), args.get("value"))
        except ClaimError as exc:
            raise ProtoError(ERR_BAD_REQUEST, str(exc)) from exc
        strength = strength_for(subject_id, by_id)
        await self._people_query(
            _CYPHER_PERSON_CLAIM,
            {
                "subject_id": subject_id,
                "field": field,
                "value": value,
                "by_id": by_id,
                "strength": strength,
                "at": utc_now().isoformat(),
            },
        )
        return {"saved": True, "field": field, "value": value, "strength": strength}

    async def _person_cards(self, args: dict[str, Any]) -> dict[str, Any]:
        ids = self._person_ids(args)
        if not ids:
            return {"cards": {}}
        result = await self._people_query(_CYPHER_PERSON_CARDS, {"ids": ids})
        claims = [dict(record) for record in result.records]
        return {"cards": {str(k): v for k, v in build_cards(claims).items()}}

    async def _queue_status(self, _args: dict[str, Any]) -> dict[str, Any]:
        return await self.get_state()

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_ADD_EPISODE:
            return await self._add_episode(args)
        if action == ACTION_SEARCH:
            return await self._search(args)
        if action == ACTION_QUEUE_STATUS:
            return await self._queue_status(args)
        if action == ACTION_PERSON_CLAIM:
            return await self._person_claim(args)
        if action == ACTION_PERSON_CARDS:
            return await self._person_cards(args)
        raise ValueError(f"необъявленное действие: {action}")


    async def _reset_stuck_processing(self) -> None:
        cur = await self._db.conn.execute(
            "UPDATE graph_episodes SET status = 'pending' WHERE status = 'processing'"
        )
        await self._db.conn.commit()
        if cur.rowcount:
            log.info("graph_memory: сброшено %d зависших эпизодов в pending", cur.rowcount)

    async def _next_pending(self) -> Any | None:
        cur = await self._db.conn.execute(
            "SELECT id, chat_id, text, source, attempts FROM graph_episodes "
            "WHERE status = 'pending' ORDER BY id LIMIT 1"
        )
        return await cur.fetchone()

    async def _process_one(self, row: Any) -> None:
        episode_id = row["id"]
        await self._db.conn.execute(
            "UPDATE graph_episodes SET status = 'processing' WHERE id = ?", (episode_id,)
        )
        await self._db.conn.commit()
        try:
            from graphiti_core.nodes import EpisodeType

            graphiti = await self._get_graphiti()
            await asyncio.wait_for(
                graphiti.add_episode(
                    name=f"episode-{episode_id}",
                    episode_body=row["text"],
                    source=EpisodeType.text,
                    source_description=row["source"],
                    reference_time=utc_now(),
                    group_id=str(row["chat_id"]),
                ),
                timeout=self._cfg.ingest_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            attempts = row["attempts"] + 1
            failed = attempts >= MAX_ATTEMPTS
            log.warning(
                "graph_memory: эпизод id=%s провален (попытка %d/%d): %s",
                episode_id,
                attempts,
                MAX_ATTEMPTS,
                exc,
            )
            await self._db.conn.execute(
                "UPDATE graph_episodes SET status = ?, attempts = ?, error = ? WHERE id = ?",
                ("failed" if failed else "pending", attempts, str(exc), episode_id),
            )
            await self._db.conn.commit()
            if not failed:
                await asyncio.sleep(RETRY_BACKOFF_S)
            return
        now = utc_now().isoformat()
        await self._db.conn.execute(
            "UPDATE graph_episodes SET status = 'done', processed_at = ? WHERE id = ?",
            (now, episode_id),
        )
        await self._db.conn.commit()
        log.info("graph_memory: эпизод id=%s обработан", episode_id)

    async def _ingest_loop(self) -> None:
        await self._reset_stuck_processing()
        while True:
            row = await self._next_pending()
            if row is None:
                await asyncio.sleep(IDLE_POLL_S)
                continue
            await self._process_one(row)

    def start_ingest_loop(self) -> asyncio.Task[None]:
        return asyncio.create_task(self._ingest_loop(), name="graph-memory-ingest-loop")

    async def aclose(self) -> None:
        if self._graphiti is not None:
            await self._graphiti.close()
