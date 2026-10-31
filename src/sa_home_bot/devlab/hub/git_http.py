from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from aiohttp import web

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.http import PRINCIPAL_KEY, STORE_KEY, roles
from sa_home_bot.devlab.hub.queue import PROJECT_NAME_RE

log = logging.getLogger(__name__)

CHUNK_BYTES = 64 * 1024
MAX_CONCURRENT = 4
STDERR_TAIL_BYTES = 4096
HEADER_END = b"\r\n\r\n"
HEADER_END_LF = b"\n\n"
PASSTHROUGH_ENV = ("PATH", "PYTHONPATH", "LANG", "LC_ALL")
FORWARDED_HEADERS = {"Git-Protocol": "GIT_PROTOCOL", "Content-Type": "CONTENT_TYPE"}
LIMIT_KEY = web.AppKey("git_limit", asyncio.Semaphore)


def split_repo_path(tail: str) -> tuple[str, str] | None:
    head, _, rest = tail.partition("/")
    if not head.endswith(protocol.GIT_REPO_SUFFIX):
        return None
    name = head[: -len(protocol.GIT_REPO_SUFFIX)]
    if not PROJECT_NAME_RE.match(name) or ".." in rest.split("/"):
        return None
    return name, rest


def cgi_env(request: web.Request, tail: str) -> dict[str, str]:
    principal = request[PRINCIPAL_KEY]
    env = {key: os.environ[key] for key in PASSTHROUGH_ENV if key in os.environ}
    env.update(
        {
            "GIT_PROJECT_ROOT": str(request.app[STORE_KEY].git_root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "GATEWAY_INTERFACE": "CGI/1.1",
            "REQUEST_METHOD": request.method,
            "QUERY_STRING": request.query_string,
            "PATH_INFO": "/" + tail,
            "REMOTE_ADDR": request.remote or "",
            protocol.GIT_ENV_USER: principal.name,
            protocol.GIT_ENV_ROLE: principal.role,
        }
    )
    for header, variable in FORWARDED_HEADERS.items():
        if header in request.headers:
            env[variable] = request.headers[header]
    return env


def parse_cgi_head(head: bytes) -> tuple[int, dict[str, str]]:
    status = 200
    headers: dict[str, str] = {}
    for line in head.decode(errors="replace").splitlines():
        key, _, value = line.partition(":")
        if key.lower() == "status":
            status = int(value.split()[0])
        elif value:
            headers[key.strip()] = value.strip()
    return status, headers


async def feed_stdin(request: web.Request, proc: asyncio.subprocess.Process) -> None:
    assert proc.stdin is not None
    try:
        async for chunk in request.content.iter_chunked(CHUNK_BYTES):
            proc.stdin.write(chunk)
            await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            proc.stdin.close()


async def collect_stderr(proc: asyncio.subprocess.Process) -> bytes:
    assert proc.stderr is not None
    tail = b""
    while chunk := await proc.stderr.read(CHUNK_BYTES):
        tail = (tail + chunk)[-STDERR_TAIL_BYTES:]
    return tail


async def read_head(stdout: asyncio.StreamReader) -> tuple[bytes, bytes]:
    buffer = b""
    while True:
        for end in (HEADER_END, HEADER_END_LF):
            index = buffer.find(end)
            if index >= 0:
                return buffer[:index], buffer[index + len(end) :]
        chunk = await stdout.read(CHUNK_BYTES)
        if not chunk:
            return buffer, b""
        buffer += chunk


@roles(protocol.ROLE_CLIENT, protocol.ROLE_EXECUTOR)
async def handle_git(request: web.Request) -> web.StreamResponse:
    tail = request.match_info["tail"]
    parsed = split_repo_path(tail)
    if parsed is None or not await request.app[STORE_KEY].queue.has_project(parsed[0]):
        raise web.HTTPNotFound()
    async with request.app[LIMIT_KEY]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "http-backend",
            env=cgi_env(request, tail),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None
        feeder = asyncio.create_task(feed_stdin(request, proc))
        stderr_task = asyncio.create_task(collect_stderr(proc))
        try:
            head, rest = await read_head(proc.stdout)
            if not head:
                await proc.wait()
                log.error(
                    "git http-backend без ответа: %s", (await stderr_task).decode(errors="replace")
                )
                raise web.HTTPBadGateway()
            status, headers = parse_cgi_head(head)
            response = web.StreamResponse(status=status, headers=headers)
            await response.prepare(request)
            if rest:
                await response.write(rest)
            while chunk := await proc.stdout.read(CHUNK_BYTES):
                await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            feeder.cancel()
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            await proc.wait()
            stderr_task.cancel()
            await asyncio.gather(feeder, stderr_task, return_exceptions=True)


def add_routes(app: web.Application) -> None:
    app[LIMIT_KEY] = asyncio.Semaphore(MAX_CONCURRENT)
    app.router.add_route("*", protocol.HTTP_GIT_PREFIX + "{tail:.+}", handle_git)
