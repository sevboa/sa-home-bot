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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
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
# Этап 49.3.4: черновик turbo — x0-предсказание после 1-го шага того же
# прогона, декодированное крошечным VAE (стенд 2026-10-01: DINO к финалу
# 0.92 за ~41% времени). Не прошёл проверку — прогон прерывается до шага 2
# и полного VAE.
_TAESDXL_REPO = "madebyollin/taesdxl"
_IP_REPO = "h94/IP-Adapter"
_IP_WEIGHT = "ip-adapter_sd15_light.bin"


@dataclass(frozen=True)
class ModelSpec:
    """Модель для ``/draw model=``. ``kind``: ``sd15`` — SD1.5-чекпоинт,
    ускоряется общей LCM-LoRA и умеет IP-Adapter; ``sdxl-turbo`` — свой
    дистиллят на 1-4 шага, без LCM и без IP-Adapter. ``steps``/``guidance``
    — умолчания модели вместо конфиговых (None — из конфига). ``file`` —
    чекпоинт одним файлом в репо ``repo`` (не в формате diffusers)."""

    repo: str
    variant: str | None
    kind: str
    steps: int | None = None
    guidance: float | None = None
    file: str = ""


# Этап 49: подбор модели под предметы (dreamshaper хорош в окружении, но
# технику и стекло рисует плохо). Короткие имена — для /draw; ключ по
# умолчанию (None) — imagegen_model из конфига, т.е. эталон C.
MODELS: dict[str, ModelSpec] = {
    "dream": ModelSpec("Lykon/dreamshaper-8", "fp16", "sd15"),
    "rv": ModelSpec("SG161222/Realistic_Vision_V5.1_noVAE", None, "sd15"),
    "epic": ModelSpec("emilianJR/epiCRealism", None, "sd15"),
    # Turbo обучен без CFG: guidance ≤1 — CFG выключен, негатив не работает.
    "turbo": ModelSpec("stabilityai/sdxl-turbo", "fp16", "sdxl-turbo", steps=2, guidance=1.0),
    # Мрачные SD1.5-чекпоинты (рекомендация владельца 2026-09-30). Зеркала
    # на HF: с Civitai эти файлы без API-токена не отдаются (401).
    "revanim": ModelSpec("Yntec/RevAnimatedV2Rebirth", "fp16", "sd15"),
    "ghostmix": ModelSpec("digiplay/GhostMix", "fp16", "sd15"),
    # Для опытов владельца в /draw (2026-10-08): фотореализм, 2.5D, аниме.
    "rv6": ModelSpec("SG161222/Realistic_Vision_V6.0_B1_noVAE", None, "sd15"),
    "cyber": ModelSpec(
        "cyberdelia/CyberRealistic", None, "sd15", file="CyberRealistic_FINAL_FP16.safetensors"
    ),
    "deliberate": ModelSpec("Yntec/Deliberate2", "fp16", "sd15"),
    "anything": ModelSpec("Yntec/AnythingV5", "fp16", "sd15"),
}


@dataclass(frozen=True)
class LoraSpec:
    """Стилевая LoRA с Civitai (``version`` — id версии файла). ``kind`` —
    к каким моделям подходит: ``sd15`` или ``sdxl`` (SDXL-LoRA идут и на
    SDXL-Turbo). ``trigger`` — слово, на которое её обучали: дописывается в
    начало промпта, кроме raw."""

    version: int
    kind: str
    trigger: str = ""
    # Своя LoRA (не с Civitai): путь внутри imagegen_model_dir, ``version``
    # тогда не используется. Файл в git не лежит — кладётся на ноду руками.
    file: str = ""


