"""Художник-промптер (Этап 48): свободное описание картинки от Альфреда —
любой длины, на любом языке — в короткий точный промпт под SD1.5.

Живая находка 2026-09-29: когда gemma-персонаж сам писал ``prompt_en``, он
спотыкался о CLIP-токены и терял суть. Персонажу не место думать о CLIP:
он описывает сцену как хочет, а отдельный служебный вызов той же gemma
(свой system, JSON, без рассуждения — как Ведущий интерактивов) выжимает из
описания промпт.

Свет, камера и «качество» («cinematic lighting», «highly detailed», «wide
shot») в промпте разрешены (2026-09-30, по решению владельца: сцены с ними
выходят заметно лучше — ср. «Замок Дракулы», #25). Пиксельность держит
стилевой шаблон и постобработка, от реализма — стилевой негатив.

Предохранитель в коде поверх модели — ``fit_prompt``: срезает хвостовые
теги, пока промпт вместе со стилевым шаблоном не влезет в 77 токенов CLIP
(иначе CLIP молча отрезал бы именно стиль — он в шаблоне после сути).
"""

from __future__ import annotations

import json
import logging
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
- English only, comma-separated short phrases, 10-30 words total.
- Order: main subject first (with colors and 1-3 distinctive features),
  then secondary objects, then place/background, then light and mood,
  then 1-4 camera/quality words last (e.g. "cinematic lighting",
  "dramatic lighting", "misty atmosphere", "wide shot", "highly detailed").
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
- No artist names.

"negative": English, 0-6 short phrases of things that must NOT appear,
only when the description clearly says so; otherwise "".
"""

# Этап 49, отладочный /draw: что это за картинка — подсказка промптеру
# перед описанием. Альфред в кадре — пожилой дворецкий: сама модель
# «Альфреда» не знает.
MODE_HINTS = {
    "free": "",
    "item": (
        "This is a picture of ONE item alone. Keep it the only subject: "
        "single <item>, centered, plain white background."
    ),
    "variant": (
        "This is ONE item redrawn from a reference picture with the listed traits. "
        "Name the item, then its distinctive traits; single object, plain white background."
    ),
    "scene": (
        "This is what Alfred (an elderly butler in a black tailcoat) sees and "
        "photographs right now. Main thing first, 8-15 words, the place in 2-3 words."
    ),
}


# Пересъёмка после промаха (Этап 49.2.1). Весов «(x:1.3)» пайплайн не
# понимает (без compel это просто текст), поэтому упор — порядком: первый
# тег fit_prompt не срезает никогда, и ключевое слово ещё раз ближе к концу.
EMPHASIZE_HINT = (
    "The previous picture FAILED to show: {items}. Now it must be the main "
    "subject: put it as the very first tag, large and clearly visible, and "
    "repeat its key noun once more later in the prompt."
)


def build_request(
    description: str,
    mode: str = "free",
    context: str = "",
    emphasize: list[str] | None = None,
) -> str:
    """Текст для промптера: подсказка режима, контекст сцены от режиссёра,
    упор пересъёмки, потом само описание. Для «free» без контекста и упора —
    описание как есть."""
    parts = []
    hint = MODE_HINTS.get(mode, "")
    if hint:
        parts.append(hint)
    if context.strip():
        parts.append(f"Scene context (use only what is visible now): {context.strip()}")
    if emphasize:
        parts.append(EMPHASIZE_HINT.format(items="; ".join(emphasize)))
    if not parts:
        return description
    parts.append(f"Picture: {description}")
    return "\n\n".join(parts)


def split_tags(prompt: str) -> list[str]:
    return [tag.strip() for tag in prompt.replace("\n", ",").split(",") if tag.strip()]


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
    как есть: картинка важнее идеального промпта."""
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
        return description, ""
    return prompt, negative
