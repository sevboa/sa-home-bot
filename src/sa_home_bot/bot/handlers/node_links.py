"""Динамические ссылки-команды /node_<id> и /svc_<нода>_<служба>.

Таких команд нет в реестре — AuthorizationMiddleware их пропускает, права
проверяются здесь (тот же паттерн, что у скилов apps): /node_* — право
`status` (как карточка ноды), /svc_* — `nodes` (как карточка службы).
Роутер включается ДО apps: наши префиксы фиксированы, чужие команды не
трогаем, но неразрешимую ссылку с нашим префиксом честно объявляем — это
наша сгенерированная ссылка, молчать как apps нельзя.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

from sa_home_bot.bot import commands, node_links, node_view
from sa_home_bot.bot.middlewares import DENIED_TEXT, extract_command
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

router = Router(name="node_links")

NOT_FOUND_TEXT = "Не нашёл такую ноду или службу — рой мог измениться, см. /swarm."


async def _known_nodes(node_link: ServiceLink) -> tuple[str | None, list[str]]:
    """(id своей ноды, все известные id) из get_state своей ноды."""
    try:
        state = await node_link.get_state()
    except (ServiceUnavailableError, ProtoError):
        return None, []
    own = state.get("node", "")
    peers = [p.get("id", "") for p in state.get("peers", [])]
    return own, [own, *[p for p in peers if p]]


@router.message(F.text.startswith(f"/{node_links.NODE_PREFIX}"))
async def cmd_node_card(
    message: Message,
    node_link: ServiceLink,
    subscription: Subscription | None = None,
) -> None:
    command = extract_command(message.text)
    if command is None:
        return
    if subscription is None or not subscription.allows_command(commands.STATUS.name):
        await message.answer(DENIED_TEXT)
        return
    own, known = await _known_nodes(node_link)
    if own is None:
        await message.answer(node_view.NODE_DOWN_TEXT)
        return
    target = node_links.resolve_node(command[len(node_links.NODE_PREFIX) :], known)
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
    command = extract_command(message.text)
    if command is None:
        return
    if subscription is None or not subscription.allows_command(commands.NODES.name):
        await message.answer(DENIED_TEXT)
        return
    own, known = await _known_nodes(node_link)
    if own is None:
        await message.answer(node_view.NODE_DOWN_TEXT)
        return
    arg = command[len(node_links.SVC_PREFIX) :]
    for target, tail in node_links.resolve_svc_candidates(arg, known):
        node_id = None if target == own else target
        dst = Address(node=node_id, service=node_view.NODE_SERVICE) if node_id else None
        try:
            state = await node_link.get_state(dst=dst)
        except (ServiceUnavailableError, ProtoError):
            continue  # спящий кандидат не мешает более коротким префиксам
        names = [s.get("name", "") for s in state.get("services", [])]
        service_name = node_links.match_service(tail, names)
        if service_name is None:
            continue
        text, keyboard = await node_view.build_service_card_view(
            node_link, subscription, service_name, node_id
        )
        await message.answer(text, reply_markup=keyboard)
        return
    await message.answer(NOT_FOUND_TEXT)
