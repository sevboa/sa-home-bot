"""Отладочный /draw (Этап 49): режимы генерации руками владельца.

Подбираем архетипы предметов и настройки сцен для интерактивов, прежде чем
отдавать их Ведущему. Рисует та же служба llm, что и generate_image в чате
(llm/imagegen.py), тем же эталоном; сверху — режим, образец и ручки
(seed, strength, ip…). Картинки ложатся в ту же таблицу ``images`` с
``purpose='debug'``: find_image их не видит, дневной лимит не считает,
«/draw clean» удаляет пачкой.

Здесь только разбор команды, памятка и подпись — без aiogram, чтобы
тестировалось без бота. Обработчик — bot/handlers/draw.py.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any

MODES = ("free", "item", "variant", "scene")

CALLBACK_PREFIX = "draw"
CLEAN_YES = "clean"
CLEAN_NO = "cancel"

HELP = """\
🎨 <b>/draw — отладка генерации</b>

<code>/draw режим [ключи] описание [| контекст сцены] [| neg: …]</code>

<b>Режимы</b>
• <code>free</code> — как «нарисуй» в чате (эталон)
• <code>item</code> — один предмет на белом фоне
• <code>variant</code> — тот же предмет по образцу, с изменениями
• <code>scene</code> — что видит Альфред; с образцом — предмет в сцене

<b>Образец</b> — ответь командой на картинку (любую, можно свою) или <code>ref=ID</code>.

<b>Ключи</b> — сразу после режима:
<code>raw</code> — описание в генератор как есть, без промптера и подрезки
<code>nostyle</code> — без стилевого шаблона
<code>seed=N</code> — повтор (без него случайный, будет в подписи)
<code>s=0.55</code> — сила изменений variant (рабочие 0.5–0.65)
<code>ip=0.4</code> — сила образца в scene (рабочие 0.3–0.5)
<code>steps=6</code> <code>cfg=1.5</code> — шаги и guidance

После <code>|</code> — контекст сцены для промптера (при raw не нужен).
<code>| neg: …</code> — чего не рисовать.

<b>Примеры</b>
<code>/draw item старый проклятый радиопередатчик с антенной</code>
<code>/draw item raw seed=42 old radio transmitter, single object, white background</code>
ответом на картинку: <code>/draw variant s=0.6 треснула лампа, светится зелёным</code>
<code>/draw scene ref=12 Альфред держит передатчик | чердак, дверь забита досками</code>

