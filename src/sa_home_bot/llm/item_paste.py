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

_SHADOW_SHIFT = (10, 6)
_SHADOW_BLUR = 10
_SHADOW_ALPHA = 0.6
_COLOR_K = 0.7
_COLOR_PAD = 40
_RESTORE_ERODE = 1
_RESTORE_BLUR = 3


@dataclass(frozen=True)
class Placement:
    cx: float
    bottom: float
    width: float


PLACEMENTS: dict[str, Placement] = {
    "desk": Placement(cx=256, bottom=0.86 * _NATIVE_PX, width=0.3 * _NATIVE_PX),
    "closeup": Placement(cx=256, bottom=0.92 * _NATIVE_PX, width=0.45 * _NATIVE_PX),
}

DESK_COMPOSITION = "view across a big wooden desk top, the desk surface fills the bottom third"

DESK_BOX_SYSTEM = (
    "You look at a picture and report only what is actually visible. "
    "Reply with strict JSON only."
)
DESK_BOX_QUESTION = (
    "Detect the flat top surface of the desk or table nearest to the viewer: only the "
    "horizontal wooden surface where an object could stand. Do not include the desk legs, "
    "drawers, front panel, the floor or the carpet. "
    'Return its bounding box as "box_2d": [ymin, xmin, ymax, xmax] normalized to 0-1000. '
    "If no desk or table top is clearly visible, return null.\n"
    'JSON: {"box_2d": [ymin, xmin, ymax, xmax] or null}'
)
DESK_BOX_KEY = "box_2d"
_BOX_GRID = 1000
_DESK_MIN_WIDTH = 0.2
_DESK_MIN_TOP = {"desk": 0.35, "closeup": 0.2}
_DESK_MAX_DEPTH = {"desk": 0.45, "closeup": 0.6}
_DESK_ANCHOR = 0.75
_DESK_SHARE = {"desk": 1 / 3, "closeup": 0.5}
_DESK_MAX_WIDTH = {"desk": 0.42, "closeup": 0.45}
_PERSPECTIVE_FAR = 0.45
_PERSPECTIVE_NEAR = 0.9
_PERSPECTIVE_MIN = 0.25
DESK_REDRAWS = 1


def desk_placement(
    box: object, size: int = _NATIVE_PX, place: str = "desk"
) -> Placement | None:
    if not isinstance(box, list) or len(box) != 4:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box):
        return None
    y0, x0, y1, x1 = (min(max(float(v), 0.0), _BOX_GRID) * size / _BOX_GRID for v in box)
    if (
        x1 - x0 < size * _DESK_MIN_WIDTH
        or y0 < size * _DESK_MIN_TOP[place]
        or y1 <= y0
        or y1 - y0 > size * _DESK_MAX_DEPTH[place]
    ):
        return None
    bottom = y0 + _DESK_ANCHOR * (y1 - y0)
    limit = _DESK_MAX_WIDTH[place] * size
    if place == "desk":
        near = (bottom / size - _PERSPECTIVE_FAR) / (_PERSPECTIVE_NEAR - _PERSPECTIVE_FAR)
        limit *= min(max(near, _PERSPECTIVE_MIN), 1.0)
    width = min(_DESK_SHARE[place] * (x1 - x0), limit)
    return Placement(cx=(x0 + x1) / 2, bottom=bottom, width=width)


def cut_path(cfg: LlmConfig, key: str) -> Path:
    if not KEY_RE.match(key):
        raise ValueError(f"плохой ключ предмета {key!r}")
    return cfg.items_dir / f"{key}.png"


_session = None
_session_lock = threading.Lock()


def cut_out(image: Image.Image, cfg: LlmConfig) -> Image.Image:
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
    soft = mask.filter(ImageFilter.MinFilter(2 * _RESTORE_ERODE + 1)).filter(
        ImageFilter.GaussianBlur(_RESTORE_BLUR)
    )
    return Image.composite(pasted.convert("RGB"), harmonized.convert("RGB"), soft)
