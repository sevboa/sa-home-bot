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
import gc
import io
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from PIL import Image

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm import image_prompt

log = logging.getLogger(__name__)

_NATIVE_PX = 512

# Этап 49, отладочный /draw: генерация по образцу. Константы, а не конфиг —
# пока подбираем; после выбора переедут в LlmConfig. Замер 2026-09-29
# (IMPLEMENTATION_PLAN, 49.0): variant (img2img) рабочий на strength
# 0.5-0.65; для сцены с образцом годится только light-адаптер, scale 0.3-0.5
# (полный топит сцену в образце).
MODES = ("free", "item", "variant", "scene")
DEFAULT_STRENGTH = 0.55
DEFAULT_IP_SCALE = 0.4
_IP_REPO = "h94/IP-Adapter"
_IP_WEIGHT = "ip-adapter_sd15_light.bin"


@dataclass(frozen=True)
class ModelSpec:
    """Модель для ``/draw model=``. ``kind``: ``sd15`` — SD1.5-чекпоинт,
    ускоряется общей LCM-LoRA и умеет IP-Adapter; ``sdxl-turbo`` — свой
    дистиллят на 1-4 шага, без LoRA и без IP-Adapter. ``steps``/``guidance``
    — умолчания модели вместо конфиговых (None — из конфига)."""

    repo: str
    variant: str | None
    kind: str
    steps: int | None = None
    guidance: float | None = None


# Этап 49: подбор модели под предметы (dreamshaper хорош в окружении, но
# технику и стекло рисует плохо). Короткие имена — для /draw; ключ по
# умолчанию (None) — imagegen_model из конфига, т.е. эталон C.
MODELS: dict[str, ModelSpec] = {
    "dream": ModelSpec("Lykon/dreamshaper-8", "fp16", "sd15"),
    "rv": ModelSpec("SG161222/Realistic_Vision_V5.1_noVAE", None, "sd15"),
    "epic": ModelSpec("emilianJR/epiCRealism", None, "sd15"),
    # Turbo обучен без CFG: guidance ≤1 — CFG выключен, негатив не работает.
    "turbo": ModelSpec("stabilityai/sdxl-turbo", "fp16", "sdxl-turbo", steps=2, guidance=1.0),
}
# Сколько НЕ-эталонных моделей держать в RAM сразу (эталон — всегда).
# SD1.5 в fp32 ~4 ГБ, SDXL ~10 ГБ; на mycraft 62 ГБ, gemma живёт в VRAM.
_MAX_EXTRA_MODELS = 2


class _Loaded:
    """Резидентный пайплайн и его производные. img2img — те же веса
    (from_pipe, без второй копии в RAM). IP-Adapter (~+2.5 ГБ) грузится при
    первой сцене с образцом и держится, пока идут такие сцены: с ним в UNet
    обычная генерация без образца падает, поэтому перед любой другой
    генерацией он выгружается. img2img и IP-Adapter — только под
    _generate_lock."""

    def __init__(self, pipe: Any, spec: ModelSpec) -> None:
        self.pipe = pipe
        self.spec = spec
        self.img2img: Any = None
        self.ip_loaded = False


# Пайплайны резидентны в RAM с первого запроса до конца жизни процесса —
# как XTTS в llm/tts.py. Ключ — repo. Лок генерации отдельно от лока
# загрузки: две генерации одновременно на одном CPU только мешали бы друг
# другу.
_pipelines: dict[str, _Loaded] = {}
_load_lock = asyncio.Lock()
_generate_lock = asyncio.Lock()


def resolve_model(name: str | None, cfg: LlmConfig) -> tuple[str, ModelSpec]:
    """Короткое имя → (имя для подписи, спецификация). None — модель из
    конфига (эталон); неизвестное имя — ValueError."""
    if name is None:
        for key, spec in MODELS.items():
            if spec.repo == cfg.imagegen_model:
                return key, ModelSpec(spec.repo, cfg.imagegen_variant or None, spec.kind)
        variant = cfg.imagegen_variant or None
        return cfg.imagegen_model, ModelSpec(cfg.imagegen_model, variant, "sd15")
    if name not in MODELS:
        raise ValueError(f"неизвестная модель {name!r}, есть: {', '.join(MODELS)}")
    return name, MODELS[name]


