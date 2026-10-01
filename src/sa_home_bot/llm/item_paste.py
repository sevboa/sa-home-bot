"""Сюжетный предмет в сцене (Этап 49.3): вырезка портрета и вставка.

Если описать предмет словами в промпте сцены, сцена его «съедает» (CLIP
читает промпт мешком слов: стол + свет дают настольную лампу вместо
микрофона), а img2img по готовому кадру с предметом его стирает. Стенд
2026-10-01 («Стенд предметов», раздел paste) показал устойчивую цепочку:

1. портрет предмета (turbo, светло-серый фон) → вырезка rembg
   ``isnet-general-use`` → RGBA в ``cfg.items_dir/{key}.png``;
2. сцена рисуется без предмета (как обычно, с настроением);
3. вырезка вставляется с мягкой тенью и подгонкой яркости/цвета под фон;
4. img2img той же моделью, сила ``item_harmonize_strength`` — свет и стиль
   сцены ложатся на края и тень;
5. сам предмет возвращается по маске (подъесть 1 px, размыть 3 px) —
   DINO к эталону 0.80-0.90 против 0.27-0.63 у простого img2img.

Здесь чистые функции над PIL (тестируются без моделей и без numpy); rembg —
ленивый импорт, как diffusers в llm/imagegen.py: экстра ``imagegen``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

from sa_home_bot.config import LlmConfig

log = logging.getLogger(__name__)

_NATIVE_PX = 512
_REMBG_MODEL = "isnet-general-use"
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PLACES = ("desk", "closeup")

# Тень: сдвиг вправо-вниз, размытие, сила затемнения. Подгонка цвета —
# поканальный множитель к среднему фона вокруг предмета, смешанный с 1.
_SHADOW_SHIFT = (10, 6)
_SHADOW_BLUR = 10
_SHADOW_ALPHA = 0.6
_COLOR_K = 0.7
_COLOR_PAD = 40
_RESTORE_ERODE = 1
_RESTORE_BLUR = 3


@dataclass(frozen=True)
class Placement:
    """Куда ставить: центр по x, низ предмета по y, ширина — в пикселях 512²."""

    cx: float
    bottom: float
    width: float


# Места вставки по умолчанию. ``desk`` — стенд 49.3.0 (2): общий вид «поверх
# столешницы» (bot cabinet.CANON_EN_DESK) ставит стол в нижнюю треть; точнее
# место даёт рамка столешницы от зрения (``desk_placement``), это — когда
# рамки нет. ``closeup`` — крупно, по центру нижней половины.
PLACEMENTS: dict[str, Placement] = {
    "desk": Placement(cx=256, bottom=0.86 * _NATIVE_PX, width=0.42 * _NATIVE_PX),
    "closeup": Placement(cx=256, bottom=0.92 * _NATIVE_PX, width=0.62 * _NATIVE_PX),
}

# Композиция общего вида под вставку на стол (стенд 49.3.0 (2)): стол «на
# переднем плане» модели не слушают, «вид поверх столешницы» ставит его в
# нижнюю треть. Служба дописывает её в начало промпта сама, после
# промптера: тот пересказывал её своими словами и терял композицию.
DESK_COMPOSITION = "view across a big wooden desk top, the desk surface fills the bottom third"

# Рамка столешницы — gemma со зрением, в её родном формате: ``box_2d`` =
# [ymin, xmin, ymax, xmax] в сетке 0..1000. Спросишь «[x0, y0, x1, y1]» без
# пояснений — отвечает то так, то эдак (стенд 49.3.0: на одном кадре
# [663, 0, 845, 542] и [0, 662, 543, 789]); в родном формате — стабильно.
DESK_BOX_SYSTEM = (
    "You look at a picture and report only what is actually visible. "
    "Reply with strict JSON only."
)
DESK_BOX_QUESTION = (
    "Detect the top surface of the desk or table (the tabletop where things can be put). "
    'Return its bounding box as "box_2d": [ymin, xmin, ymax, xmax] normalized to 0-1000. '
    "If there is no desk or table, return null.\n"
    'JSON: {"box_2d": [ymin, xmin, ymax, xmax] or null}'
)
DESK_BOX_KEY = "box_2d"
_BOX_GRID = 1000
# Годная столешница: не уже 20% кадра и не выше верхней трети (дальний
# стол у окна — радио на нём вышло бы с напёрсток).
_DESK_MIN_WIDTH = 0.2
_DESK_MIN_TOP = 0.35


def desk_placement(box: object, size: int = _NATIVE_PX) -> Placement | None:
    """Рамка столешницы ``box_2d`` ([ymin, xmin, ymax, xmax], сетка 0..1000)
    → место предмета; None — рамки нет или она негодная (тогда
    ``PLACEMENTS["desk"]``)."""
    if not isinstance(box, list) or len(box) != 4:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box):
        return None
    y0, x0, y1, x1 = (min(max(float(v), 0.0), _BOX_GRID) * size / _BOX_GRID for v in box)
    if x1 - x0 < size * _DESK_MIN_WIDTH or y0 < size * _DESK_MIN_TOP or y1 <= y0:
        return None
    width = min(PLACEMENTS["desk"].width * size / _NATIVE_PX, 0.8 * (x1 - x0))
    return Placement(cx=(x0 + x1) / 2, bottom=y0 + 0.6 * (y1 - y0), width=width)


def cut_path(cfg: LlmConfig, key: str) -> Path:
    if not KEY_RE.match(key):
        raise ValueError(f"плохой ключ предмета {key!r}")
    return cfg.items_dir / f"{key}.png"


_session = None
_session_lock = threading.Lock()


def cut_out(image: Image.Image, cfg: LlmConfig) -> Image.Image:
    """Портрет на сером фоне → RGBA-вырезка (rembg, синхронно — звать в потоке)."""
    global _session
    with _session_lock:
        if _session is None:
            os.environ.setdefault("U2NET_HOME", str(cfg.imagegen_model_dir / "rembg"))
            from rembg import new_session

            _session = new_session(_REMBG_MODEL)
    from rembg import remove

    cut = remove(image.convert("RGB"), session=_session)
    return cut.convert("RGBA")


def paste(
    scene: Image.Image, cut: Image.Image, where: Placement
) -> tuple[Image.Image, Image.Image, tuple[int, int, int, int]]:
    """Вставить вырезку в сцену: масштаб, тень, подгонка цвета.
    → (кадр RGB, маска предмета L, рамка предмета x0,y0,x1,y1)."""
    scene = scene.convert("RGB")
    size = scene.width
    cut = cut.convert("RGBA")
    bbox = cut.getchannel("A").getbbox()
    if bbox is None:
        raise ValueError("пустая вырезка")
    cut = cut.crop(bbox)
    w = max(8, min(size, int(where.width)))
    h = max(8, min(size, int(cut.height * w / cut.width)))
    cut = cut.resize((w, h), Image.Resampling.LANCZOS)
    x0 = int(min(max(where.cx - w / 2, 0), size - w))
    y0 = int(min(max(where.bottom - h, 0), size - h))
    alpha = cut.getchannel("A")
    obj = cut.convert("RGB")
    # Подгонка цвета: поканальный множитель «среднее фона вокруг / среднее
    # предмета», смешанный с 1 (предмет темнеет в тёмной сцене, но не
    # растворяется в ней).
    pad = _COLOR_PAD
    around = ImageStat.Stat(
        scene.crop(
            (max(0, x0 - pad), max(0, y0 - pad), min(size, x0 + w + pad), min(size, y0 + h + pad))
        )
    ).mean
    own = ImageStat.Stat(obj, alpha).mean
    gains = [
        (1 - _COLOR_K) + _COLOR_K * min(max(a / max(o, 1.0), 0.3), 1.5)
        for a, o in zip(around, own, strict=True)
    ]
    obj = Image.merge(
        "RGB",
        [
            band.point(lambda v, g=g: min(255, round(v * g)))
            for band, g in zip(obj.split(), gains, strict=True)
        ],
    )
    # Мягкая тень правее-ниже предмета.
    shadow = Image.new("L", scene.size, 0)
    shadow.paste(alpha, (x0 + _SHADOW_SHIFT[0], y0 + _SHADOW_SHIFT[1]))
    shadow = shadow.filter(ImageFilter.GaussianBlur(_SHADOW_BLUR)).point(
        lambda v: round(v * _SHADOW_ALPHA)
    )
    out = Image.composite(Image.new("RGB", scene.size, "black"), scene, shadow)
    out.paste(obj, (x0, y0), alpha.filter(ImageFilter.GaussianBlur(1.0)))
    mask = Image.new("L", scene.size, 0)
    mask.paste(alpha, (x0, y0))
    return out, mask, (x0, y0, x0 + w, y0 + h)


def restore(harmonized: Image.Image, pasted: Image.Image, mask: Image.Image) -> Image.Image:
    """Вернуть предмет поверх гармонизированного кадра по подъеденной и
    размытой маске: края и тень остаются от img2img, сам предмет — эталон."""
    soft = mask.filter(ImageFilter.MinFilter(2 * _RESTORE_ERODE + 1)).filter(
        ImageFilter.GaussianBlur(_RESTORE_BLUR)
    )
    return Image.composite(pasted.convert("RGB"), harmonized.convert("RGB"), soft)
