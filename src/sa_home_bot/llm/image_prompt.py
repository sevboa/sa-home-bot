from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm import ollama

log = logging.getLogger(__name__)

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
- BUT a character's facial expression and emotion ARE visible: keep them as
  1-3 words right after the subject ("terrified face, screaming", "angry scowl").
- Keep the viewpoint when given: "side view", "seen from behind".
- No complex poses or actions (waving, jumping, holding hands): keep a simple
  state (standing, sitting, lying, flying).
- No text, letters, logos, numbers on the picture.
- At most 2-3 separate figures; a crowd becomes "a crowd".
- Rare animals or creatures: add recognizable features
  (alpaca -> "alpaca, long neck, woolly llama").
- A home two-way radio set (радиостанция, трансивер, рация, передатчик,
  radio transmitter) is "vintage ham radio transceiver"; never write
  "radio station" or "transmitter" alone — they draw a broadcast building
  or an antenna tower.
- If the description asks for one item/object, keep it as the only subject:
  "single <item>, centered, plain background".
- No artist names.

"negative": English, 0-6 short phrases of things that must NOT appear,
only when the description clearly says so; otherwise "".
"""

HANDS_NEGATIVE = "bad hands, extra fingers, mutated hands, fused fingers"

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
        "photographs right now. Main thing first, 8-15 words, the place in 2-3 words. "
        "He is indoors: never write castle, tower, palace or fortress — they draw "
        "a building from outside; say room, study, hall instead."
    ),
}


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


def put_second(prompt: str, extra: str, *, move: bool = False) -> str:
    tags = split_tags(prompt)
    if move:
        want = {tag.lower() for tag in split_tags(extra)}
        tags = [tags[0], *(tag for tag in tags[1:] if tag.lower() not in want)] if tags else tags
    have = {tag.lower() for tag in tags}
    new = [tag for tag in split_tags(extra) if tag.lower() not in have]
    return ", ".join([*tags[:1], *new, *tags[1:]])


def weight_tags(prompt: str, first: float, last: float = 1.0) -> str:
    tags = [t.replace("(", "").replace(")", "") for t in split_tags(prompt)]
    if not tags:
        return prompt
    if first != 1.0:
        tags[0] = f"({tags[0]}){first:g}"
    if last != 1.0 and len(tags) > 2:
        tags[-1] = f"({tags[-1]}){last:g}"
    return ", ".join(tags)


def fit_prompt(
    prompt: str, template: str, count_tokens: Callable[[str], int], limit: int = CLIP_MAX_TOKENS
) -> str:
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
    data = ollama.parse_json_object(content)
    prompt = data.get("prompt")
    negative = data.get("negative") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("пустой prompt")
    if not isinstance(negative, str):
        negative = ""
    return prompt.strip(), negative.strip()


async def compose(description: str, cfg: LlmConfig, think: Any = None) -> tuple[str, str]:
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
