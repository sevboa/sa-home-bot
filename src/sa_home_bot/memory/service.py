"""MemoryService — ServiceHandler службы memory (долгая память Альфреда).

Зачем: у модели нет памяти между разговорами. Всё, что Альфред «знает», —
это либо персонажный промпт (статика, лежит на ноде с llm и стоит контекста
на КАЖДОМ вызове), либо текущий тред. «Качаем в /mnt/data/pr», «смотрим в
озвучке LostFilm», «jeeves — это VDS» хранить было негде.

**Память — по чату** (решение пользователя 2026-07-29): сказанное в личке не
должно всплыть в общей группе. `chat_id` проставляет не модель, а бот
(bot/tools.py) — модель этого параметра не видит вовсе и до чужой памяти
дотянуться не может.

Поверх этого две оси (просьба пользователя 2026-07-29):

* `scope="common"` — общее знание дома: кто такой Альфред, как он выглядит,
  что умеет рой, кто есть кто. Видно из любого разговора, потому что это не
  чья-то тайна, а справочник. Смысл в том, чтобы вынести справочную часть из
  персонажного промпта, который иначе целиком едет в контекст на КАЖДОМ
  вызове. Такие записи заводит человек руками (`nodectl call memory
  remember scope=common …`) — тул модели этого параметра не даёт, чтобы
  сказанное в одном чате не стало вдруг общим знанием.
* `sensitive=true` — личное (адрес, здоровье, деньги, документы). Никогда не
  бывает `common` (служба откажет), а в контекст уезжает с оговоркой «знай,
  но вслух не пересказывай» (bot/ai_flow.py). Честно про границы: жёсткая
  здесь только видимость — sensitive-факт физически не покидает свой чат;
  «не произноси вслух» — это просьба к модели, а не гарантия.

Поиск — обычный полнотекстовый (FTS5 из системного sqlite, никаких новых
зависимостей и никаких эмбеддингов). Эмбеддинги сознательно не заводим:
единственная машина с моделями — winpc, которая штатно спит, а память
обязана отвечать всегда. Токенайзер `trigram`, а не стандартный: он ищет по
подстроке, что для русского практичнее — «озвучк» находит «озвучке». Полной
морфологии это не даёт («качаю» не найдёт «качаем»), и обещать её не надо:
запрос из нескольких слов вытягивает нужное (см. _match_query — слова
объединяются через OR, а не AND).
"""

from __future__ import annotations

import re
import socket
from datetime import UTC, datetime
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.memory.protocol import (
    ACTION_FORGET,
    ACTION_LIST,
    ACTION_RECALL,
    ACTION_REMEMBER,
    SCOPE_CHAT,
    SCOPE_COMMON,
    SERVICE_NAME,
)
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)

# Факт — это одна мысль, а не пересказ разговора: длинные записи и вспоминать
# бесполезно (они подойдут под любой запрос), и в контекст модели их не
# вставить — там на всю память отведены сотни знаков (bot/ai_flow.py).
MAX_FACT_CHARS = 400
# Сколько фактов отдавать. Больше трёх в контекст всё равно не влезет, но
# оставляем запас для ручных вызовов через nodectl.
DEFAULT_RECALL = 3
MAX_RECALL = 10
# Короче трёх букв триграммный индекс не ищет в принципе, а предлоги и союзы
# как поисковые слова только шумят.
MIN_WORD_CHARS = 3
SCOPES = (SCOPE_CHAT, SCOPE_COMMON)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _match_query(text: str) -> str:
    """Текст сообщения → выражение MATCH для FTS5.

    Слова объединяются через OR: совпадение хотя бы по одному слову лучше,
    чем ничего (ранжирование bm25 само поднимет запись, где совпало больше).
    Каждое слово — в кавычках как фраза: иначе пунктуация и служебные слова
    FTS5 (`AND`, `NOT`, `*`) из живого сообщения ломают разбор запроса.
    """
    words = [w for w in _WORD_RE.findall(text.lower()) if len(w) >= MIN_WORD_CHARS]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


def _row_to_fact(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "text": row["text"],
        "scope": row["scope"],
        "sensitive": bool(row["sensitive"]),
        "created_at": row["created_at"],
    }