# Этап 49: древние постройки, боди-хоррор, сплав органики с предметами и
# зданиями. «World Morph» — перекраивает в свой материал весь кадр.
LORAS: dict[str, LoraSpec] = {
    "giger": LoraSpec(24810, "sd15", "hnsrdlf style"),
    "gigerworld": LoraSpec(343533, "sd15", "gigerworld"),
    "flesh": LoraSpec(246428, "sd15", "fleshmutant"),
    "rottech": LoraSpec(308286, "sd15", "rottentech"),
    "eldritch": LoraSpec(95774, "sd15", "eldritchtech"),
    "ruins": LoraSpec(68719, "sd15"),
    "gigerxl": LoraSpec(195028, "sdxl", "gigercraft"),
    "fleshxl": LoraSpec(246708, "sdxl", "fleshmutant"),
    "bonesxl": LoraSpec(676798, "sdxl", "boneswm"),
    "wormsxl": LoraSpec(670786, "sdxl", "made of worms"),
    "castlesxl": LoraSpec(1281424, "sdxl"),
    "lovecraftxl": LoraSpec(205756, "sdxl", "hp_lovecraft_style"),
    "biomechxl": LoraSpec(1613047, "sdxl"),
    # Облик Альфреда (2026-10-05): обучена на dreamshaper-8 по 104 кадрам
    # Qwen-Image-Edit, стенд — /mnt/data/claude/alfred-bench/qwen на alfred.
    "alfred": LoraSpec(0, "sd15", "alfredbutler", file="loras/alfred-v2.safetensors"),
}
DEFAULT_LORA_WEIGHT = 0.8
_CIVITAI_URL = "https://civitai.com/api/download/models/{version}"


class ImagegenError(RuntimeError):
    """Понятная владельцу причина, почему не нарисовалось (текст — как есть)."""


def lora_fits(lora: LoraSpec, model: ModelSpec) -> bool:
    return lora.kind == ("sd15" if model.kind == "sd15" else "sdxl")


def lora_file(name: str, cfg: LlmConfig) -> Path:
    """Файл LoRA: своя — из кэша моделей (нет — понятная ошибка), с Civitai —
    скачивается при первом обращении."""
    spec = LORAS[name]
    if not spec.file:
        return _civitai_file(spec.version, cfg)
    path = cfg.imagegen_model_dir / spec.file
    if not path.exists():
        raise ImagegenError(f"нет файла LoRA {name}: {path}")
    return path


