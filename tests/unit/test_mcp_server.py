"""MCP-сервер над тулами бота (Этап 42.4, bot/mcp_server.py).

Хендлеры (``_handle_list_tools``/``_handle_call_tool``) тестируются
напрямую, с поддельным ``ServerRequestContext`` (только то, что они реально
читают — ``.request.headers``), а не через реальный HTTP/uvicorn: это
проверяет саму логику резолва токена и делегирования в ``bot.tools`` без
хрупкости живого сокета. Резолв токена уходит в ``store.resolve_mcp_token``
— тот же код, что и в проде, только не через сеть.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

import pytest_asyncio

from sa_home_bot.bot.commands import GUESTS, required_right
from sa_home_bot.bot.mcp_server import McpServer
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

OWNER_CHAT = 111
PLAIN_CHAT = 222
GUESTS_RIGHT = required_right(GUESTS.name)  # "invite"


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


@pytest_asyncio.fixture
async def book():
    return SubscriptionBook(
        [
            Subscription(
                name="owner", chat_id=OWNER_CHAT, allowed_commands=frozenset({GUESTS_RIGHT})
            ),
            Subscription(name="plain", chat_id=PLAIN_CHAT, allowed_commands=frozenset()),
        ]
    )


@pytest_asyncio.fixture
async def server(store, book):
    return McpServer(
        settings=Settings(),
        book=book,
        notifier=None,
        store=store,
        node_link=None,
        host="127.0.0.1",
        port=0,
    )


class _FakeRequest:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


class _FakeCtx:
    """Подменяет ровно то, что читают хендлеры из ServerRequestContext —
    ``.request`` (см. mcp.server.runner._make_context)."""

    def __init__(self, request: _FakeRequest | None = None) -> None:
        self.request = request


def _bearer_ctx(token: str) -> _FakeCtx:
    return _FakeCtx(_FakeRequest({"authorization": f"Bearer {token}"}))


def _tool_names(list_result) -> set[str]:
    return {t.name for t in list_result.tools}


async def test_list_tools_without_token_is_fail_closed(server):
    result = await server._handle_list_tools(_FakeCtx(), None)
    names = _tool_names(result)
    assert "calc" in names  # requires=None — виден всем, см. bot/tools.py::TOOLS
    assert "guests_list" not in names  # требует права invite — нет токена, нет подписки


async def test_list_tools_with_owner_token_includes_privileged_tool(server, store):
    token = await store.issue_mcp_token(OWNER_CHAT, datetime.now())
    result = await server._handle_list_tools(_bearer_ctx(token), None)
    assert "guests_list" in _tool_names(result)


async def test_list_tools_with_unprivileged_token_excludes_tool(server, store):
    token = await store.issue_mcp_token(PLAIN_CHAT, datetime.now())
    result = await server._handle_list_tools(_bearer_ctx(token), None)
    assert "guests_list" not in _tool_names(result)


async def test_list_tools_with_unknown_token_is_fail_closed(server):
    result = await server._handle_list_tools(_bearer_ctx("garbage"), None)
    assert "guests_list" not in _tool_names(result)


class _Params:
    def __init__(self, name: str, arguments: dict | None = None) -> None:
        self.name = name
        self.arguments = arguments


async def test_call_tool_calc_without_token_succeeds(server):
    result = await server._handle_call_tool(_FakeCtx(), _Params("calc", {"expression": "1+1"}))
    assert result.is_error is False
    assert result.content[0].text == "2"


async def test_call_tool_privileged_tool_with_owner_token_runs_real_handler(server, store):
    token = await store.issue_mcp_token(OWNER_CHAT, datetime.now())
    result = await server._handle_call_tool(_bearer_ctx(token), _Params("guests_list", {}))
    assert result.is_error is False
    assert "гост" in result.content[0].text.lower()


async def test_call_tool_privileged_tool_without_rights_is_denied(server):
    """Тул недоступен по правам — тот же путь, что и неизвестное модели имя
    (bot.tools.tools_for уже отфильтровала его из toolkit.handlers), не
    тихий проход мимо прав."""
    result = await server._handle_call_tool(_FakeCtx(), _Params("guests_list", {}))
    assert result.is_error is True
    assert "неизвестный инструмент" in result.content[0].text


async def test_call_tool_unknown_name_is_denied(server):
    result = await server._handle_call_tool(_FakeCtx(), _Params("bogus_tool", {}))
    assert result.is_error is True
    assert "bogus_tool" in result.content[0].text


async def test_mcp_token_round_trip(store):
    token = await store.issue_mcp_token(OWNER_CHAT, datetime.now())
    assert await store.resolve_mcp_token(token) == OWNER_CHAT

    revoked = await store.revoke_mcp_token(OWNER_CHAT)
    assert revoked is True
    assert await store.resolve_mcp_token(token) is None

    # Второй revoke — нечего гасить.
    assert await store.revoke_mcp_token(OWNER_CHAT) is False


async def test_mcp_token_reissue_replaces_previous(store):
    first = await store.issue_mcp_token(OWNER_CHAT, datetime.now())
    second = await store.issue_mcp_token(OWNER_CHAT, datetime.now())
    assert first != second
    assert await store.resolve_mcp_token(first) is None
    assert await store.resolve_mcp_token(second) == OWNER_CHAT


async def test_mcp_token_stored_as_hash_not_plaintext(store):
    token = await store.issue_mcp_token(OWNER_CHAT, datetime.now())
    cur = await store.db.conn.execute(
        "SELECT token_hash FROM mcp_tokens WHERE chat_id=?", (OWNER_CHAT,)
    )
    row = await cur.fetchone()
    assert row["token_hash"] != token
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()


async def test_resolve_mcp_token_rejects_empty_string(store):
    assert await store.resolve_mcp_token("") is None
