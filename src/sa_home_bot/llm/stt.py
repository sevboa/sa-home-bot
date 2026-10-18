from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from pathlib import Path
from typing import Any

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm.lazy_load import LazyModel

log = logging.getLogger(__name__)


def _load_model_sync(cfg: LlmConfig) -> Any:
    from faster_whisper import WhisperModel

    cfg.stt_model_dir.mkdir(parents=True, exist_ok=True)
    log.info(
        "stt: загрузка модели %s (CPU, compute_type=%s, %d потоков)...",
        cfg.stt_model_size, cfg.stt_compute_type, cfg.stt_cpu_threads,
    )
    model = WhisperModel(
        cfg.stt_model_size,
        device="cpu",
        compute_type=cfg.stt_compute_type,
        cpu_threads=cfg.stt_cpu_threads,
        download_root=str(cfg.stt_model_dir),
    )
    log.info("stt: модель %s загружена", cfg.stt_model_size)
    return model


def _write_transcribe_cleanup_sync(
    model: Any, raw_bytes: bytes, tmp_dir: Path, cfg: LlmConfig
) -> str:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f"{uuid.uuid4().hex}.ogg"
    try:
        path.write_bytes(raw_bytes)
        segments, _info = model.transcribe(
            str(path), language=cfg.stt_language, vad_filter=True
        )
        return " ".join(seg.text.strip() for seg in segments if seg.text and seg.text.strip())
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


async def transcribe_voice(raw_bytes: bytes, cfg: LlmConfig) -> str:
    model = await _model.get(cfg)
    text = await asyncio.to_thread(
        _write_transcribe_cleanup_sync, model, raw_bytes, cfg.stt_tmp_dir, cfg
    )
    return text.strip()


_model = LazyModel(lambda cfg: _load_model_sync(cfg))
