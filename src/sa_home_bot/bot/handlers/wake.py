"""/wake — разбудить домашний ПК по Wake-on-LAN."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from sa_home_bot import wol
from sa_home_bot.bot import commands
from sa_home_bot.config import Settings

router = Router(name="wake")
log = logging.getLogger(__name__)

NOT_CONFIGURED_TEXT = (
    "⚙️ Wake-on-LAN не настроен: задайте mac в секции [wake] файла config.toml."
)


@router.message(Command(commands.WAKE.name))
async def cmd_wake(message: Message, config: Settings) -> None:
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

    # Хендлеры aiogram выполняются конкурентно, ожидание не блокирует polling.
    elapsed = await wol.wait_host_up(wake.ip, wake.wait_timeout_s)
    if elapsed is not None:
        await sent.reply(f"✅ Машина проснулась: ping через {elapsed:.0f} с.")
    else:
        await sent.reply(
            f"⚠️ Машина не ответила на ping за {wake.wait_timeout_s:.0f} с. "
            "Проверьте, что WoL включён в BIOS и в настройках сетевой карты Windows."
        )
