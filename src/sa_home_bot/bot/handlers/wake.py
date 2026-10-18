from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from sa_home_bot import wol
from sa_home_bot.bot import commands
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.ui.callback_data import pack
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.wake_core import wake_swarm_node_core

router = Router(name="wake")
log = logging.getLogger(__name__)

NOT_CONFIGURED_TEXT = (
    "⚙️ Wake-on-LAN не настроен: задайте mac в секции [wake] файла config.toml, "
    "либо дождитесь, пока нужная нода роя хотя бы раз появится в сети."
)

WAKE_CALLBACK_PREFIX = pack(commands.CALLBACK_PREFIX, commands.WAKE_CODE)


async def _wake_manual(message: Message, config: Settings) -> None:
    wake = config.wake
    if not wake.mac:
        await message.answer(NOT_CONFIGURED_TEXT)
        return

    if wake.ip and await wol.ping_host(wake.ip):
        await message.answer(f"💡 Машина уже в сети ({wake.ip} отвечает на ping).")
        return

    try:
        mac = wol.normalize_mac(wake.mac)
        wol.send_magic_packet(mac, wake.broadcast, wake.port)
    except (ValueError, OSError) as exc:
        log.warning("WoL: не удалось отправить magic packet: %s", exc)
        await message.answer(f"❌ Не удалось отправить magic packet: {exc}")
        return

    sent = await message.answer(f"🔌 Magic packet отправлен на <code>{mac}</code>…")
    if not wake.ip:
        return

    elapsed = await wol.wait_host_up(wake.ip, wake.wait_timeout_s)
    if elapsed is not None:
        await sent.reply(f"✅ Машина проснулась: ping через {elapsed:.0f} с.")
    else:
        await sent.reply(
            f"⚠️ Машина не ответила на ping за {wake.wait_timeout_s:.0f} с. "
            "Проверьте, что WoL включён в BIOS и в настройках сетевой карты Windows."
        )


async def _wake_swarm_node(
    message: Message, node_link: ServiceLink, store: Store, node_id: str
) -> None:
    outcome = await wake_swarm_node_core(node_link, store, node_id)
    await message.answer(outcome.detail)


@router.message(Command(commands.WAKE.name))
async def cmd_wake(message: Message, config: Settings) -> None:
    await _wake_manual(message, config)


@router.callback_query(F.data.startswith(WAKE_CALLBACK_PREFIX))
async def on_wake_button(
    callback: CallbackQuery, config: Settings, node_link: ServiceLink, store: Store
) -> None:
    if callback.message is None:
        await callback.answer()
        return
    node_id = commands.parse_wake_callback(callback.data)
    await callback.answer()
    if node_id is None:
        await _wake_manual(callback.message, config)
    else:
        await _wake_swarm_node(callback.message, node_link, store, node_id)