class MemoryService:
    def __init__(self, settings: Settings, db: Database) -> None:
        self._cfg = settings.memory
        self._db = db
        self._node = socket.gethostname()

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=(ACTION_REMEMBER, ACTION_RECALL, ACTION_FORGET, ACTION_LIST),
            actions=(
                ActionSpec(
                    id=ACTION_REMEMBER,
                    title="🧠 Запомнить",
                    params=(
                        ActionParam(name="text", type="string", title="Что запомнить"),
                        ActionParam(name="chat_id", type="int", title="Чей это факт"),
                        ActionParam(
                            name="scope",
                            type="string",
                            required=False,
                            title="chat (по умолчанию) или common — общее знание дома",
                            choices=(SCOPE_CHAT, SCOPE_COMMON),
                        ),
                        ActionParam(
                            name="sensitive",
                            type="bool",
                            required=False,
                            title="Личное: не пересказывать вслух, никогда не common",
                        ),
                    ),
                ),
                ActionSpec(
                    id=ACTION_RECALL,
                    title="💭 Вспомнить",
                    params=(
                        ActionParam(name="query", type="string", title="О чём"),
                        ActionParam(name="chat_id", type="int", title="Чья память"),
                    ),
                ),
                ActionSpec(
                    id=ACTION_FORGET,
                    title="🧽 Забыть",
                    params=(
                        ActionParam(name="id", type="int", title="Номер факта"),
                        ActionParam(name="chat_id", type="int", title="Чья память"),
                    ),
                ),
                ActionSpec(
                    id=ACTION_LIST,
                    title="📒 Что помню",
                    params=(ActionParam(name="chat_id", type="int", title="Чья память"),),
                ),
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        cur = await self._db.conn.execute("SELECT count(*) AS n FROM facts")
        row = await cur.fetchone()
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "facts": row["n"] if row else 0,
        }

    @staticmethod
    def _chat_id(args: dict[str, Any]) -> int:
        raw = args.get("chat_id")
        if raw is None:
            raise ProtoError(ERR_BAD_REQUEST, "не указан chat_id — память у каждого чата своя")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ProtoError(ERR_BAD_REQUEST, f"chat_id должен быть числом: {raw!r}") from exc

    @staticmethod
    def _scope(args: dict[str, Any], *, sensitive: bool) -> str:
        scope = str(args.get("scope") or SCOPE_CHAT).strip().lower()
        if scope not in SCOPES:
            raise ProtoError(ERR_BAD_REQUEST, f"неизвестный scope: {scope!r} (есть: chat, common)")
        if scope == SCOPE_COMMON and sensitive:
            # Не «предупредим и запишем», а отказ: общее знание видно во всех
            # чатах, и личное там не место по определению.
            raise ProtoError(
                ERR_BAD_REQUEST,
                "личный факт не может быть общим: sensitive-записи живут только "
                "в том разговоре, где их сказали",
            )
        return scope

    async def _remember(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        sensitive = bool(args.get("sensitive"))
        scope = self._scope(args, sensitive=sensitive)
        text = str(args.get("text") or "").strip()
        if not text:
            raise ProtoError(ERR_BAD_REQUEST, "нечего запоминать (text пуст)")
        if len(text) > MAX_FACT_CHARS:
            raise ProtoError(
                ERR_BAD_REQUEST,
                f"слишком длинно ({len(text)} знаков, максимум {MAX_FACT_CHARS}) — "
                "запоминай одну мысль, а не пересказ разговора",
            )
        now = datetime.now(tz=UTC).isoformat()
        cur = await self._db.conn.execute(
            "INSERT INTO facts (text, chat_id, scope, sensitive, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (text, chat_id, scope, int(sensitive), now),
        )
        await self._db.conn.commit()
        return {"id": cur.lastrowid, "text": text, "scope": scope, "sensitive": sensitive}

    async def _recall(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        limit = min(int(args.get("limit") or DEFAULT_RECALL), MAX_RECALL)
        match = _match_query(str(args.get("query") or ""))
        if not match:
            return {"facts": [], "count": 0}
        # Общее знание дома видно из любого разговора, всё остальное — только
        # из своего: это и есть граница приватности, и держится она запросом,
        # а не обещанием модели молчать.
        cur = await self._db.conn.execute(
            "SELECT rowid AS id, text, scope, sensitive, created_at FROM facts "
            "WHERE facts MATCH ? AND (chat_id = ? OR scope = ?) ORDER BY rank LIMIT ?",
            (match, chat_id, SCOPE_COMMON, limit),
        )
        facts = [_row_to_fact(row) for row in await cur.fetchall()]
        return {"facts": facts, "count": len(facts)}

    async def _forget(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        try:
            fact_id = int(args.get("id"))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ProtoError(ERR_BAD_REQUEST, "не указан номер факта (id)") from exc
        # chat_id в условии — не формальность: без него один чат стирал бы
        # память другого, зная лишь номер. Общее знание тоже не стираем из
        # случайного чата: его заводит человек руками, пусть руками и убирает.
        cur = await self._db.conn.execute(
            "DELETE FROM facts WHERE rowid = ? AND chat_id = ? AND scope = ?",
            (fact_id, chat_id, SCOPE_CHAT),
        )
        await self._db.conn.commit()
        return {"forgotten": cur.rowcount > 0, "id": fact_id}

    async def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        limit = min(int(args.get("limit") or MAX_RECALL), MAX_RECALL)
        cur = await self._db.conn.execute(
            "SELECT rowid AS id, text, scope, sensitive, created_at FROM facts "
            "WHERE chat_id = ? OR scope = ? ORDER BY rowid DESC LIMIT ?",
            (chat_id, SCOPE_COMMON, limit),
        )
        facts = [_row_to_fact(row) for row in await cur.fetchall()]
        return {"facts": facts, "count": len(facts)}

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_REMEMBER:
            return await self._remember(args)
        if action == ACTION_RECALL:
            return await self._recall(args)
        if action == ACTION_FORGET:
            return await self._forget(args)
        if action == ACTION_LIST:
            return await self._list(args)
        # Сервер валидирует action по describe — сюда неизвестное не доходит.
        raise ValueError(f"необъявленное действие: {action}")
