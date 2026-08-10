"""Распознавание голосовых сообщений Telegram (STT) для мультимодального /ai.

Намеренно на стороне службы llm (см. ``LlmConfig.stt_*``), не бота: alfred
шлёт сюда сырые байты голосового как есть, эта нода (сейчас mycraft — Xeon
E5-2678 v3, 12 ядер/24 потока) сама транскрибирует — тот же принцип, что и
у фото (llm/vision.py).

В отличие от фото — CPU/RAM, не GPU и не Ollama. Живая находка при выборе
архитектуры (2026-08-10): аудио-вход самой Gemma 4 (``gemma4:e2b``/``e4b``)
через Ollama на практике сломан (галлюцинации, минуты обработки секунд
аудио, открытые баги апстрима), а продовая модель персонажа
(``gemma-4-26B-A4B``) аудио не поддерживает вовсе — и VRAM V100 занята ей
почти под завязку, второй модели там просто нет места без выгрузки первой.
faster-whisper на CPU не трогает VRAM вообще и не зависит от этой
незрелой фичи апстрима.

``faster-whisper`` — не основная зависимость пакета (устанавливается
extras-группой ``stt``, см. pyproject.toml), поэтому импортируется лениво
внутри ``_load_model_sync`` — модуль (и служба llm целиком) должен
оставаться импортируемым на нодах без этой экстры.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from pathlib import Path
from typing import Any

from sa_home_bot.config import LlmConfig

log = logging.getLogger(__name__)

# Модель резидентна в RAM с первого запроса до конца жизни процесса — в
# отличие от Ollama/gemma4:e4b (отброшенный вариант архитектуры), тут нет
# конкуренции за VRAM и незачем выгружать/перезагружать между запросами:
# medium занимает ~1.5 ГБ, при 62 ГБ RAM на mycraft это не заметно.
_model: Any = None
_model_lock = asyncio.Lock()


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


async def _get_model(cfg: LlmConfig) -> Any:
    global _model
    if _model is None:
        async with _model_lock:
            if _model is None:
                _model = await asyncio.to_thread(_load_model_sync, cfg)
    return _model


def _write_transcribe_cleanup_sync(
    model: Any, raw_bytes: bytes, tmp_dir: Path, cfg: LlmConfig
) -> str:
    """Синхронная часть целиком в одном потоке: запись временного файла,
    сам инференс (CPU-bound, отдаём GIL надолго) и удаление файла — вызывать
    только через ``asyncio.to_thread``, не блокировать событийный цикл
    службы на время генерации других чатов."""
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
    """Транскрибировать голосовое сообщение (сырые байты OGG/Opus) в текст.

    faster-whisper декодирует и режет на окна сам (VAD отсекает тишину) —
    произвольная длина входа, без ручной нарезки на 30-секундные куски
    (в отличие от отброшенного варианта с аудио-входом Gemma 4).

    Пустая строка — распознать не удалось (тишина/неразборчиво), это не
    ошибка; вызывающий (llm/service.py) решает, как её показать пользователю.
    """
    model = await _get_model(cfg)
    text = await asyncio.to_thread(
        _write_transcribe_cleanup_sync, model, raw_bytes, cfg.stt_tmp_dir, cfg
    )
    return text.strip()
