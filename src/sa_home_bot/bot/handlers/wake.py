"""/wake и кнопки «Разбудить ПК»/«Разбудить <нода>» — Wake-on-LAN.

Два пути (этап 19 п.6, IMPLEMENTATION_PLAN.md):
- ручной (запасной, совместимость) — фиксированная машина из [wake] конфига,
  magic packet шлёт сам бот; годится, только если бот крутится в той же LAN,
  что и цель;
- через рой — нода сама знает MAC/IP/broadcast своего Ethernet-интерфейса
  (node/service.py:get_state()["wake"]), бот кэширует их, пока нода жива
  (bot/wake_state.py), а когда та уснула — просит отправить magic packet
  живую ноду из того же сегмента LAN (swarm_view.find_lan_waker). Так сигнал
  уходит в правильную подсеть, даже если сам бот — на удалённой машине
  (например, ходит к ноде через tailscale).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from sa_home_bot import wol
from sa_home_bot.bot import commands, swarm_view
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.wake_state import cached as cached_wake_info
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import Address, ProtoError

router = Router(name="wake")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WakeOutcome:
    """Результат `wake_swarm_node_core` — ``detail`` уже готовый HTML-текст
    для чата (с эмодзи, как в исходных сообщениях `/wake`), чтобы обёртка
    `_wake_swarm_node` могла просто переслать его без изменения поведения."""

    ok: bool
    detail: str

NOT_CONFIGURED_TEXT = (
    "⚙️ Wake-on-LAN не настроен: задайте mac в секции [wake] файла config.toml, "
    "либо дождитесь, пока нужная нода роя хотя бы раз появится в сети."
)

WAKE_CALLBACK_PREFIX = f"{commands.CALLBACK_PREFIX}:{commands.WAKE_CODE}"


async def _wake_manual(message: Message, config: Settings) -> None:
    """Ручной путь: фиксированная машина из [wake], magic packet шлёт бот."""
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


async def wake_swarm_node_core(
    node_link: ServiceLink, store: Store, node_id: str
) -> WakeOutcome:
    """Ядро пути через рой, без побочных сообщений в чат: будим известную
    ноду по её кэшированным реквизитам — отправляет не бот, а живая нода из
    той же LAN (см. докстринг модуля). Переиспользуется и `_wake_swarm_node`
    (кнопка/команда `/wake`, шлёт `.detail` пользователю как есть), и
    `bot/ai_flow.py` (молчаливый wake перед `/ai`, `.detail` не показывается)."""
    info = await cached_wake_info(store, node_id)
    if info is None:
        return WakeOutcome(
            False, f"⚙️ Нет данных о MAC «{node_id}» — нода ещё ни разу не была видна в рое."
        )

    waker = await swarm_view.find_lan_waker(node_link, store, node_id, info["broadcast"])
    if waker is None:
        return WakeOutcome(
            False, f"⚠️ Некому отправить сигнал: нет живой ноды в той же сети, что «{node_id}»."
        )

    dst = Address(node=waker, service="node")
    try:
        await node_link.command("send_wol", {"mac": info["mac"]}, dst=dst)
    except ServiceUnavailableError:
        return WakeOutcome(False, f"⚠️ Нода «{waker}» перестала отвечать во время отправки.")
    except ProtoError as exc:
        return WakeOutcome(False, f"❌ {waker}: {exc.message}")

    return WakeOutcome(
        True,
        f"🔌 Magic packet для «{node_id}» (<code>{info['mac']}</code>) отправлен через "
        f"ноду «{waker}». Появится в /nodes, как поднимется.",
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
    # Право (команда wake) уже проверено CallbackAuthorizationMiddleware.
    if callback.message is None:
        await callback.answer()
        return
    node_id = commands.parse_wake_callback(callback.data)
    await callback.answer()
    if node_id is None:
        await _wake_manual(callback.message, config)
    else:
        await _wake_swarm_node(callback.message, node_link, store, node_id)