def _load_pipeline_sync(spec: ModelSpec, cfg: LlmConfig) -> Any:
    import torch

    torch.set_num_threads(cfg.imagegen_threads)
    cfg.imagegen_model_dir.mkdir(parents=True, exist_ok=True)
    log.info("imagegen: загрузка %s (%s, CPU)...", spec.repo, spec.kind)
    started = time.monotonic()
    # variant="fp16" — вдвое меньше скачивать; на CPU считаем в fp32
    # (half на CPU медленнее и местами не поддержан), веса апкастятся при
    # загрузке. safety_checker выключен: право generate_image@llm выдаётся
    # владельцем вручную, а сам чекер — ещё ~1 ГБ и лишний проход.
    common = {
        "variant": spec.variant,
        "torch_dtype": torch.float32,
        "cache_dir": str(cfg.imagegen_model_dir),
    }
    if spec.kind == "sdxl-turbo":
        from diffusers import StableDiffusionXLPipeline

        pipe = StableDiffusionXLPipeline.from_pretrained(spec.repo, **common)
    else:
        from diffusers import LCMScheduler, StableDiffusionPipeline

        pipe = StableDiffusionPipeline.from_pretrained(
            spec.repo, safety_checker=None, requires_safety_checker=False, **common
        )
        pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)
        pipe.load_lora_weights(cfg.imagegen_lcm_lora, cache_dir=str(cfg.imagegen_model_dir))
        pipe.fuse_lora()
    pipe.set_progress_bar_config(disable=True)
    log.info("imagegen: %s загружен за %.1fс", spec.repo, time.monotonic() - started)
    return pipe


async def _get_pipeline(spec: ModelSpec, cfg: LlmConfig) -> _Loaded:
    loaded = _pipelines.get(spec.repo)
    if loaded is not None:
        return loaded
    async with _load_lock:
        loaded = _pipelines.get(spec.repo)
        if loaded is None:
            pipe = await asyncio.to_thread(_load_pipeline_sync, spec, cfg)
            loaded = _pipelines[spec.repo] = _Loaded(pipe, spec)
            extra = [repo for repo in _pipelines if repo not in (cfg.imagegen_model, spec.repo)]
            # dict хранит порядок загрузки — выселяем самые старые. Идущая
            # генерация держит свою ссылку, память освободится после неё.
            for repo in extra[: max(0, len(extra) - _MAX_EXTRA_MODELS)]:
                del _pipelines[repo]
                log.info("imagegen: %s выгружен из RAM", repo)
            gc.collect()
    return loaded


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


def apply_style(prompt: str, negative: str, cfg: LlmConfig) -> tuple[str, str]:
    """Собрать промпт по шаблону из конфига (``imagegen_prompt_template``,
    ``{prompt}`` — суть от модели-персонажа). Слово-триггер стилевой модели
    в шаблоне стоит первым: CLIP сильнее всего слушает начало и режет всё
    после 77 токенов. Шаблон без ``{prompt}`` — суть дописывается в конец,
    чтобы опечатка в конфиге не превращала все картинки в одну и ту же."""
    template = cfg.imagegen_prompt_template.strip() or "{prompt}"
    if "{prompt}" in template:
        prompt = template.replace("{prompt}", prompt)
    else:
        prompt = f"{template}, {prompt}"
    style_negative = cfg.imagegen_style_negative.strip()
    if style_negative:
        negative = f"{negative}, {style_negative}" if negative else style_negative
    return prompt, negative


def _fit_ref(ref: Image.Image) -> Image.Image:
    """Образец → 512² RGB. Маленькие (наши 64px из БД) растягиваются гладко:
    img2img и IP-Adapter ждут «обычную» картинку, а не лесенку пикселей."""
    from PIL import ImageOps

    return ImageOps.fit(ref.convert("RGB"), (_NATIVE_PX, _NATIVE_PX), Image.Resampling.LANCZOS)


def _generate_sync(
    loaded: _Loaded, prompt: str, negative: str, cfg: LlmConfig, job: dict
) -> Image.Image:
    pipe = loaded.pipe
    ip_scale = job.get("ip_scale")
    ref = job.get("ref")
    wants_ip = ref is not None and ip_scale is not None
    if loaded.ip_loaded and not wants_ip:
        pipe.unload_ip_adapter()
        loaded.ip_loaded = False
    common = {
        "negative_prompt": negative or None,
        # LCM рассчитан на 1.0 (guidance выключен); чуть выше — послушнее
        # к промпту, но шаг вдвое дороже (см. LlmConfig.imagegen_guidance).
        "guidance_scale": job["guidance"],
        "generator": job.get("generator"),
    }
    if ref is not None and not wants_ip:
        if loaded.img2img is None:
            if loaded.spec.kind == "sdxl-turbo":
                from diffusers import StableDiffusionXLImg2ImgPipeline as Img2Img
            else:
                from diffusers import StableDiffusionImg2ImgPipeline as Img2Img

            loaded.img2img = Img2Img.from_pipe(pipe)
        return loaded.img2img(
            prompt, image=ref, strength=job["strength"],
            num_inference_steps=job["steps"], **common,
        ).images[0]
    if wants_ip:
        if not loaded.ip_loaded:
            started = time.monotonic()
            pipe.load_ip_adapter(
                _IP_REPO, subfolder="models", weight_name=_IP_WEIGHT,
                cache_dir=str(cfg.imagegen_model_dir),
            )
            loaded.ip_loaded = True
            log.info("imagegen: IP-Adapter загружен за %.1fс", time.monotonic() - started)
        pipe.set_ip_adapter_scale(ip_scale)
        common["ip_adapter_image"] = ref
    return pipe(
        prompt, num_inference_steps=job["steps"], width=_NATIVE_PX, height=_NATIVE_PX, **common,
    ).images[0]


