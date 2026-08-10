"""Обработка фото для мультимодального /ai.

Намеренно на стороне службы llm (см. ``LlmConfig.photos_dir``), не бота:
alfred присылает сырые байты как есть (``raw_image`` в llm/service.py), а
эта нода (сейчас mycraft — сильнее CPU, уже держит Ollama/Tesla V100) сама
делает Pillow-ресайз и хранит результат у себя же. Тулу look_at_photo
(bot/tools.py) потом не нужно ничего пересылать заново — файл уже на месте.
"""

from __future__ import annotations

import base64
import io
import logging
import time

from PIL import Image, ImageOps

from sa_home_bot.config import LlmConfig

log = logging.getLogger(__name__)

# Деградация качества, если 250 КБ (запас от MAX_MESSAGE_BYTES=1МиБ, см.
# proto/messages.py и прецедент vpn/service.py::APK_CHUNK_BYTES) не удаётся
# уложить с первого раза — редкий случай очень шумного/сложного фото.
_MAX_OUTPUT_BYTES = 250 * 1024
_QUALITY_STEPS = (80, 65, 50)
_SWEEP_MIN_INTERVAL_S = 24 * 3600.0

_last_sweep_at = 0.0


def resize_and_store(raw: bytes, photo_key: str, cfg: LlmConfig) -> str:
    """EXIF-transpose + ресайз до ``cfg.photo_max_edge_px`` + JPEG-сжатие с
    деградацией качества, сохранить в ``cfg.photos_dir/{photo_key}.jpg``,
    вернуть base64 итогового файла (готов для ``images`` в сообщении Ollama).

    CPU-bound — вызывать через ``asyncio.to_thread`` (как ``llm/ollama.py``
    делает для своего urllib-клиента), чтобы не блокировать событийный цикл
    службы на время генерации других чатов.
    """
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img) or img
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail(
        (cfg.photo_max_edge_px, cfg.photo_max_edge_px), resample=Image.Resampling.LANCZOS
    )

    encoded = _encode_with_degradation(img, cfg.photo_jpeg_quality)

    cfg.photos_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.photos_dir / f"{photo_key}.jpg"
    path.write_bytes(encoded)
    log.info("vision: сохранено фото %s (%d байт)", photo_key, len(encoded))
    return base64.b64encode(encoded).decode()


def _encode_with_degradation(img: Image.Image, start_quality: int) -> bytes:
    qualities = sorted({start_quality, *(q for q in _QUALITY_STEPS if q <= start_quality)})
    encoded = b""
    for quality in reversed(qualities):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        encoded = buf.getvalue()
        if len(encoded) <= _MAX_OUTPUT_BYTES:
            return encoded
    return encoded  # не уложились даже на самом низком качестве — отдаём как есть


def load_stored(photo_key: str, cfg: LlmConfig) -> str | None:
    """Прочитать уже сохранённый файл (для ACTION_LOOK_AT_PHOTO) — без
    повторного ресайза, файл уже в нужном виде. None — файла нет (устарел,
    удалён TTL-уборкой, либо ключ невалиден)."""
    path = cfg.photos_dir / f"{photo_key}.jpg"
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return base64.b64encode(raw).decode()


def cleanup_old_photos(cfg: LlmConfig) -> int:
    """Удалить файлы старше ``cfg.photo_ttl_days`` (по mtime). Возвращает
    число удалённых файлов. Синхронная — вызывать через asyncio.to_thread."""
    if not cfg.photos_dir.is_dir():
        return 0
    cutoff = time.time() - cfg.photo_ttl_days * 86400
    removed = 0
    for path in cfg.photos_dir.glob("*.jpg"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        log.info("vision: TTL-уборка удалила %d старых фото", removed)
    return removed


def maybe_sweep_sync(cfg: LlmConfig) -> None:
    """Ленивая версия ``cleanup_old_photos`` — не чаще раза в сутки за
    время жизни процесса. Вызывается из run_command после успешного
    resize_and_store; у службы llm нет своего периодического планировщика
    (в отличие от monitor/tasks), заводить его ради редкой уборки — оверкилл
    при типичном свободном месте на диске."""
    global _last_sweep_at
    now = time.time()
    if now - _last_sweep_at < _SWEEP_MIN_INTERVAL_S:
        return
    _last_sweep_at = now
    cleanup_old_photos(cfg)
