"""Синтез речи (TTS) для голосовых ответов Альфреда в /ai.

Намеренно на стороне службы llm (см. ``LlmConfig.tts_*``), не бота: та же
служба и та же нода (сейчас mycraft — Xeon E5-2678 v3, 12 ядер/24 потока),
что и распознавание голосовых (llm/stt.py) — по той же причине: CPU, не
GPU/Ollama, VRAM V100 занята продовой моделью персонажа почти под завязку.

Coqui XTTS v2 — voice cloning по референсному клипу (``LlmConfig.
tts_reference_voice_path``), не набор фиксированных дикторов: голос Альфреда
задаётся одним WAV-образцом, а не выбором из готового списка. Картавость
персонажа (llm/speech_therapy.py) сюда не имеет отношения — она уже вшита
побуквенной подменой в сам текст ответа ДО синтеза, поэтому синтезатору
не нужно уметь имитировать дефект речи: он просто прочитает уже
подменённые буквы.

XTTS v2 отдаёт только WAV — прямого OGG/Opus-выхода у Coqui нет, а Telegram
``sendVoice`` требует именно OGG/Opus. Конвертация — внешним процессом
``ffmpeg`` (тот же класс интеграции, что и у остальных системных утилит в
проекте: smartctl, systemctl, git, wg — см. sensors/*.py, vpn/awg.py), не
Python-биндинг и не новая pip-зависимость. Требует ``apt install ffmpeg`` на
ноде — разовый инфраструктурный шаг, не покрытый extras-группой ``tts``.

``coqui-tts`` — не основная зависимость пакета (устанавливается
extras-группой ``tts``, см. pyproject.toml), поэтому импортируется лениво
внутри ``_load_model_sync`` — модуль (и служба llm целиком) должен
оставаться импортируемым на нодах без этой экстры.

Модель распространяется под Coqui Public Model License (CPML,
некоммерческая) — первая загрузка весов требует явного согласия, которое
даёт оператор через переменную окружения ``COQUI_TOS_AGREED=1`` в юните
службы (см. деплой-заметку в pyproject.toml), не код: молча соглашаться с
лицензией от имени пользователя не наше дело.
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

_MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"

# Модель резидентна в RAM с первого запроса до конца жизни процесса — как и
# faster-whisper в llm/stt.py, никакой конкуренции за VRAM и незачем
# выгружать/перезагружать между запросами.
_model: Any = None
_model_lock = asyncio.Lock()


def _load_model_sync(cfg: LlmConfig) -> Any:
    from TTS.api import TTS
    from TTS.utils.manage import ModelManager

    cfg.tts_model_dir.mkdir(parents=True, exist_ok=True)
    log.info("tts: загрузка XTTS v2 (CPU)...")
    manager = ModelManager(output_prefix=str(cfg.tts_model_dir), progress_bar=False)
    model_path, config_path, _model_item = manager.download_model(_MODEL_NAME)
    model = TTS(
        model_path=str(model_path), config_path=str(config_path), progress_bar=False, gpu=False
    )
    log.info("tts: модель XTTS v2 загружена")
    return model


async def _get_model(cfg: LlmConfig) -> Any:
    global _model
    if _model is None:
        async with _model_lock:
            if _model is None:
                _model = await asyncio.to_thread(_load_model_sync, cfg)
    return _model


def _synthesize_wav_sync(model: Any, text: str, cfg: LlmConfig) -> Path:
    cfg.tts_tmp_dir.mkdir(parents=True, exist_ok=True)
    wav_path = cfg.tts_tmp_dir / f"{uuid.uuid4().hex}.wav"
    model.tts_to_file(
        text=text,
        language=cfg.tts_language,
        speaker_wav=str(cfg.tts_reference_voice_path),
        file_path=str(wav_path),
    )
    return wav_path


async def _wav_to_ogg(wav_path: Path, ogg_path: Path, cfg: LlmConfig) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(wav_path),
        "-c:a",
        "libopus",
        "-b:a",
        cfg.tts_opus_bitrate,
        "-ac",
        "1",
        str(ogg_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg завершился с кодом {proc.returncode}: {stderr.decode(errors='replace')}"
        )


async def synthesize_speech(text: str, cfg: LlmConfig) -> bytes:
    """Синтезировать голосовой ответ Альфреда, вернуть готовый OGG/Opus.

    Бросает исключение при сбое (нет референсного голоса, модель не
    загрузилась, ffmpeg упал) — в отличие от STT, где пустой результат не
    ошибка. Вызывающий (llm/service.py) явно транслирует это в ProtoError,
    а bot/voice_tts.py решает откатиться на текстовый ответ.
    """
    if not Path(cfg.tts_reference_voice_path).is_file():
        raise RuntimeError(f"референсный голос не найден: {cfg.tts_reference_voice_path}")
    model = await _get_model(cfg)
    wav_path = await asyncio.to_thread(_synthesize_wav_sync, model, text, cfg)
    ogg_path = wav_path.with_suffix(".ogg")
    try:
        await _wav_to_ogg(wav_path, ogg_path, cfg)
        return ogg_path.read_bytes()
    finally:
        for p in (wav_path, ogg_path):
            with contextlib.suppress(OSError):
                p.unlink()
