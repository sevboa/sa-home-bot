"""Генерация картинок по просьбе собеседника /ai (Этап 48).

Намеренно на стороне службы llm и на CPU — та же причина, что у llm/stt.py и
llm/tts.py: VRAM Tesla V100 почти целиком занята gemma, вторая модель её бы
выгрузила. Xeon E5-2678 v3 (12 ядер/24 потока, AVX2) вытягивает SD1.5 за
секунды благодаря LCM-LoRA — 4 шага вместо 25-50.

Отдельный процесс-генератор, а не Ollama: у Ollama на mycraft одна общая
очередь, долгая CPU-задача в ней стопорила бы живой чат (живая находка
Этапа 41, graph_memory).

SD1.5 рисует только в родном 512×512 — меньшие размеры (латент 1/8) дают
кашу. Поэтому генерация всегда 512², а уменьшение до ``imagegen_size`` и
необязательное квантование палитры — отдельная постобработка
(``shrink_to_png``), которая от модели не зависит и тестируется без неё.

``diffusers``/``torch`` — не основные зависимости пакета (экстра
``imagegen``, см. pyproject.toml), поэтому импорт ленивый, внутри
``_load_pipeline_sync``: модуль (и служба llm целиком) импортируется и на
нодах без этой экстры.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import Any

from PIL import Image

from sa_home_bot.config import LlmConfig

log = logging.getLogger(__name__)

_NATIVE_PX = 512

# Пайплайн резидентен в RAM (~4 ГБ fp32) с первого запроса до конца жизни
# процесса — как XTTS в llm/tts.py. Лок генерации отдельно от лока загрузки:
# две генерации одновременно на одном CPU только мешали бы друг другу.
_pipeline: Any = None
_load_lock = asyncio.Lock()
_generate_lock = asyncio.Lock()


def _load_pipeline_sync(cfg: LlmConfig) -> Any:
    import torch
    from diffusers import LCMScheduler, StableDiffusionPipeline

    torch.set_num_threads(cfg.imagegen_threads)
    cfg.imagegen_model_dir.mkdir(parents=True, exist_ok=True)
    log.info("imagegen: загрузка %s + %s (CPU)...", cfg.imagegen_model, cfg.imagegen_lcm_lora)
    started = time.monotonic()
    # variant="fp16" — вдвое меньше скачивать; на CPU считаем в fp32
    # (half на CPU медленнее и местами не поддержан), веса апкастятся при
    # загрузке. safety_checker выключен: право generate_image@llm выдаётся
    # владельцем вручную, а сам чекер — ещё ~1 ГБ и лишний проход.
    pipe = StableDiffusionPipeline.from_pretrained(
        cfg.imagegen_model,
        variant="fp16",
        torch_dtype=torch.float32,
        cache_dir=str(cfg.imagegen_model_dir),
        safety_checker=None,
        requires_safety_checker=False,
    )
    pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)
    pipe.load_lora_weights(cfg.imagegen_lcm_lora, cache_dir=str(cfg.imagegen_model_dir))
    pipe.fuse_lora()
    pipe.set_progress_bar_config(disable=True)
    log.info("imagegen: пайплайн загружен за %.1fс", time.monotonic() - started)
    return pipe


async def _get_pipeline(cfg: LlmConfig) -> Any:
    global _pipeline
    if _pipeline is None:
        async with _load_lock:
            if _pipeline is None:
                _pipeline = await asyncio.to_thread(_load_pipeline_sync, cfg)
    return _pipeline


def shrink_to_png(image: Image.Image, size: int, colors: int) -> tuple[bytes, int, int]:
    """512² → ``size``×``size`` (с сохранением пропорций), опционально
    ``colors`` цветов палитры, → PNG. Возвращает (png, ширина, высота).

    Уменьшение — BOX (усреднение), а не nearest: из 512 в 64-128 nearest
    выдёргивает случайные пиксели и даёт шум, усреднение — чистые пятна.
    Квантование без дизеринга: при крупных пикселях дизер выглядит как грязь.
    """
    image = image.convert("RGB")
    if max(image.size) > size:
        scale = size / max(image.size)
        target = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        image = image.resize(target, Image.Resampling.BOX)
    if colors:
        image = image.quantize(colors=colors, dither=Image.Dither.NONE)
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), image.width, image.height


def _generate_sync(pipe: Any, prompt: str, negative: str, cfg: LlmConfig) -> Image.Image:
    return pipe(
        prompt,
        negative_prompt=negative or None,
        num_inference_steps=cfg.imagegen_steps,
        # LCM работает без classifier-free guidance: 1.0 = выключен, иначе
        # картинка пересвечивается, а время удваивается.
        guidance_scale=1.0,
        width=_NATIVE_PX,
        height=_NATIVE_PX,
    ).images[0]


async def generate_image(prompt: str, negative: str, cfg: LlmConfig) -> dict[str, Any]:
    """Сгенерировать картинку. Результат: ``png`` (байты), ``width``,
    ``height``, ``seconds`` (время самой генерации, без ожидания лока)."""
    pipe = await _get_pipeline(cfg)
    async with _generate_lock:
        started = time.monotonic()
        image = await asyncio.to_thread(_generate_sync, pipe, prompt, negative, cfg)
        seconds = time.monotonic() - started
    png, width, height = shrink_to_png(image, cfg.imagegen_size, cfg.imagegen_colors)
    log.info(
        "imagegen: %dx%d, %d цв., %d байт за %.1fс",
        width, height, cfg.imagegen_colors, len(png), seconds,
    )
    return {"png": png, "width": width, "height": height, "seconds": seconds}
