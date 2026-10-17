from __future__ import annotations

import logging
from typing import Any

from PIL import Image

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm import ollama, vision

log = logging.getLogger(__name__)

_OPTIONS: dict[str, Any] = {"temperature": 0, "num_predict": 512}

SYSTEM_PROMPT = """\
You look at a picture and report only what is actually visible. The picture is
a stylized illustration of a room or an object; small details may be simplified.
Do not guess or assume things that are not drawn. Reply with strict JSON only:
{"description": "...", "answers": {"1": "yes|no", ...}}

- description: 2-4 short phrases IN RUSSIAN: what is in the picture, where,
  what colors, what light. Only what is visible. Describe the scene itself, as
  if it were a photo: never name the medium or style ("иллюстрация",
  "рисунок", "стилизованный", "пиксельный").
- answers: for every numbered item, "yes" if it can be recognized in the
  picture at least roughly (stylization, another angle or shade is still
  "yes"), otherwise "no". Respect the "does NOT count" notes of an item.
"""

DEFINITION_SEP = " :: "


def label(item: str) -> str:
    return item.split(DEFINITION_SEP, 1)[0].strip()


def save_original(image: Image.Image, photo_key: str, cfg: LlmConfig) -> str:
    data = vision.jpeg_bytes(image)
    cfg.photos_dir.mkdir(parents=True, exist_ok=True)
    (cfg.photos_dir / f"{photo_key}.jpg").write_bytes(data)
    log.info("photo_check: оригинал снимка %s сохранён (%d байт)", photo_key, len(data))
    return vision.encode_b64(data)


def build_question(expect: list[str]) -> str:
    if not expect:
        return 'What is in the picture? No items to check: "answers" is {}.'
    items = "\n".join(
        f"{n}. {item.replace(DEFINITION_SEP, ' — ')}" for n, item in enumerate(expect, 1)
    )
    return f"What is in the picture? Is each item visible?\n{items}"


def _yes(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ("yes", "да", "true"):
            return True
        if value in ("no", "нет", "false"):
            return False
    return None


def parse(content: str, expect: list[str]) -> dict[str, Any] | None:
    try:
        data = ollama.parse_json_object(content)
    except (ValueError, TypeError):
        return None
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    raw = data.get("answers")
    if not isinstance(raw, dict):
        raw = {}
    answers: dict[str, bool] = {}
    for n, item in enumerate(expect, 1):
        said = _yes(raw.get(str(n)))
        if said is not None:
            answers[label(item)] = said
    missing = [name for name, seen in answers.items() if not seen]
    return {
        "description": " ".join(description.split()),
        "missing": missing,
        "answers": answers,
    }


async def inspect(
    image_b64: str, expect: list[str], cfg: LlmConfig, *, think: Any = None
) -> dict[str, Any] | None:
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