def _civitai_file(version: int, cfg: LlmConfig) -> Path:
    """Скачать файл версии Civitai в кэш моделей (один раз). Синхронно —
    зовётся из потоков загрузки/генерации."""
    import shutil
    import urllib.error
    import urllib.request

    folder = cfg.imagegen_model_dir / "civitai"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{version}.safetensors"
    if path.exists():
        return path
    part = path.with_suffix(".part")
    started = time.monotonic()
    log.info("imagegen: скачиваю Civitai %d...", version)
    headers = {"User-Agent": "sa-home-bot"}
    if cfg.imagegen_civitai_token:
        headers["Authorization"] = f"Bearer {cfg.imagegen_civitai_token}"
    request = urllib.request.Request(_CIVITAI_URL.format(version=version), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response, part.open("wb") as out:
            shutil.copyfileobj(response, out, 1 << 20)
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        if exc.code in (401, 403):
            raise ImagegenError(
                f"Civitai не отдаёт файл {version} без входа — нужен imagegen_civitai_token"
            ) from None
        raise
    part.rename(path)
    log.info(
        "imagegen: Civitai %d — %.0f МБ за %.0fс",
        version, path.stat().st_size / 2**20, time.monotonic() - started,
    )
    return path
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
        self.loras: set[str] = set()
        self.taesd: Any = None
        self.compel: Any = None


def _prompt_kwargs(loaded: _Loaded, prompt: str, negative: str, job: dict) -> dict[str, Any]:
    """Промпт для пайплайна: строкой или, при ``weighted`` (Этап 49.3,
    портрет предмета на turbo), эмбеддингами compel — веса «(черта)1.3» и
    длина без обрезки по 77 токенам (truncate_long_prompts=False). Негатив
    при весах не нужен: у turbo guidance выключен."""
    if not job.get("weighted"):
        return {"prompt": prompt, "negative_prompt": negative or None}
    import torch

    if loaded.compel is None:
        # compel 2.1: базовый Compel на оба энкодера SDXL (обёрток CompelFor*
        # ещё нет, а 2.4 требует transformers 5).
        from compel import Compel, ReturnedEmbeddingsType

        pipe = loaded.pipe
        loaded.compel = Compel(
            tokenizer=[pipe.tokenizer, pipe.tokenizer_2],
            text_encoder=[pipe.text_encoder, pipe.text_encoder_2],
            returned_embeddings_type=ReturnedEmbeddingsType.PENULTIMATE_HIDDEN_STATES_NON_NORMALIZED,
            requires_pooled=[False, True],
            truncate_long_prompts=False,
        )
    with torch.no_grad():
        embeds, pooled = loaded.compel(prompt)
    return {"prompt_embeds": embeds, "pooled_prompt_embeds": pooled}


class PreviewRejected(Exception):
    """Черновик turbo не прошёл проверку — прогон прерван (Этап 49.3.4)."""


def _preview_sync(
    loaded: _Loaded, prompt: str, negative: str, cfg: LlmConfig, job: dict
) -> Image.Image:
    """turbo с проверкой черновика: обёртка ``scheduler.step`` ловит x0 после
    шага 1 (в callback_on_step_end его нет), TAESDXL декодирует, ``preview``
    решает; «нет» — ``pipe._interrupt`` и PreviewRejected, «да» — шаг 2 и
    полный VAE. Латенты пайплайн отдаёт сырыми (output_type="latent"), чтобы
    прерванный прогон не платил за VAE."""
    import torch

    pipe = loaded.pipe
    if loaded.loras:
        pipe.disable_lora()
    if loaded.taesd is None:
        from diffusers import AutoencoderTiny

        loaded.taesd = AutoencoderTiny.from_pretrained(
            _TAESDXL_REPO, torch_dtype=torch.float32, cache_dir=str(cfg.imagegen_model_dir)
        )
    preview = job["preview"]
    seen: dict[str, Any] = {"x0": None, "rejected": False}
    original_step = pipe.scheduler.step

    def step(*args: Any, **kwargs: Any) -> Any:
        out = original_step(*args, **kwargs)
        if seen["x0"] is None and isinstance(out, tuple) and len(out) > 1:
            seen["x0"] = out[1]
        return out

    def on_step_end(pipe_: Any, index: int, _t: Any, kwargs: dict) -> dict:
        if index == 0 and seen["x0"] is not None:
            with torch.no_grad():
                x = loaded.taesd.decode(seen["x0"]).sample[0]
            x = ((x.clamp(-1, 1) + 1) * 127.5).round().byte().permute(1, 2, 0).numpy()
            if not preview(Image.fromarray(x)):
                seen["rejected"] = True
                pipe_._interrupt = True
        return kwargs

    pipe.scheduler.step = step
    try:
        latents = pipe(
            **_prompt_kwargs(loaded, prompt, negative, job),
            num_inference_steps=job["steps"],
            width=_NATIVE_PX,
            height=_NATIVE_PX,
            guidance_scale=job["guidance"],
            generator=job.get("generator"),
            output_type="latent",
            callback_on_step_end=on_step_end,
        ).images
    finally:
        pipe.scheduler.step = original_step
    if seen["rejected"]:
        raise PreviewRejected
    with torch.no_grad():
        image = pipe.vae.decode(latents / pipe.vae.config.scaling_factor).sample
    return pipe.image_processor.postprocess(image, output_type="pil")[0]


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

        if spec.file:
            from huggingface_hub import hf_hub_download

            path = hf_hub_download(spec.repo, spec.file, cache_dir=common["cache_dir"])
            pipe = StableDiffusionPipeline.from_single_file(
                path, torch_dtype=torch.float32, safety_checker=None,
                requires_safety_checker=False,
            )
        else:
            pipe = StableDiffusionPipeline.from_pretrained(
                spec.repo, safety_checker=None, requires_safety_checker=False, **common
            )
        pipe.scheduler = LCMScheduler.from_config(pipe.scheduler.config)
        pipe.load_lora_weights(cfg.imagegen_lcm_lora, cache_dir=str(cfg.imagegen_model_dir))
        pipe.fuse_lora()
        # LCM остаётся вшитым в веса, а слой адаптера снимаем — иначе
        # стилевые LoRA через set_adapters «распаивали» бы LCM обратно.
        pipe.unload_lora_weights()
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
    if job.get("preview") is not None:
        return _preview_sync(loaded, prompt, negative, cfg, job)
    loras = job.get("loras") or []
    for name, _ in loras:
        if name not in loaded.loras:
            started = time.monotonic()
            pipe.load_lora_weights(
                str(lora_file(name, cfg)), adapter_name=name
            )
            loaded.loras.add(name)
            log.info("imagegen: LoRA %s загружена за %.1fс", name, time.monotonic() - started)
    if loras:
        pipe.enable_lora()
        pipe.set_adapters([name for name, _ in loras], [weight for _, weight in loras])
    elif loaded.loras:
        pipe.disable_lora()
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
    if job.get("weighted"):
        del common["negative_prompt"]
        return pipe(
            **_prompt_kwargs(loaded, prompt, negative, job),
            num_inference_steps=job["steps"], width=_NATIVE_PX, height=_NATIVE_PX, **common,
        ).images[0]
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
    loras: list[tuple[str, float]] | None = None,
    preview: Callable[[Image.Image], bool] | None = None,
    weighted: bool = False,
) -> dict[str, Any]:
    """Сгенерировать картинку. Результат: ``png`` (байты), ``width``,
    ``height``, ``seconds`` (время самой генерации, без ожидания лока),
    ``prompt`` (суть, как она ушла в модель — после подгонки под CLIP),
    ``full_prompt``/``full_negative`` (с шаблоном стиля), ``tokens``,
    ``seed``, ``steps``, ``colors``, ``model``, ``loras``, ``original`` —
    сам 512²-кадр до уменьшения (PIL, для сверки снимка, Этап 49.2.1).

    Без ключевых аргументов — эталон C (Этап 48). ``ref`` + ``ip_scale`` —
    сцена с образцом (IP-Adapter), ``ref`` без ``ip_scale`` — вариант
    образца (img2img, ``strength``). ``style=False`` — без стилевого
    шаблона, ``fit=False`` — промпт не подрезается под 77 токенов CLIP.
    ``size``/``colors`` — итоговый размер и палитра вместо конфиговых
    (рисуется всё равно 512², это только уменьшение после). ``model`` —
    короткое имя из ``MODELS`` вместо модели из конфига. ``loras`` —
    [(имя из ``LORAS``, вес)]; их триггеры дописываются в начало промпта,
    кроме ``fit=False`` (raw — ровно то, что написано).

    ``preview`` (Этап 49.3.4, только turbo без образца и LoRA) — проверка
    черновика после 1-го шага, зовётся в потоке генерации; False — прогон
    прерывается, ``PreviewRejected``. ``weighted`` — промпт с весами compel
    «(слова)1.3» без обрезки по 77 токенам (тоже только turbo без образца
    и LoRA; ``fit`` при этом не нужен)."""
    model, spec = resolve_model(model, cfg)
    if (preview is not None or weighted) and (
        spec.kind != "sdxl-turbo" or ref is not None or loras
    ):
        raise ValueError("черновик и веса compel — только turbo без образца и LoRA")
    if spec.kind != "sd15" and ref is not None and ip_scale is not None:
        raise ValueError(f"у {model} нет IP-Adapter — сцена с образцом только на SD1.5-моделях")
    loras = list(loras or [])
    for name, _ in loras:
        if name not in LORAS:
            raise ValueError(f"неизвестная LoRA {name!r}")
        if not lora_fits(LORAS[name], spec):
            raise ValueError(f"LoRA {name} ({LORAS[name].kind}) не подходит к {model}")
    if fit:
        triggers = [LORAS[name].trigger for name, _ in loras if LORAS[name].trigger]
        if triggers:
            prompt = ", ".join([*triggers, prompt])
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
        "ref": ref, "strength": strength, "ip_scale": ip_scale, "steps": steps, "loras": loras,
        "preview": preview, "weighted": weighted,
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
        "original": image,
        "full_prompt": prompt, "full_negative": negative, "tokens": count_tokens(prompt),
        "seed": seed, "steps": steps, "colors": colors, "model": model,
        "loras": [f"{name}:{weight:g}" for name, weight in loras],
    }
