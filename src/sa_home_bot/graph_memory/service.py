"""GraphMemoryService — ServiceHandler службы graph_memory (графовая память
Альфреда, Neo4j + Graphiti, Этап 41).

Зачем отдельно от `memory`: та служба хранит отдельные факты («качаем в
/mnt/data/pr») и ищет их полнотекстовым поиском (FTS5), но не умеет
отвечать на реляционные вопросы («кто жена Алексея», если факт был записан
как «Наташа — жена Алексея», а спросили про Алексея). graph_memory решает
это через граф сущностей/рёбер: экстракция текста в граф — через LLM
(Ollama на mycraft), поиск — гибридный (векторный + текстовый + граф).

**ОСЛАБЛЕННЫЙ инвариант в отличие от memory** (см. protocol.py::NODE_ID):
служба пинуется на mycraft, потому что там живут Neo4j и Ollama, а не на
всегда-включённой ноде. Её недоступность во время сна mycraft —
предусмотренная деградация, а не авария: recall_graph_facts
(bot/ai_flow.py) в этом случае молча отдаёт [], `memory` продолжает
работать как обычно.

**Партиционирование по chat_id**: как и у `memory`, привязка к чату —
принципиальная граница приватности (сказанное в личке не должно всплыть в
общей группе), а не деталь реализации. Держим её тем же способом, что и
Graphiti предлагает из коробки — `group_id` графа Graphiti = str(chat_id).

**Асинхронная запись**: экстракция через LLM — секунды, не миллисекунды, и
не должна блокировать вызывающего (тул `memory remember` в bot/tools.py,
best-effort). ACTION_ADD_EPISODE сразу пишет строку в таблицу
`graph_episodes` (SQLite, персистентно) со статусом pending и отвечает;
`_ingest_loop` обрабатывает очередь последовательно в фоне (по образцу
tasks/service.py::fire_loop), двигая status pending -> processing ->
done/failed с ретраями до MAX_ATTEMPTS. При старте службы `processing`-хвост
от прошлого (возможно, аварийного) завершения сбрасывается обратно в
pending — иначе рестарт посреди обработки терял бы эпизод навсегда.

**Ленивая инициализация Graphiti-клиента**: конструируется при первом
обращении, а не при старте службы — Neo4j/Ollama на mycraft могут быть ещё
не готовы (или сама эта нода просыпается) в момент, когда стартует
sa-home-node. Любая ошибка построения/использования клиента (Neo4j
недоступен, Ollama не отвечает, модель не загружена) превращается в
ProtoError(ERR_UNAVAILABLE) — штатный отказ действия, не исключение наружу.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import UTC, datetime
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.graph_memory.protocol import (
    ACTION_ADD_EPISODE,
    ACTION_QUEUE_STATUS,
    ACTION_SEARCH,
    EPISODE_SOURCE_MEMORY_FACT,
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

log = logging.getLogger(__name__)

# Как MAX_FACT_CHARS у memory: эпизод — одна мысль, не пересказ разговора.
MAX_EPISODE_CHARS = 400
MAX_ATTEMPTS = 3
# Пауза между тиками очереди, когда она пуста — реже, чем poll задач tasks
# (там речь о таймерах день-в-день, здесь просто фоновая обработка очереди).
IDLE_POLL_S = 5.0
# Пауза перед повтором эпизода, упавшего с ошибкой — не долбить Neo4j/Ollama
# без перерыва, если они временно недоступны.
RETRY_BACKOFF_S = 15.0
DEFAULT_SEARCH_RESULTS = 5
MAX_SEARCH_RESULTS = 10
# Параметры LLM-экстракции Graphiti (замер 2026-09-25 на mycraft, gemma через
# /v1): без них эпизод шёл 30-110 с и держал общую очередь Ollama, в которой
# стоит живой чат (см. _ExtractionClient).
# - Через OpenAI-совместимый /v1 Ollama по умолчанию ВКЛЮЧАЕТ thinking у gemma
#   (нативный /api/chat бота — нет): ~60% токенов ответа уходило в рассуждение
#   перед JSON. "none" выключает его, JSON не хуже.
EXTRACTION_REASONING_EFFORT = "none"
# Дефолт Graphiti — 16384, а extract_edges передаёт свой лимит в обход
# LLMConfig. Отдельные ответы убегали до 3-4 тыс. токенов и обрывались по
# таймауту (500 от Ollama, эпизод failed) — режем в _generate_response.
EXTRACTION_MAX_TOKENS = 4096
# Экстракции нужна детерминированность, а не разнообразие (дефолт — 1.0).
EXTRACTION_TEMPERATURE = 0.0


def _row_to_episode(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "text": row["text"],
        "source": row["source"],
        "status": row["status"],
        "attempts": row["attempts"],
        "created_at": row["created_at"],
    }


class GraphMemoryService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self._cfg = settings.graph_memory
        # Полный Settings, не только graph_memory — нужен settings.llm.model
        # для _extraction_model() (см. config.py::GraphMemoryConfig).
        self._settings = settings
        self._db = db
        self._node = socket.gethostname()
        self._graphiti: Any = None
        self._graphiti_lock = asyncio.Lock()

    def _extraction_model(self) -> str:
        """Тег модели для экстракции/reranker'а — settings.llm.model, если
        graph_memory.extraction_model не задан явно (см. докстринг
        GraphMemoryConfig в config.py: намеренно ТА ЖЕ модель, что уже
        держит в VRAM живой чат, а не отдельная — избегаем и вытеснения из
        VRAM, и очереди Ollama позади медленной CPU-генерации)."""
        return self._cfg.extraction_model or self._settings.llm.model

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=(ACTION_ADD_EPISODE, ACTION_SEARCH, ACTION_QUEUE_STATUS),
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
        """Ленивая инициализация — см. докстринг модуля. Лок, а не проверка
        `is None` без него: два одновременных первых запроса иначе строили бы
        клиента дважды (гонка, лишние соединения к Neo4j)."""
        if self._graphiti is not None:
            return self._graphiti
        async with self._graphiti_lock:
            if self._graphiti is not None:
                return self._graphiti
            try:
                self._graphiti = await self._build_graphiti()
            except Exception as exc:  # noqa: BLE001 — любой сбой = недоступность
                raise ProtoError(
                    ERR_UNAVAILABLE, f"graph_memory: Neo4j/Ollama недоступны: {exc}"
                ) from exc
            return self._graphiti

    async def _build_graphiti(self) -> Any:
        # Импорты внутри метода, а не на верху модуля: graphiti-core/neo4j —
        # extras-зависимость, живущая только на mycraft (см. pyproject.toml).
        # Тяжёлый импорт при первом реальном обращении, а не при импорте
        # service.py — тот импортируется и app.py, и (косвенно) тестами.
        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.llm_client import LLMConfig
        from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

        class _ExtractionClient(OpenAIGenericClient):
            """OpenAIGenericClient с выключенным thinking и потолком
            max_tokens — см. EXTRACTION_* выше. Graphiti не даёт передать
            свои параметры запроса, поэтому reasoning_effort вшиваем в
            create() клиента openai, а потолок — в _generate_response (туда
            приходит и лимит, который extract_edges задаёт сам)."""

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
        # Reranker (cross-encoder) на практике не вызывается: search()
        # (graph_memory/service.py::_search) использует EDGE_HYBRID_SEARCH_RRF
        # — чистый Reciprocal Rank Fusion по BM25+cosine, без LLM. Держим
        # клиент только чтобы конструктор Graphiti не завёл свой дефолтный
        # OpenAIRerankerClient() без api_key (падает на
        # "Missing credentials"). Та же модель — на случай, если граф когда-то
        # перейдёт на search_() с cross-encoder ranking.
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
        raw = args.get("chat_id")
        if raw is None:
            raise ProtoError(ERR_BAD_REQUEST, "не указан chat_id — граф у каждого чата свой")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ProtoError(ERR_BAD_REQUEST, f"chat_id должен быть числом: {raw!r}") from exc

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
        now = datetime.now(tz=UTC).isoformat()
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
        except Exception as exc:  # noqa: BLE001 — сбой поиска = недоступность
            raise ProtoError(ERR_UNAVAILABLE, f"graph_memory: сбой поиска: {exc}") from exc
        facts = [edge.fact for edge in edges]
        return {"facts": facts, "count": len(facts)}

    async def _queue_status(self, _args: dict[str, Any]) -> dict[str, Any]:
        return await self.get_state()

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_ADD_EPISODE:
            return await self._add_episode(args)
        if action == ACTION_SEARCH:
            return await self._search(args)
        if action == ACTION_QUEUE_STATUS:
            return await self._queue_status(args)
        raise ValueError(f"необъявленное действие: {action}")

    # --- фоновая обработка очереди ---

    async def _reset_stuck_processing(self) -> None:
        """processing-хвост от прошлого (возможно, аварийного) завершения
        службы -> обратно в pending, иначе рестарт посреди обработки терял
        бы эпизод навсегда (см. докстринг модуля и Верификацию, п.6 плана)."""
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
                    reference_time=datetime.now(tz=UTC),
                    group_id=str(row["chat_id"]),
                ),
                timeout=self._cfg.ingest_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — сбой одного эпизода не должен ронять очередь
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
        now = datetime.now(tz=UTC).isoformat()
        await self._db.conn.execute(
            "UPDATE graph_episodes SET status = 'done', processed_at = ? WHERE id = ?",
            (now, episode_id),
        )
        await self._db.conn.commit()
        log.info("graph_memory: эпизод id=%s обработан", episode_id)

    async def _ingest_loop(self) -> None:
        # Отмена (см. app.py::run_graph_memory) приходит как CancelledError в
        # await-точке ниже и штатно всплывает — по тому же приёму, что и
        # tasks/app.py::run_tasks (cancel() + suppress снаружи, не здесь).
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