async def generate_image(
    prompt: str,
    negative: str,
    cfg: LlmConfig,
    *,
    seed: int | None = None,
    ref: Image.Image | None = None,
    strength: float | None = None,
    ip_scale: float | None = None,
    steps: int | None = None,
    guidance: float | None = None,
    style: bool = True,
    fit: bool = True,
    size: int | None = None,
    colors: int | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Сгенерировать картинку. Результат: ``png`` (байты), ``width``,
    ``height``, ``seconds`` (время самой генерации, без ожидания лока),
    ``prompt`` (суть, как она ушла в модель — после подгонки под CLIP),
    ``full_prompt``/``full_negative`` (с шаблоном стиля), ``tokens``,
    ``seed``, ``steps``, ``colors``, ``model``.

    Без ключевых аргументов — эталон C (Этап 48). ``ref`` + ``ip_scale`` —
    сцена с образцом (IP-Adapter), ``ref`` без ``ip_scale`` — вариант
    образца (img2img, ``strength``). ``style=False`` — без стилевого
    шаблона, ``fit=False`` — промпт не подрезается под 77 токенов CLIP.
    ``size``/``colors`` — итоговый размер и палитра вместо конфиговых
    (рисуется всё равно 512², это только уменьшение после). ``model`` —
    короткое имя из ``MODELS`` вместо модели из конфига."""
    model, spec = resolve_model(model, cfg)
    if spec.kind != "sd15" and ref is not None and ip_scale is not None:
        raise ValueError(f"у {model} нет IP-Adapter — сцена с образцом только на SD1.5-моделях")
    loaded = await _get_pipeline(spec, cfg)
    pipe = loaded.pipe

    def count_tokens(text: str) -> int:
        return len(pipe.tokenizer(text).input_ids) - 2

    template = cfg.imagegen_prompt_template if style else "{prompt}"
    # Суть + стилевой шаблон должны влезть в 77 токенов CLIP, иначе он молча
    # отрежет хвост — а там как раз стиль (см. llm/image_prompt.py).
    if fit:
        prompt = image_prompt.fit_prompt(prompt, template, count_tokens)
    subject = prompt
    if style:
        prompt, negative = apply_style(prompt, negative, cfg)
    if seed is None:
        seed = random.randrange(2**32)
    steps = steps or spec.steps or cfg.imagegen_steps
    if ref is not None:
        ref = _fit_ref(ref)
    if ref is not None and ip_scale is None:
        strength = strength or DEFAULT_STRENGTH
        # LCM в img2img делает int(steps*strength) шагов — держим столько же
        # реальных, сколько без образца.
        steps = max(steps, round(steps / strength))
    import torch

    job = {
        "generator": torch.Generator().manual_seed(seed),
        "ref": ref, "strength": strength, "ip_scale": ip_scale, "steps": steps,
        "guidance": guidance or spec.guidance or cfg.imagegen_guidance,
    }
    async with _generate_lock:
        started = time.monotonic()
        image = await asyncio.to_thread(_generate_sync, loaded, prompt, negative, cfg, job)
        seconds = time.monotonic() - started
    size = size or cfg.imagegen_size
    colors = cfg.imagegen_colors if colors is None else colors
    png, width, height = shrink_to_png(image, size, colors)
    log.info(
        "imagegen: %s, %dx%d, %d цв., %d байт за %.1fс",
        model, width, height, colors, len(png), seconds,
    )
    return {
        "png": png, "width": width, "height": height, "seconds": seconds, "prompt": subject,
        "full_prompt": prompt, "full_negative": negative, "tokens": count_tokens(prompt),
        "seed": seed, "steps": steps, "colors": colors, "model": model,
    }
