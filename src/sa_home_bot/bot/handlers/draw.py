"""/draw — отладка режимов генерации (Этап 49, bot/draw_debug.py).

Право — ``draw`` (у владельца открыто по ``*``), команда скрыта из меню.
Кнопки «/draw clean» — свой префикс «draw:», право проверяется здесь же тем
же правилом.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from PIL import Image

from sa_home_bot.bot import commands, draw_debug
from sa_home_bot.bot.draw_debug import DrawCommand, DrawRequest, DrawSyntaxError
from sa_home_bot.bot.image_tools import (
    ACTION_GENERATE_IMAGE,
    LLM_NODE,
    LLM_SERVICE,
    upscale_png,
)
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

router = Router(name="draw")

# Первая сцена с образцом ещё и грузит IP-Adapter (+2.5 ГБ с диска).
_MIN_TIMEOUT_S = 300.0
# Чужое фото-образец ужимаем до родного размера SD1.5 — меньше гнать по сети.
_REF_MAX_PX = 512


def _ref_from_photo(data: bytes) -> bytes:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    image.thumbnail((_REF_MAX_PX, _REF_MAX_PX), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


async def _find_ref(
    message: Message, request: DrawRequest, store: Store
) -> tuple[bytes, str] | str | None:
    """(png, подпись образца), текст ошибки или None (образца нет).

    Порядок: ``ref=ID``, картинка, приложенная к самой команде (команда в
    подписи), картинка, на которую ответили."""
    if request.ref_id is not None:
        row = await store.image_by_id(request.ref_id)
        if row is None:
            return f"картинки #{request.ref_id} в базе нет"
        return row["png"], f"#{row['id']}"
    if message.photo:
        buf = await message.bot.download(message.photo[-1])
        return _ref_from_photo(buf.getvalue()), "приложенное фото"
    replied = message.reply_to_message
    if replied is None or not replied.photo:
        return None
    row = await store.image_by_message(replied.chat.id, replied.message_id)
    if row is not None:
        return row["png"], f"#{row['id']}"
    buf = await message.bot.download(replied.photo[-1])
    return _ref_from_photo(buf.getvalue()), "фото из чата"


def _clean_keyboard() -> InlineKeyboardMarkup:
    prefix = draw_debug.CALLBACK_PREFIX
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Удалить", callback_data=f"{prefix}:{draw_debug.CLEAN_YES}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"{prefix}:{draw_debug.CLEAN_NO}"),
        ]]
    )


async def _service(message: Message, cmd: DrawCommand, store: Store) -> None:
    if cmd.name == "help":
        await message.answer(draw_debug.HELP)
        return
    if cmd.name == "keep":
        row = await store.image_by_id(cmd.image_id or 0)
        if row is None:
            await message.answer(f"Картинки #{cmd.image_id} в базе нет.")
        elif row["purpose"] == "chat":
            await message.answer(f"#{cmd.image_id} — из разговора, её чистка и так не трогает.")
        else:
            await store.set_image_purpose(row["id"], "ref")
            await message.answer(f"#{row['id']} оставлена образцом, «/draw clean» её не тронет.")
        return
    count = await store.count_images_by_purpose("debug")
    if not count:
        await message.answer("Отладочных картинок в базе нет.")
        return
    await message.answer(
        f"Удалить из базы отладочные картинки: {count} шт.? "
        "Сообщения в чате останутся, образцы (keep) — тоже.",
        reply_markup=_clean_keyboard(),
    )


@router.message(Command(commands.DRAW.name))
async def cmd_draw(
    message: Message,
    command: CommandObject,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
) -> None:
    try:
        parsed = draw_debug.parse(command.args)
    except DrawSyntaxError as exc:
        await message.answer(f"⚠️ {exc}\n\nПамятка: /draw help")
        return
    if isinstance(parsed, DrawCommand):
        await _service(message, parsed, store)
        return
    request = parsed

    ref = await _find_ref(message, request, store)
    if isinstance(ref, str):
        await message.answer(f"⚠️ {ref}")
        return
    if ref is not None and not draw_debug.accepts_ref(request):
        await message.answer(
            f"⚠️ образец нужен только в variant и scene, а тут {request.mode}. "
            "Без образца — отправь без картинки и не ответом на неё."
        )
        return
    if ref is None and draw_debug.needs_ref(request):
        await message.answer(
            "⚠️ variant рисуется по образцу: приложи картинку, ответь на неё или добавь ref=ID."
        )
        return

    args = request.service_args()
    args["chat_id"] = message.chat.id
    ref_label = None
    if ref is not None:
        args["ref_png_b64"] = base64.b64encode(ref[0]).decode()
        ref_label = ref[1]
    note = " (контекст при raw не используется)" if request.raw and request.context else ""
    waiting = await message.reply(f"🎨 рисую {request.mode}…{note}")
    cfg = config.llm
    try:
        result = await node_link.command(
            ACTION_GENERATE_IMAGE,
            args,
            dst=Address(node=LLM_NODE, service=LLM_SERVICE),
            timeout=max(cfg.imagegen_request_timeout_s, _MIN_TIMEOUT_S),
        )
    except (ServiceUnavailableError, ProtoError, TimeoutError) as exc:
        await waiting.edit_text(f"⚠️ не нарисовалось: {exc}")
        return

    png = base64.b64decode(result["png_b64"])
    author = message.from_user.full_name if message.from_user else None
    image_id = await store.add_image(
        chat_id=message.chat.id,
        author=author,
        prompt_ru=request.description,
        prompt_en=str(result.get("prompt") or request.description),
        caption=f"отладка {request.mode}",
        width=int(result["width"]),
        height=int(result["height"]),
        colors=int(result.get("colors") or 0),
        png=png,
        now=datetime.now(tz=UTC),
        purpose="debug",
        params=json.dumps(
            {
                **request.params(),
                **({"ref_from": ref_label} if ref_label else {}),
                "seed": result.get("seed"),
                "steps": result.get("steps"),
            },
            ensure_ascii=False,
        ),
    )
    text = draw_debug.caption(image_id, request, result, ref_label)
    try:
        photo = upscale_png(png, cfg.imagegen_display_px)
        sent = await message.reply_photo(
            BufferedInputFile(photo, filename="draw.png"), caption=text
        )
    except TelegramBadRequest as exc:
        await waiting.edit_text(f"⚠️ #{image_id} нарисована, но Telegram не принял: {exc}")
        return
    await store.set_image_sent(image_id, sent.photo[-1].file_id, sent.message_id)
    try:
        await waiting.delete()
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith(f"{draw_debug.CALLBACK_PREFIX}:"))
async def cb_draw(
    callback: CallbackQuery, store: Store, subscription: Subscription | None = None
) -> None:
    message = callback.message
    if (
        subscription is None
        or subscription.broken
        or not subscription.allows_command(commands.required_right(commands.DRAW.name))
    ):
        await callback.answer("⛔️ Недоступно", show_alert=True)
        return
    choice = (callback.data or "").split(":", 1)[1]
    if choice == draw_debug.CLEAN_YES:
        deleted = await store.delete_images_by_purpose("debug")
        text = f"Удалено отладочных картинок: {deleted}."
    else:
        text = "Чистка отменена."
    await callback.answer()
    if message is not None:
        try:
            await message.edit_text(text, reply_markup=None)
        except TelegramBadRequest:
            pass
