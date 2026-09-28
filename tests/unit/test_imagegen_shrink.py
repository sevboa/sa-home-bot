"""llm/imagegen.py::shrink_to_png и bot/image_tools.py::upscale_png (Этап 48).

Только Pillow — diffusers/torch в dev-окружении нет, и модуль не должен
тянуть их при импорте (ленивый импорт внутри _load_pipeline_sync)."""

from __future__ import annotations

import io
import sys

import pytest
from PIL import Image

from sa_home_bot.bot.image_tools import upscale_png
from sa_home_bot.llm.imagegen import shrink_to_png


def _gradient(width: int = 512, height: int = 512) -> Image.Image:
    image = Image.new("RGB", (width, height))
    image.putdata([
        (x * 255 // width, y * 255 // height, (x + y) * 255 // (width + height))
        for y in range(height) for x in range(width)
    ])
    return image


def _open(png: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG"
    return image


def test_import_does_not_pull_diffusers():
    assert "diffusers" not in sys.modules
    assert "torch" not in sys.modules


@pytest.mark.parametrize("size", [64, 128, 256])
def test_shrink_square_to_size(size):
    png, width, height = shrink_to_png(_gradient(), size, 0)
    assert (width, height) == (size, size)
    image = _open(png)
    assert image.size == (size, size)
    assert image.mode == "RGB"


def test_shrink_keeps_aspect_ratio():
    png, width, height = shrink_to_png(_gradient(512, 256), 128, 0)
    assert (width, height) == (128, 64)
    assert _open(png).size == (128, 64)


def test_shrink_does_not_upscale_small_image():
    _, width, height = shrink_to_png(_gradient(32, 32), 128, 0)
    assert (width, height) == (32, 32)


def test_shrink_converts_rgba_to_rgb():
    image = Image.new("RGBA", (512, 512), (10, 20, 30, 128))
    png, _, _ = shrink_to_png(image, 64, 0)
    assert _open(png).mode == "RGB"


def test_shrink_with_palette_quantizes_to_p_mode():
    png, width, height = shrink_to_png(_gradient(), 64, 16)
    image = _open(png)
    assert image.mode == "P"
    assert (width, height) == (64, 64)
    used = image.getcolors(maxcolors=256)
    assert used is not None and len(used) <= 16


@pytest.mark.parametrize("display_px, factor", [(512, 4), (500, 3), (128, 1), (64, 1)])
def test_upscale_is_integer_multiple(display_px, factor):
    small, _, _ = shrink_to_png(_gradient(), 128, 0)
    big = _open(upscale_png(small, display_px))
    assert big.size == (128 * factor, 128 * factor)


def test_upscale_is_nearest_without_smoothing():
    small = Image.new("RGB", (2, 1))
    small.putdata([(0, 0, 0), (255, 255, 255)])
    buf = io.BytesIO()
    small.save(buf, format="PNG")
    big = _open(upscale_png(buf.getvalue(), 8)).convert("RGB")
    assert big.size == (8, 4)
    # чёткая граница: левая половина чёрная, правая белая, без полутонов
    assert {big.getpixel((x, 0)) for x in range(4)} == {(0, 0, 0)}
    assert {big.getpixel((x, 0)) for x in range(4, 8)} == {(255, 255, 255)}


def test_upscale_keeps_palette_mode():
    small, _, _ = shrink_to_png(_gradient(), 64, 16)
    big = _open(upscale_png(small, 256))
    assert big.size == (256, 256)
    assert big.mode == "P"


# --- apply_style: сборка промпта по шаблону из конфига ---

from sa_home_bot.config import LlmConfig  # noqa: E402
from sa_home_bot.llm.imagegen import apply_style  # noqa: E402


def test_style_default_template_keeps_prompt():
    assert apply_style("a cat", "blurry", LlmConfig()) == ("a cat", "blurry")


def test_style_template_wraps_prompt_and_extends_negative():
    cfg = LlmConfig(
        imagegen_prompt_template="pixelsprite, {prompt}, pixel art",
        imagegen_style_negative="photorealistic",
    )
    assert apply_style("a cat", "blurry", cfg) == (
        "pixelsprite, a cat, pixel art",
        "blurry, photorealistic",
    )


def test_style_template_without_placeholder_appends_prompt():
    cfg = LlmConfig(imagegen_prompt_template="pixelsprite")
    assert apply_style("a cat", "", cfg)[0] == "pixelsprite, a cat"


def test_style_negative_alone_when_no_base_negative():
    cfg = LlmConfig(imagegen_style_negative="photo")
    assert apply_style("a cat", "", cfg)[1] == "photo"
