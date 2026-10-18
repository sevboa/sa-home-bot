from __future__ import annotations

import base64
import io
import logging
import time

from PIL import Image, ImageOps

from sa_home_bot.config import LlmConfig

log = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 250 * 1024
_QUALITY_STEPS = (80, 65, 50)
_SWEEP_MIN_INTERVAL_S = 24 * 3600.0

_last_sweep_at = 0.0


JPEG_QUALITY = 90


def encode_b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def jpeg_bytes(image: Image.Image, quality: int = JPEG_QUALITY) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def jpeg_b64(image: Image.Image) -> str:
    return encode_b64(jpeg_bytes(image))


def _resized_jpeg(raw: bytes, cfg: LlmConfig) -> bytes:
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img) or img
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail(
        (cfg.photo_max_edge_px, cfg.photo_max_edge_px), resample=Image.Resampling.LANCZOS
    )
    return _encode_with_degradation(img, cfg.photo_jpeg_quality)


def prepare(raw: bytes, cfg: LlmConfig) -> str:
    return encode_b64(_resized_jpeg(raw, cfg))


def resize_and_store(raw: bytes, photo_key: str, cfg: LlmConfig) -> str:
    encoded = _resized_jpeg(raw, cfg)

    cfg.photos_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.photos_dir / f"{photo_key}.jpg"
    path.write_bytes(encoded)
    log.info("vision: сохранено фото %s (%d байт)", photo_key, len(encoded))
    return encode_b64(encoded)


def _encode_with_degradation(img: Image.Image, start_quality: int) -> bytes:
    qualities = sorted({start_quality, *(q for q in _QUALITY_STEPS if q <= start_quality)})
    encoded = b""
    for quality in reversed(qualities):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        encoded = buf.getvalue()
        if len(encoded) <= _MAX_OUTPUT_BYTES:
            return encoded
    return encoded


def load_stored(photo_key: str, cfg: LlmConfig) -> str | None:
    path = cfg.photos_dir / f"{photo_key}.jpg"
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return encode_b64(raw)


def cleanup_old_photos(cfg: LlmConfig) -> int:
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
    global _last_sweep_at
    now = time.time()
    if now - _last_sweep_at < _SWEEP_MIN_INTERVAL_S:
        return
    _last_sweep_at = now
    cleanup_old_photos(cfg)
