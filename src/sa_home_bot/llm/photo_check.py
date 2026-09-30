"""Сверка снимка Альфреда (Этап 49.2.1): что на самом деле вышло на картинке.

Альфред знает, что снимает (это уходит в генератор), но генератор может
промахнуться — обещанной собаки в кадре нет. Здесь gemma со зрением смотрит
на 512-оригинал (не на пиксельную версию, которую видит гость — на 64px
детали не разобрать) и отвечает, что на снимке и чего из обещанного не видно.

Служебный вызов: без персоны, без рассуждения, temperature 0, ответ JSON.
Оригинал сохраняется в ``cfg.photos_dir`` рядом с фото гостей — та же чистка
по TTL (llm/vision.py).
"""

from __future__ import annotations

import base64
import io
import json
import logging
from typing import Any

from PIL import Image

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm import ollama

log = logging.getLogger(__name__)

_JPEG_QUALITY = 90
_OPTIONS: dict[str, Any] = {"temperature": 0, "num_predict": 512}

SYSTEM_PROMPT = """\
Ты внимательно смотришь на картинку и честно говоришь, что на ней видно.
Картинка — стилизованный снимок комнаты или предмета, мелкие детали могут
быть условными. Не додумывай то, чего не видно.

Ответ — строго JSON без пояснений:
{"description": "...", "missing": ["...", ...]}

- description — 2-4 коротких фразы по-русски: что в кадре, где, какого цвета,
  какой свет. Только видимое.
- missing — что из списка «Должно быть в кадре» на картинке НЕ видно или
  нельзя узнать. Пункт считается на месте, если его можно узнать хотя бы
  приблизительно (стилизация, другой ракурс, другой оттенок — не промах).
  Пишешь пункты теми же словами, что в списке. Всё на месте или списка
  нет — пустой список.
"""


def save_original(image: Image.Image, photo_key: str, cfg: LlmConfig) -> str:
    """512²-оригинал → JPEG в ``photos_dir/{photo_key}.jpg``; вернуть base64."""
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=_JPEG_QUALITY)
    data = buf.getvalue()
    cfg.photos_dir.mkdir(parents=True, exist_ok=True)
    (cfg.photos_dir / f"{photo_key}.jpg").write_bytes(data)
    log.info("photo_check: оригинал снимка %s сохранён (%d байт)", photo_key, len(data))
    return base64.b64encode(data).decode()


def build_question(expect: list[str]) -> str:
    if not expect:
        return "Что на картинке? Должно быть в кадре: (списка нет)."
    items = "\n".join(f"- {item}" for item in expect)
    return f"Что на картинке? Должно быть в кадре:\n{items}"


def parse(content: str, expect: list[str]) -> dict[str, Any] | None:
    """JSON модели → {"description", "missing"}; None — ответ не разобрать.
    В ``missing`` остаются только пункты из ``expect`` (модель могла
    перефразировать — сверяем без регистра и по вхождению)."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    raw_missing = data.get("missing")
    if not isinstance(raw_missing, list):
        raw_missing = []
    said = [m.strip().lower() for m in raw_missing if isinstance(m, str) and m.strip()]
    missing = [
        item
        for item in expect
        if any(m == item.lower() or m in item.lower() or item.lower() in m for m in said)
    ]
    return {"description": " ".join(description.split()), "missing": missing}


async def inspect(
    image_b64: str, expect: list[str], cfg: LlmConfig, *, think: Any = None
) -> dict[str, Any] | None:
    """Посмотреть на снимок. None — сверка не удалась (снимок уходит как есть)."""
    try:
        result = await ollama.chat(
            cfg,
            [{"role": "user", "content": build_question(expect), "images": [image_b64]}],
            SYSTEM_PROMPT,
            tools=None,
            think=think,
            response_format="json",
            options=_OPTIONS,
        )
    except Exception:
        log.warning("photo_check: сверка снимка не удалась", exc_info=True)
        return None
    content = result.get("message", {}).get("content", "")
    parsed = parse(content, expect)
    if parsed is None:
        log.warning("photo_check: ответ не разобран: %r", content[:300])
    else:
        log.info("photo_check: %r, промах: %s", parsed["description"], parsed["missing"])
    return parsed
