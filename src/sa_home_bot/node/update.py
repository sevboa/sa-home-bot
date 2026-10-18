from __future__ import annotations

import asyncio
import importlib.metadata
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from sa_home_bot.proto.client import DEFAULT_TIMEOUT
from sa_home_bot.utils.version import version_key

log = logging.getLogger(__name__)

PACKAGE_NAME = "sa-home-bot"

_TAG_RE = re.compile(r"v\d+(?:\.\d+)*")

LS_REMOTE_TIMEOUT_S = DEFAULT_TIMEOUT - 3
PIPX_INSTALL_TIMEOUT_S = 300


def installed_version() -> str | None:
    importlib.invalidate_caches()
    try:
        return importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def origin_repo_url() -> str | None:
    try:
        dist = importlib.metadata.distribution(PACKAGE_NAME)
        raw = dist.read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if data.get("dir_info", {}).get("editable"):
        return None
    vcs_info = data.get("vcs_info") or {}
    if vcs_info.get("vcs") != "git":
        return None
    return data.get("url")


def _parse_tags(ls_remote_output: str) -> list[str]:
    tags = []
    for line in ls_remote_output.splitlines():
        _, _, ref = line.partition("refs/tags/")
        if ref and _TAG_RE.fullmatch(ref):
            tags.append(ref)
    return tags


def _latest_tag_sync(repo_url: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", repo_url],
            capture_output=True,
            text=True,
            timeout=LS_REMOTE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("git ls-remote %s не сработал: %s", repo_url, exc)
        return None
    if out.returncode != 0:
        log.warning("git ls-remote %s вернул код %s: %s", repo_url, out.returncode, out.stderr)
        return None
    tags = _parse_tags(out.stdout)
    if not tags:
        return None
    return max(tags, key=lambda t: version_key(t.lstrip("v")))


async def latest_tag(repo_url: str) -> str | None:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _latest_tag_sync, repo_url)


def _pipx_home() -> str:
    return str(Path(sys.executable).parents[3])


_EXTRAS_RE = re.compile(r"\s*" + re.escape(PACKAGE_NAME) + r"\s*\[([^\]]*)\]")


def _installed_extras() -> str | None:
    meta = Path(sys.executable).parents[1] / "pipx_metadata.json"
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        spec = data["main_package"]["package_or_url"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    m = _EXTRAS_RE.match(spec) if isinstance(spec, str) else None
    if not m:
        return None
    extras = ",".join(e.strip() for e in m.group(1).split(",") if e.strip())
    return extras or None


async def pipx_reinstall(repo_url: str, ref: str) -> tuple[bool, str]:
    spec = f"git+{repo_url}@{ref}"
    extras = _installed_extras()
    if extras:
        spec = f"{PACKAGE_NAME}[{extras}] @ {spec}"
    env = {**os.environ, "PIPX_HOME": _pipx_home()}
    try:
        proc = await asyncio.create_subprocess_exec(
            "pipx",
            "install",
            "--force",
            spec,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=PIPX_INSTALL_TIMEOUT_S
        )
    except (OSError, TimeoutError) as exc:
        return False, str(exc)
    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        return False, output[-2000:]
    return True, output[-2000:]


WINDOWS_AUTOUPDATE_TASK_NAME = "sa-home-node-autoupdate"

SCHTASKS_RUN_TIMEOUT_S = 30


async def trigger_scheduled_task(task_name: str = WINDOWS_AUTOUPDATE_TASK_NAME) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "schtasks",
            "/run",
            "/tn",
            task_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=SCHTASKS_RUN_TIMEOUT_S)
    except (OSError, TimeoutError) as exc:
        return False, str(exc)
    output = stdout.decode(errors="replace").strip()
    if proc.returncode != 0:
        return False, output
    return True, output
