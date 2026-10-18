from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from sa_home_bot.bot import commands, node_links, node_view
from sa_home_bot.bot.middlewares import DENIED_TEXT, extract_command
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.proto.addresses import node_address
from sa_home_bot.proto.errors import SERVICE_CALL_ERRORS
from sa_home_bot.subscriptions.models import Subscription

router = Router(name="node_links")

NOT_FOUND_TEXT = "Не нашёл такую ноду или службу — рой мог измениться, см. /swarm."


async def _known_nodes(node_link: ServiceLink) -> tuple[str | None, list[str]]:
    try:
        state = await node_link.get_state()
    except SERVICE_CALL_ERRORS:
        return None, []
    own = state.get("node", "")
    peers = [p.get("id", "") for p in state.get("peers", [])]
    return own, [own, *[p for p in peers if p]]


async def _authorized_command(
    message: Message,
    node_link: ServiceLink,
    subscription: Subscription | None,
    required_command: str,
    prefix: str,
) -> tuple[str, str, list[str]] | None:
    command = extract_command(message.text)
    if command is None:
        return None
    if subscription is None or not subscription.allows_command(required_command):
        await message.answer(DENIED_TEXT)
        return None
    own, known = await _known_nodes(node_link)
    if own is None:
        await message.answer(node_view.NODE_DOWN_TEXT)
        return None
    return command[len(prefix) :], own, known


@router.message(F.text.startswith(f"/{node_links.NODE_PREFIX}"))
async def cmd_node_card(
    message: Message,
    node_link: ServiceLink,
    subscription: Subscription | None = None,
) -> None:
    parsed = await _authorized_command(
        message, node_link, subscription, commands.STATUS.name, node_links.NODE_PREFIX
    )
    if parsed is None:
        return
    arg, own, known = parsed
    target = node_links.match_name(arg, known)
    if target is None:
        await message.answer(NOT_FOUND_TEXT)
        return
    node_id = None if target == own else target
    text, keyboard = await node_view.build_node_card_view(node_link, subscription, node_id)
    await message.answer(text, reply_markup=keyboard)


@router.message(F.text.startswith(f"/{node_links.SVC_PREFIX}"))
async def cmd_service_card(
    message: Message,
    node_link: ServiceLink,
    subscription: Subscription | None = None,
) -> None:
    parsed = await _authorized_command(
        message, node_link, subscription, commands.NODES.name, node_links.SVC_PREFIX
    )
    if parsed is None:
        return
    arg, own, known = parsed
    for target, tail in node_links.resolve_svc_candidates(arg, known):
        node_id = None if target == own else target
        try:
            state = await node_link.get_state(dst=node_address(node_id))
        except SERVICE_CALL_ERRORS:
            continue
        names = [s.get("name", "") for s in state.get("services", [])]
        service_name = node_links.match_name(tail, names)
        if service_name is None:
            continue
        text, keyboard = await node_view.build_service_card_view(
            node_link, subscription, service_name, node_id
        )
        await message.answer(text, reply_markup=keyboard)
        return
    await message.answer(NOT_FOUND_TEXT)