<b>Служебное</b>
<code>/draw keep ID</code> — оставить как образец (чистка не тронет)
<code>/draw clean</code> — удалить отладочные картинки из базы (спросит)
<code>/draw help</code> — эта памятка"""

# Ключ → (имя аргумента службы, тип, мин, макс).
_NUMERIC_KEYS: dict[str, tuple[str, type, float, float]] = {
    "seed": ("seed", int, 0, 2**32 - 1),
    "s": ("strength", float, 0.05, 1.0),
    "strength": ("strength", float, 0.05, 1.0),
    "ip": ("ip_scale", float, 0.0, 1.5),
    "steps": ("steps", int, 1, 30),
    "cfg": ("guidance", float, 1.0, 10.0),
    "guidance": ("guidance", float, 1.0, 10.0),
    "ref": ("ref", int, 1, 2**63 - 1),
}
_FLAGS = ("raw", "nostyle")


class DrawSyntaxError(ValueError):
    """Текст ошибки — для владельца как есть."""


@dataclass
class DrawRequest:
    mode: str
    description: str
    context: str = ""
    negative: str = ""
    raw: bool = False
    style: bool = True
    ref_id: int | None = None
    numbers: dict[str, Any] = field(default_factory=dict)

    def service_args(self) -> dict[str, Any]:
        """Аргументы generate_image службы llm (без образца — его кладёт
        обработчик: байты берутся из БД или из Telegram)."""
        args: dict[str, Any] = {"description": self.description, "mode": self.mode}
        if self.context:
            args["context"] = self.context
        if self.negative:
            args["negative"] = self.negative
        if self.raw:
            args["raw"] = True
        if not self.style:
            args["style"] = False
        args.update(self.numbers)
        return args

    def params(self) -> dict[str, Any]:
        """Что записать в images.params — всё, чтобы повторить."""
        data = self.service_args()
        data.pop("description")
        if self.ref_id is not None:
            data["ref"] = self.ref_id
        return data


@dataclass(frozen=True)
class DrawCommand:
    """help / clean / keep — всё, что не рисование."""

    name: str
    image_id: int | None = None


def _parse_number(key: str, value: str) -> tuple[str, Any]:
    name, kind, lo, hi = _NUMERIC_KEYS[key]
    try:
        number = kind(value.replace(",", "."))
    except ValueError:
        raise DrawSyntaxError(f"{key}= ждёт число, а не «{value}»") from None
    if not lo <= number <= hi:
        raise DrawSyntaxError(f"{key}= вне диапазона {lo:g}…{hi:g}")
    return name, number


def parse(args: str | None) -> DrawRequest | DrawCommand:
    """Текст после «/draw» → запрос на рисование или служебная команда."""
    text = (args or "").strip()
    if not text:
        return DrawCommand("help")
    head, *segments = [part.strip() for part in text.split("|")]
    words = head.split()
    first = words[0].lower()
    if first in ("help", "помощь", "?"):
        return DrawCommand("help")
    if first == "clean":
        return DrawCommand("clean")
    if first == "keep":
        if len(words) != 2 or not words[1].lstrip("#").isdigit():
            raise DrawSyntaxError("keep ждёт номер картинки: /draw keep 12")
        return DrawCommand("keep", int(words[1].lstrip("#")))
    if first not in MODES:
        raise DrawSyntaxError(f"неизвестный режим «{words[0]}», есть: {', '.join(MODES)}")

    request = DrawRequest(mode=first, description="")
    rest = words[1:]
    while rest:
        token = rest[0]
        low = token.lower()
        if low in _FLAGS:
            # Повтор флага — уже описание («item raw raw meat…»).
            if (request.raw if low == "raw" else not request.style):
                break
            if low == "raw":
                request.raw = True
            else:
                request.style = False
        elif "=" in low and low.split("=", 1)[0] in _NUMERIC_KEYS:
            key, value = token.split("=", 1)
            name, number = _parse_number(key.lower(), value)
            if name == "ref":
                request.ref_id = number
            else:
                request.numbers[name] = number
        else:
            break
        rest = rest[1:]
    request.description = " ".join(rest)
    if not request.description:
        raise DrawSyntaxError("нет описания — что рисовать?")
    for segment in segments:
        if not segment:
            continue
        if segment.lower().startswith("neg:"):
            request.negative = segment[4:].strip()
        else:
            for prefix in ("режиссёр:", "режиссер:", "контекст:"):
                if segment.lower().startswith(prefix):
                    segment = segment[len(prefix):].strip()
                    break
            request.context = f"{request.context}; {segment}" if request.context else segment
    if request.mode == "variant" and "ip_scale" in request.numbers:
        raise DrawSyntaxError("ip= только для scene; у variant сила — s=")
    if request.mode == "scene" and "strength" in request.numbers:
        raise DrawSyntaxError("s= только для variant; у scene сила образца — ip=")
    return request


def needs_ref(request: DrawRequest) -> bool:
    return request.mode == "variant"


def accepts_ref(request: DrawRequest) -> bool:
    return request.mode in ("variant", "scene")


def caption(
    image_id: int, request: DrawRequest, result: dict[str, Any], ref_label: str | None
) -> str:
    """Подпись к отладочной картинке (HTML, ≤1024 символов)."""
    head = [f"#{image_id}", request.mode, f"seed {result.get('seed', '?')}"]
    if ref_label and request.mode == "variant":
        head.append(f"s {request.numbers.get('strength', 'по умолч.')}")
    if ref_label and request.mode == "scene":
        head.append(f"ip {request.numbers.get('ip_scale', 'по умолч.')}")
    head.append(f"шагов {result.get('steps', '?')}")
    timing = f"{result.get('seconds', '?')} с"
    if result.get("prompt_seconds"):
        timing += f" (+промптер {result['prompt_seconds']} с)"
    head.append(timing)
    lines = [" · ".join(str(part) for part in head)]
    flags = [name for name, on in (("raw", request.raw), ("nostyle", not request.style)) if on]
    if ref_label:
        flags.append(f"образец {ref_label}")
    if flags:
        lines.append(", ".join(flags))
    tokens = result.get("tokens")
    over = " ⚠️ больше 75 — хвост отрежется" if isinstance(tokens, int) and tokens > 75 else ""
    budget = 1024 - sum(len(line) for line in lines) - 120
    prompt = str(result.get("full_prompt") or result.get("prompt") or "")
    negative = str(result.get("full_negative") or "")
    negative = negative[: max(60, budget // 4)]
    prompt = prompt[: max(100, budget - len(negative))]
    lines.append(f"промпт ({tokens} ток.){over}: <code>{html.escape(prompt)}</code>")
    if negative:
        lines.append(f"neg: <code>{html.escape(negative)}</code>")
    return "\n".join(lines)
