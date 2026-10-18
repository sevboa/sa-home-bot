"""Тулы картинок для /ai (Этап 48): ``generate_image`` и ``find_image``.

Рисует служба llm на mycraft (llm/imagegen.py, CPU), хранит бот — таблица
``images`` в своей БД (db/schema.sql): «покажи ту картинку» не должно будить
mycraft. Повторный показ идёт по Telegram file_id — без байтов вовсе; байты
из БД нужны, только если Telegram file_id не принял.

Отдельный модуль, а не ещё пара сотен строк в bot/tools.py: обработчики там
тонкие обёртки над ``generate``/``find`` отсюда (как у интерактива радио),
а этот модуль не импортирует bot.tools — иначе был бы цикл. Всё нужное из
ToolContext берётся утиной типизацией (``ctx.chat_id``, ``ctx.notifier``…),
эпизод графа — колбэком ``remember``.
"""

from __future__ import annotations

import base64
import io
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from PIL import Image

from sa_home_bot.proto.addresses import LLM_ADDRESS, LLM_SERVICE
from sa_home_bot.proto.errors import SERVICE_CALL_ERRORS

log = logging.getLogger(__name__)

__all__ = ["LLM_SERVICE"]
ACTION_GENERATE_IMAGE = "generate_image"
ALFRED_LORA = ("alfred", 0.5)

# Сколько вариантов показывать модели, если однозначного совпадения нет.
_CANDIDATES = 5

Remember = Callable[[str], Awaitable[None]]

# «Нарисуй себя» (2026-10-05): Альфред на картинке — LoRA облика и
# короткая примета первой; что он делает и где — из description модели.
ALFRED_IN_PICTURE_EN = "Main subject: Alfred, an elderly butler in a black tailcoat."


GENERATE_IMAGE_DECLARATION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "Нарисовать НОВУЮ картинку по просьбе собеседника («нарисуй», "
            "«сгенерируй картинку», «покажи как выглядел бы…»). Картинка сама "
            "уйдёт собеседнику в чат — не описывай её словами заново и не "
            "вставляй ссылок. Рисуется ~30-40 секунд. Если просят показать "
            "картинку, которую ты УЖЕ рисовал раньше, — это find_image, не этот тул."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": (
                        "Что нарисовать — своими словами, на любом языке, сколько "
                        "нужно: главный объект с цветом и приметами, что рядом, "
                        "где он, освещение и настроение. Промпт для художника-"
                        "нейросети из этого составят сами, пиксельный стиль "
                        "добавится сам. Свет, ракурс и атмосферу («кинематографичный "
                        "свет», «туман», «общий план») писать можно и нужно."
                    ),
                },
                "prompt_ru": {
                    "type": "string",
                    "description": (
                        "Просьба собеседника своими словами по-русски — для поиска потом"
                    ),
                },
                "caption": {
                    "type": "string",
                    "description": "Короткая подпись к картинке по-русски, 2-8 слов",
                },
                "negative_en": {
                    "type": "string",
                    "description": (
                        "Необязательно: чего на картинке быть НЕ должно, по-английски"
                    ),
                },
                "alfred": {
                    "type": "boolean",
                    "description": (
                        "true — на картинке ты сам, Альфред: «нарисуй себя», «а как "
                        "бы ты выглядел на пляже», ты в придуманной сцене. Твою "
                        "внешность подставят сами — в description пиши, что ты "
                        "делаешь, где и какое у тебя лицо."
                    ),
                },
            },
            "required": ["description", "prompt_ru", "caption"],
        },
    },
}

FIND_IMAGE_DECLARATION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "find_image",
        "description": (
            "Найти и заново прислать картинку, которую ты УЖЕ рисовал в этом "
            "чате («покажи того кота», «скинь ещё раз дракона»). Картинка сама "
            "уйдёт в чат. Если точного совпадения нет — вернёт список "
            "последних картинок с номерами: переспроси или вызови ещё раз с image_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Ключевые слова по-русски: что было на картинке",
                },
                "image_id": {
                    "type": "integer",
                    "description": "Номер картинки, если он уже известен",
                },
            },
        },
    },
}


