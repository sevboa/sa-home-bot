from __future__ import annotations

import socket
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.chat_scope import require_chat_id
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.memory.protocol import (
    ACTION_FORGET,
    ACTION_LIST,
    ACTION_RECALL,
    ACTION_REMEMBER,
    SCOPE_CHAT,
    SCOPE_COMMON,
    SCOPE_FAMILY,
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
from sa_home_bot.text_search import MIN_WORD_CHARS, WORD_RE, fts_or_query
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.utils.text import stem

MAX_FACT_CHARS = 400
DEFAULT_RECALL = 3
MAX_RECALL = 10
SCOPES = (SCOPE_CHAT, SCOPE_FAMILY, SCOPE_COMMON)


def _match_query(text: str) -> str:
    words = [w for w in WORD_RE.findall(text.lower()) if len(w) >= MIN_WORD_CHARS]
    return fts_or_query(stem(w) for w in dict.fromkeys(words))


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
                            title=(
                                "chat (по умолчанию), family — видно чатам из "
                                "family_chat_ids, или common — видно всем"
                            ),
                            choices=(SCOPE_CHAT, SCOPE_FAMILY, SCOPE_COMMON),
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
                    params=(
                        ActionParam(name="chat_id", type="int", title="Чья память"),
                    ),
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
        return require_chat_id(args, "память у каждого чата своя")

    def _is_family_chat(self, chat_id: int) -> bool:
        return chat_id in self._cfg.family_chat_ids

    def _scope(self, args: dict[str, Any], *, chat_id: int, sensitive: bool) -> str:
        scope = str(args.get("scope") or SCOPE_CHAT).strip().lower()
        if scope not in SCOPES:
            raise ProtoError(
                ERR_BAD_REQUEST, f"неизвестный scope: {scope!r} (есть: chat, family, common)"
            )
        if scope == SCOPE_COMMON and sensitive:
            raise ProtoError(
                ERR_BAD_REQUEST,
                "личный факт не может быть общим: sensitive-записи живут только "
                "в том разговоре, где их сказали",
            )
        if scope == SCOPE_FAMILY and not self._is_family_chat(chat_id):
            raise ProtoError(
                ERR_BAD_REQUEST,
                "scope=family доступен только чатам из [memory].family_chat_ids",
            )
        return scope

    async def _remember(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        sensitive = bool(args.get("sensitive"))
        scope = self._scope(args, chat_id=chat_id, sensitive=sensitive)
        text = str(args.get("text") or "").strip()
        if not text:
            raise ProtoError(ERR_BAD_REQUEST, "нечего запоминать (text пуст)")
        if len(text) > MAX_FACT_CHARS:
            raise ProtoError(
                ERR_BAD_REQUEST,
                f"слишком длинно ({len(text)} знаков, максимум {MAX_FACT_CHARS}) — "
                "запоминай одну мысль, а не пересказ разговора",
            )
        now = utc_now().isoformat()
        cur = await self._db.conn.execute(
            "INSERT INTO facts (text, chat_id, scope, sensitive, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (text, chat_id, scope, int(sensitive), now),
        )
        await self._db.conn.commit()
        return {"id": cur.lastrowid, "text": text, "scope": scope, "sensitive": sensitive}

    def _visible_to(self, chat_id: int) -> tuple[str, list[Any]]:
        scopes = [SCOPE_COMMON]
        if self._is_family_chat(chat_id):
            scopes.append(SCOPE_FAMILY)
        clause = "(chat_id = ?" + " OR scope = ?" * len(scopes) + ")"
        return clause, [chat_id, *scopes]

    async def _select_facts(self, tail_sql: str, params: list[Any]) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT rowid AS id, text, scope, sensitive, created_at FROM facts " + tail_sql,
            params,
        )
        facts = [_row_to_fact(row) for row in await cur.fetchall()]
        return {"facts": facts, "count": len(facts)}

    async def _recall(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        limit = min(int(args.get("limit") or DEFAULT_RECALL), MAX_RECALL)
        match = _match_query(str(args.get("query") or ""))
        if not match:
            return {"facts": [], "count": 0}
        visible_sql, visible_params = self._visible_to(chat_id)
        return await self._select_facts(
            f"WHERE facts MATCH ? AND {visible_sql} ORDER BY rank LIMIT ?",
            [match, *visible_params, limit],
        )

    async def _forget(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        try:
            fact_id = int(args.get("id"))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ProtoError(ERR_BAD_REQUEST, "не указан номер факта (id)") from exc
        cur = await self._db.conn.execute(
            "DELETE FROM facts WHERE rowid = ? AND chat_id = ? AND scope = ?",
            (fact_id, chat_id, SCOPE_CHAT),
        )
        await self._db.conn.commit()
        return {"forgotten": cur.rowcount > 0, "id": fact_id}

    async def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(args)
        limit = min(int(args.get("limit") or MAX_RECALL), MAX_RECALL)
        visible_sql, visible_params = self._visible_to(chat_id)
        return await self._select_facts(
            f"WHERE {visible_sql} ORDER BY rowid DESC LIMIT ?", [*visible_params, limit]
        )

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_REMEMBER:
            return await self._remember(args)
        if action == ACTION_RECALL:
            return await self._recall(args)
        if action == ACTION_FORGET:
            return await self._forget(args)
        if action == ACTION_LIST:
            return await self._list(args)
        raise ValueError(f"необъявленное действие: {action}")
