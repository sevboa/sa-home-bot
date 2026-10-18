from __future__ import annotations

from typing import Any

from sa_home_bot.bot import tools
from sa_home_bot.config import Settings
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.subscriptions.models import Subscription
from tests.unit.support.fakes import FakeNodeLink as SupportNodeLink

CHAT_ID = 111


def make_ctx(
    store=None,
    settings=None,
    chat_id=CHAT_ID,
    *,
    dialogue_id=1,
    trigger_message_id=1,
    node_link=None,
    history=None,
    subscription=None,
    dismissal=None,
    notifier=None,
    woken_by=None,
    message_thread_id=None,
):
    return tools.ToolContext(
        chat_id=chat_id,
        dialogue_id=dialogue_id,
        trigger_message_id=trigger_message_id,
        settings=settings or Settings(),
        node_link=node_link,
        history=history if history is not None else [],
        subscription=subscription,
        dismissal=dismissal,
        notifier=notifier,
        store=store,
        woken_by=woken_by,
        message_thread_id=message_thread_id,
    )


def make_sub(*allowed: str) -> Subscription:
    return Subscription(chat_id=CHAT_ID, name="me", allowed_commands=frozenset(allowed))


ADMIN = make_sub("*")


def tool_names(subscription) -> list[str]:
    return [d["function"]["name"] for d in tools.tools_for(subscription).declarations]


def declared_enum(tool: str, param: str, subscription) -> list[Any]:
    declaration = next(
        (d for d in tools.tools_for(subscription).declarations if d["function"]["name"] == tool),
        None,
    )
    if declaration is None:
        return []
    return declaration["function"]["parameters"]["properties"][param]["enum"]


class FakeNodeLink(SupportNodeLink):
    def __init__(self, *, raises: Exception | None = None) -> None:
        super().__init__(result={"task_id": 1}, error=raises)


OWN_STATE = {
    "node": "alfred",
    "kind": "server",
    "version": "0.38.1",
    "system_uptime_s": 7200,
    "services": [
        {"name": "monitor", "status": "running"},
        {"name": "torrents", "status": "running"},
    ],
    "peers": [{"id": "winpc", "alive": False, "kind": "workstation"}],
}

ALFRED_MONITOR = {
    "health": [
        {
            "component_id": "cpu:pkg",
            "kind": "cpu",
            "label": "CPU",
            "status": "ok",
            "temperature_c": 37.0,
        }
    ],
    "disks": [
        {"label": "eMMC", "kind": "emmc", "health": None, "free_bytes": 51000000000},
    ],
    "requirements": [],
}


class FakeSwarmLink:
    def __init__(self, states=None, command_result=None, command_raises=None):
        self._states = states or {}
        self._command_result = command_result if command_result is not None else {"torrents": []}
        self._command_raises = command_raises
        self.commands: list[tuple[str, object]] = []
        self.sent_args: list[dict] = []
        self.timeouts: list[float | None] = []

    async def get_state(self, dst=None):
        key = "own" if dst is None else f"{dst.node}:{dst.service}"
        if key in self._states:
            return self._states[key]
        raise ServiceUnavailableError("нет связи")

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.commands.append((action, dst))
        self.sent_args.append(args or {})
        self.timeouts.append(timeout)
        if self._command_raises is not None:
            raise self._command_raises
        return self._command_result


def swarm_link(**kwargs):
    states = {"own": OWN_STATE, "alfred:monitor": ALFRED_MONITOR}
    states.update(kwargs.pop("states", {}))
    return FakeSwarmLink(states=states, **kwargs)
