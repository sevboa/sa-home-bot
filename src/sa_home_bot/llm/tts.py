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

_MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
_SPEAKER_ID = "alfred"


class _VoiceCache:
    def __init__(self, speakers: dict[str, dict[str, Any]]) -> None:
        self.speakers = speakers


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

    log.info("tts: расчёт латентов голоса из референса (один раз за процесс)...")
    xtts_model = model.synthesizer.tts_model
    xtts_cfg = xtts_model.config
    gpt_cond_latent, speaker_embedding = xtts_model.get_conditioning_latents(
        audio_path=str(cfg.tts_reference_voice_path),
        gpt_cond_len=xtts_cfg["gpt_cond_len"],
        gpt_cond_chunk_len=xtts_cfg["gpt_cond_chunk_len"],
        max_ref_length=xtts_cfg["max_ref_len"],
        sound_norm_refs=xtts_cfg["sound_norm_refs"],
    )
    xtts_model.speaker_manager = _VoiceCache(
        {
            _SPEAKER_ID: {
                "gpt_conditioning_latents": gpt_cond_latent,
                "speaker_embedding": speaker_embedding,
            }
        }
    )
    log.info("tts: латенты голоса закэшированы")
    return model


def _synthesize_wav_sync(model: Any, text: str, cfg: LlmConfig) -> Path:
    cfg.tts_tmp_dir.mkdir(parents=True, exist_ok=True)
    wav_path = cfg.tts_tmp_dir / f"{uuid.uuid4().hex}.wav"
    model.tts_to_file(
        text=text,
        language=cfg.tts_language,
        speaker=_SPEAKER_ID,
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
    if not Path(cfg.tts_reference_voice_path).is_file():
        raise RuntimeError(f"референсный голос не найден: {cfg.tts_reference_voice_path}")
    model = await _model.get(cfg)
    wav_path = await asyncio.to_thread(_synthesize_wav_sync, model, text, cfg)
    ogg_path = wav_path.with_suffix(".ogg")
    try:
        await _wav_to_ogg(wav_path, ogg_path, cfg)
        return ogg_path.read_bytes()
    finally:
        for p in (wav_path, ogg_path):
            with contextlib.suppress(OSError):
                p.unlink()


_model = LazyModel(lambda cfg: _load_model_sync(cfg))