def upscale_png(png: bytes, display_px: int) -> bytes:
    """Растянуть до ~``display_px`` целым множителем без сглаживания —
    пиксели маленькой картинки остаются чёткими квадратами. Делается на
    лету при отправке: храним только маленькую копию."""
    image = Image.open(io.BytesIO(png))
    factor = max(1, display_px // max(image.size))
    if factor > 1:
        image = image.resize(
            (image.width * factor, image.height * factor), Image.Resampling.NEAREST
        )
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _str_arg(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _can_deliver(ctx: Any) -> bool:
    return ctx.notifier is not None and ctx.store is not None and ctx.chat_id is not None


async def _send(ctx: Any, photo: bytes | str, caption: str) -> tuple[int, str] | None:
    return await ctx.notifier.send_photo_ex(
        ctx.chat_id,
        photo,
        caption=caption,
        message_thread_id=ctx.message_thread_id,
        reply_to_message_id=ctx.trigger_message_id,
    )


async def record_generated(
    store: Any,
    result: dict[str, Any],
    *,
    chat_id: int,
    author: str | None,
    prompt_ru: str,
    fallback_prompt_en: str,
    caption: str,
    colors: int,
    now: datetime,
    **stored_options: Any,
) -> tuple[bytes, int]:
    png = base64.b64decode(result["png_b64"])
    image_id = await store.add_image(
        chat_id=chat_id,
        author=author,
        prompt_ru=prompt_ru,
        prompt_en=str(result.get("prompt") or fallback_prompt_en),
        caption=caption,
        width=int(result["width"]),
        height=int(result["height"]),
        colors=colors,
        png=png,
        now=now,
        **stored_options,
    )
    return png, image_id


async def deliver_generated(
    notifier: Any,
    store: Any,
    image_id: int,
    chat_id: int,
    photo: bytes,
    **send_options: Any,
) -> tuple[int, str] | None:
    sent = await notifier.send_photo_ex(chat_id, photo, **send_options)
    if sent is not None:
        await store.set_image_sent(image_id, sent[1], sent[0])
    return sent


async def generate(ctx: Any, args: dict[str, Any], remember: Remember) -> str:
    # prompt_en — имя параметра до v0.117.0: модель может вспомнить его по
    # старым тул-вызовам в истории чата.
    description = _str_arg(args, "description") or _str_arg(args, "prompt_en")
    prompt_ru = _str_arg(args, "prompt_ru")
    caption = _str_arg(args, "caption") or prompt_ru[:60]
    if not description:
        return "ошибка: нет описания картинки (description)"
    if not prompt_ru:
        prompt_ru = caption
    if not _can_deliver(ctx):
        return "недоступно: отсюда не могу прислать картинку в чат"
    if ctx.node_link is None:
        return "ошибка: нет связи с мастерской, где рисуются картинки"
    request: dict[str, Any] = {
        "description": description,
        "negative": _str_arg(args, "negative_en"),
        "chat_id": ctx.chat_id,
    }
    if args.get("alfred") is True or str(args.get("alfred")).lower() == "true":
        request["description"] = f"{ALFRED_IN_PICTURE_EN} {description}"
        request["loras"] = [list(ALFRED_LORA)]
    cfg = ctx.settings.llm
    now = datetime.now(tz=UTC)
    if cfg.imagegen_daily_limit:
        drawn_today = await ctx.store.count_images_since(
            ctx.chat_id, now - timedelta(days=1)
        )
        if drawn_today >= cfg.imagegen_daily_limit:
            return (
                f"отказ: лимит {cfg.imagegen_daily_limit} картинок в сутки для этого "
                "чата исчерпан — скажи собеседнику, что краски кончились до завтра"
            )
    try:
        result = await ctx.node_link.command(
            ACTION_GENERATE_IMAGE,
            request,
            dst=LLM_ADDRESS,
            timeout=cfg.imagegen_request_timeout_s,
        )
    except (*SERVICE_CALL_ERRORS, TimeoutError) as exc:
        return f"не получилось нарисовать: {exc}"
    png, image_id = await record_generated(
        ctx.store,
        result,
        chat_id=ctx.chat_id,
        author=ctx.author,
        prompt_ru=prompt_ru,
        fallback_prompt_en=request["description"],
        caption=caption,
        colors=cfg.imagegen_colors,
        now=now,
    )
    sent = await deliver_generated(
        ctx.notifier,
        ctx.store,
        image_id,
        ctx.chat_id,
        upscale_png(png, cfg.imagegen_display_px),
        caption=caption,
        message_thread_id=ctx.message_thread_id,
        reply_to_message_id=ctx.trigger_message_id,
    )
    if sent is None:
        return f"картинка #{image_id} нарисована, но в чат не ушла — Telegram не принял"
    who = ctx.author or "собеседника"
    await remember(f"Альфред нарисовал для {who} картинку #{image_id}: {caption} ({prompt_ru})")
    return (
        f"картинка #{image_id} «{caption}» уже отправлена собеседнику "
        f"(рисовалась {result.get('seconds', '?')}с). Коротко прокомментируй, "
        "не описывай её заново."
    )


async def find(ctx: Any, args: dict[str, Any]) -> str:
    if not _can_deliver(ctx):
        return "недоступно: отсюда не могу прислать картинку в чат"
    image_id = args.get("image_id")
    query = _str_arg(args, "query")
    chosen: dict | None = None
    if isinstance(image_id, int) and not isinstance(image_id, bool):
        chosen = await ctx.store.get_image(ctx.chat_id, image_id)
        if chosen is None:
            return f"картинки #{image_id} в этом чате нет"
    elif query:
        found = await ctx.store.search_images(ctx.chat_id, query, limit=_CANDIDATES)
        if found:
            chosen = await ctx.store.get_image(ctx.chat_id, found[0]["id"])
    if chosen is None:
        recent = await ctx.store.recent_images(ctx.chat_id, limit=_CANDIDATES)
        if not recent:
            return "в этом чате я ещё ничего не рисовал"
        listing = "; ".join(f"#{r['id']} «{r['caption']}»" for r in recent)
        return f"по запросу ничего не нашлось. Последние картинки: {listing}"

    caption = chosen["caption"]
    sent = None
    if chosen["telegram_file_id"]:
        sent = await _send(ctx, chosen["telegram_file_id"], caption)
    if sent is None:
        # file_id нет или Telegram его больше не принимает — шлём байты из БД
        # и запоминаем новый file_id.
        display_px = ctx.settings.llm.imagegen_display_px
        sent = await _send(ctx, upscale_png(chosen["png"], display_px), caption)
        if sent is None:
            return f"картинку #{chosen['id']} нашёл, но Telegram её не принял"
        await ctx.store.set_image_sent(chosen["id"], sent[1], sent[0])
    return f"картинка #{chosen['id']} «{caption}» отправлена собеседнику ещё раз"
