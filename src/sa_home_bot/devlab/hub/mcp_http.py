from __future__ import annotations

import json
from typing import Any

from aiohttp import web

from sa_home_bot import __version__
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import mcp_tools
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.http import PRINCIPAL_KEY, STORE_KEY, roles

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INSTRUCTIONS = f"Как пользоваться мастерской: ресурс {protocol.RESOURCE_USAGE}."


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def reply(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": protocol.MCP_JSONRPC, "id": request_id, "result": result}


def failure(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": protocol.MCP_JSONRPC,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def initialize(params: dict[str, Any]) -> dict[str, Any]:
    asked = params.get("protocolVersion")
    version = (
        asked if asked in protocol.MCP_PROTOCOL_VERSIONS else protocol.MCP_PROTOCOL_VERSIONS[0]
    )
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {}, "resources": {}},
        "serverInfo": {"name": protocol.MCP_SERVER_NAME, "version": __version__},
        "instructions": INSTRUCTIONS,
    }


def tools_list() -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.schema(),
            }
            for tool in mcp_tools.TOOLS
        ]
    }


def text_result(text: str, is_error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


async def tools_call(ctx: mcp_tools.Context, params: dict[str, Any]) -> dict[str, Any]:
    tool = mcp_tools.TOOLS_BY_NAME.get(params.get("name", ""))
    if tool is None:
        raise RpcError(INVALID_PARAMS, f"нет инструмента {params.get('name')!r}")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "arguments — объект")
    try:
        result = await tool.handler(ctx, **tool.check(arguments))
    except HubError as exc:
        return text_result(str(exc), True)
    return text_result(json.dumps(result, ensure_ascii=False, indent=2), False)


def resources_list() -> dict[str, Any]:
    return {
        "resources": [
            {
                "uri": spec.uri,
                "name": spec.name,
                "description": spec.description,
                "mimeType": mcp_tools.MARKDOWN_MIME,
            }
            for spec in mcp_tools.RESOURCES
        ]
    }


def resources_read(params: dict[str, Any]) -> dict[str, Any]:
    spec = mcp_tools.RESOURCES_BY_URI.get(params.get("uri", ""))
    if spec is None:
        raise RpcError(INVALID_PARAMS, f"нет ресурса {params.get('uri')!r}")
    return {
        "contents": [
            {
                "uri": spec.uri,
                "mimeType": mcp_tools.MARKDOWN_MIME,
                "text": mcp_tools.read_resource(spec),
            }
        ]
    }


async def dispatch(ctx: mcp_tools.Context, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == protocol.MCP_METHOD_INITIALIZE:
        return initialize(params)
    if method == protocol.MCP_METHOD_PING:
        return {}
    if method == protocol.MCP_METHOD_TOOLS_LIST:
        return tools_list()
    if method == protocol.MCP_METHOD_TOOLS_CALL:
        return await tools_call(ctx, params)
    if method == protocol.MCP_METHOD_RESOURCES_LIST:
        return resources_list()
    if method == protocol.MCP_METHOD_RESOURCES_READ:
        return resources_read(params)
    raise RpcError(METHOD_NOT_FOUND, f"метод {method!r} не поддерживается")


@roles(protocol.ROLE_CLIENT)
async def mcp_post(request: web.Request) -> web.Response:
    try:
        body = json.loads(await request.read())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response(failure(None, PARSE_ERROR, "тело — не JSON"), status=400)
    if not isinstance(body, dict) or not isinstance(body.get("method"), str):
        return web.json_response(
            failure(None, INVALID_REQUEST, "ожидается один JSON-RPC запрос"), status=400
        )
    if "id" not in body:
        return web.Response(status=202)
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return web.json_response(failure(body["id"], INVALID_PARAMS, "params — объект"))
    ctx = mcp_tools.Context(request.app[STORE_KEY], request[PRINCIPAL_KEY].name)
    try:
        return web.json_response(reply(body["id"], await dispatch(ctx, body["method"], params)))
    except RpcError as exc:
        return web.json_response(failure(body["id"], exc.code, str(exc)))


def add_routes(app: web.Application) -> None:
    app.router.add_post(protocol.HTTP_MCP_PATH, mcp_post)
