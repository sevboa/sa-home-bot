"""Художник-промптер (Этап 48): свободное описание картинки от Альфреда —
любой длины, на любом языке — в короткий точный промпт под SD1.5.

Живая находка 2026-09-29: когда gemma-персонаж сам писал ``prompt_en``, он
раз за разом дописывал «cinematic lighting», «hyperrealism», «oil painting
style» вопреки описанию тула — и эти слова спорили со стилевым шаблоном
(``imagegen_prompt_template``), утягивая картинку в реализм. Персонажу не
место думать о CLIP-токенах: он описывает сцену как хочет, а отдельный
служебный вызов той же gemma (свой system, JSON, без рассуждения — как
Ведущий интерактивов) выжимает из описания суть.

Два предохранителя в коде поверх модели — на случай, если она всё же
ослушается: ``strip_style_tags`` выкидывает стилевые/«качественные» теги, а
``fit_prompt`` срезает хвостовые теги, пока промпт вместе со стилевым
шаблоном не влезет в 77 токенов CLIP (иначе CLIP молча отрезал бы именно
стиль — он в шаблоне после сути).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm import ollama

log = logging.getLogger(__name__)

# CLIP SD1.5: 77 позиций, две из них — служебные BOS/EOS.
CLIP_MAX_TOKENS = 75

SYSTEM_PROMPT = """\
You write prompts for a small Stable Diffusion 1.5 image model.
Input: a description of a picture (any language, any length, may be chatty).
Output: JSON {"prompt": "...", "negative": "..."} and nothing else.

"prompt" rules:
- English only, comma-separated short phrases, 8-25 words total.
- Order: main subject first (with colors and 1-3 distinctive features),
  then secondary objects, then place/background, then light color and mood.
- Concrete visible things only. Drop story, dialogue, emotions of the viewer,
  sounds, smells, anything the picture cannot show.
- No complex poses or actions (waving, jumping, holding hands): keep a simple
  state (standing, sitting, lying, flying).
- No text, letters, logos, numbers on the picture.
- At most 2-3 separate figures; a crowd becomes "a crowd".
- Rare animals or creatures: add recognizable features
  (alpaca -> "alpaca, long neck, woolly llama").
- If the description asks for one item/object, keep it as the only subject:
  "single <item>, centered, plain background".
- NEVER add style, medium, quality or camera words: no "cinematic", "realistic",
  "photo", "hyperrealism", "painting", "oil", "art style", "render", "8k",
  "detailed", "masterpiece", "high quality", "epic", "lens", "bokeh",
  artist names. Style is added automatically later. (A painting or photo that
  is itself an object in the scene, like "a painting of poppies in a gold frame
  on a wall", is fine.)

"negative": English, 0-6 short phrases of things that must NOT appear,
only when the description clearly says so; otherwise "".
"""

# Стиль, материал, «качество», камера — всё, что спорит со стилевым шаблоном.
# Тег (кусок между запятыми) с таким словом выкидывается целиком.
_STYLE_TAG = re.compile(
    r"\b("
    r"cinematic\w*|realis\w*|hyperreal\w*|photo\w*|photograph\w*|"
    r"oil paint\w*|watercolou?r|impressionis\w*|"
    r"render\w*|octane|unreal engine|3d render|cgi|"
    r"\d+k|uhd|hdr|high(ly)? detailed|ultra detailed|detailed \w+ texture|"
    r"masterpiece|best quality|high quality|high resolution|"
    r"trending|artstation|art style|in the style of|art by|"
    r"epic scale|bokeh|depth of field|dslr|sharp focus|studio lighting"
    r")\b",
    re.IGNORECASE,
)


def split_tags(prompt: str) -> list[str]:
    return [tag.strip() for tag in prompt.replace("\n", ",").split(",") if tag.strip()]


def strip_style_tags(prompt: str) -> str:
    """Выкинуть теги со стилевыми и «качественными» словами. Если выкинулось
    всё (промпт был из одного стиля) — вернуть исходный: пусть лучше рисует
    хоть что-то по сути, чем пустоту."""
    tags = split_tags(prompt)
    kept = [tag for tag in tags if not _STYLE_TAG.search(tag)]
    return ", ".join(kept) if kept else ", ".join(tags)


def fit_prompt(
    prompt: str, template: str, count_tokens: Callable[[str], int], limit: int = CLIP_MAX_TOKENS
) -> str:
    """Срезать хвостовые теги, пока ``template`` с подставленным промптом не
    влезет в ``limit`` токенов. Первый тег (главный объект) не срезается
    никогда."""
    template = template.strip() or "{prompt}"
    tags = split_tags(prompt)

    def full(parts: list[str]) -> str:
        body = ", ".join(parts)
        if "{prompt}" in template:
            return template.replace("{prompt}", body)
        return f"{template}, {body}"

    while len(tags) > 1 and count_tokens(full(tags)) > limit:
        tags.pop()
    return ", ".join(tags)


def _parse(content: str) -> tuple[str, str]:
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("ответ не объект")
    prompt = data.get("prompt")
    negative = data.get("negative") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("пустой prompt")
    if not isinstance(negative, str):
        negative = ""
    return prompt.strip(), negative.strip()


async def compose(description: str, cfg: LlmConfig, think: Any = None) -> tuple[str, str]:
    """Описание → (prompt_en, negative_en). При любой ошибке модели — описание
    как есть (после чистки стилевых тегов): картинка важнее идеального промпта."""
    try:
        result = await ollama.chat(
            cfg,
            [{"role": "user", "content": description}],
            SYSTEM_PROMPT,
            tools=None,
            think=think,
            response_format="json",
        )
        prompt, negative = _parse(result.get("message", {}).get("content", ""))
    except Exception:
        log.warning("image_prompt: промптер не справился, беру описание как есть", exc_info=True)
        return strip_style_tags(description), ""
    return strip_style_tags(prompt), negative
