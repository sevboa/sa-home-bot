"""Синтез голосовых ответов (llm/tts.py) — Coqui XTTS v2 и ffmpeg замокан
целиком: модуль не тянется как зависимость теста, проверяем только нашу
обвязку (референс проверяется, временные файлы создаются/удаляются, модель
кэшируется по процессу)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm import tts as llm_tts


@pytest.fixture(autouse=True)
def _reset_model_cache(monkeypatch):
    # _model — модульный синглтон (см. докстринг llm/tts.py) — без сброса
    # между тестами один тест "прогрел" бы модель для всех последующих.
    monkeypatch.setattr(llm_tts, "_model", None)


class _FakeXttsModel:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def tts_to_file(self, *, text, language, speaker_wav, file_path):
        self.calls.append(
            {"text": text, "language": language, "speaker_wav": speaker_wav, "file_path": file_path}
        )
        Path(file_path).write_bytes(b"fake-wav-bytes")


def _cfg(tmp_path, *, reference_exists: bool = True) -> LlmConfig:
    ref = tmp_path / "alfred.wav"
    if reference_exists:
        ref.write_bytes(b"fake-reference")
    return LlmConfig(
        tts_tmp_dir=tmp_path / "tts-tmp",
        tts_model_dir=tmp_path / "tts-models",
        tts_reference_voice_path=ref,
    )


def _fake_wav_to_ogg_factory(payload: bytes):
    async def _fake(wav_path: Path, ogg_path: Path, cfg: LlmConfig) -> None:
        ogg_path.write_bytes(payload)

    return _fake


async def test_synthesize_speech_calls_model_and_cleans_temp_files(tmp_path, monkeypatch):
    model = _FakeXttsModel()

    async def fake_get_model(cfg):
        return model

    monkeypatch.setattr(llm_tts, "_get_model", fake_get_model)
    monkeypatch.setattr(llm_tts, "_wav_to_ogg", _fake_wav_to_ogg_factory(b"fake-ogg-bytes"))

    cfg = _cfg(tmp_path)
    audio = await llm_tts.synthesize_speech("Добрый вечер, сэр.", cfg)

    assert audio == b"fake-ogg-bytes"
    assert len(model.calls) == 1
    call = model.calls[0]
    assert call["text"] == "Добрый вечер, сэр."
    assert call["language"] == cfg.tts_language
    assert call["speaker_wav"] == str(cfg.tts_reference_voice_path)
    # Временные wav/ogg удалены после синтеза — каталог остался пустым.
    assert list(cfg.tts_tmp_dir.iterdir()) == []


async def test_synthesize_speech_missing_reference_raises(tmp_path):
    cfg = _cfg(tmp_path, reference_exists=False)
    with pytest.raises(RuntimeError, match="референсный голос не найден"):
        await llm_tts.synthesize_speech("текст", cfg)


async def test_synthesize_speech_cleans_temp_files_even_on_ffmpeg_failure(tmp_path, monkeypatch):
    model = _FakeXttsModel()

    async def fake_get_model(cfg):
        return model

    async def fake_wav_to_ogg_fail(wav_path, ogg_path, cfg):
        raise RuntimeError("ffmpeg упал")

    monkeypatch.setattr(llm_tts, "_get_model", fake_get_model)
    monkeypatch.setattr(llm_tts, "_wav_to_ogg", fake_wav_to_ogg_fail)

    cfg = _cfg(tmp_path)
    with pytest.raises(RuntimeError, match="ffmpeg упал"):
        await llm_tts.synthesize_speech("текст", cfg)

    assert list(cfg.tts_tmp_dir.iterdir()) == []


async def test_get_model_loads_once_and_caches(tmp_path, monkeypatch):
    calls = []

    def fake_load(cfg):
        calls.append(cfg)
        return _FakeXttsModel()

    monkeypatch.setattr(llm_tts, "_load_model_sync", fake_load)

    cfg = _cfg(tmp_path)
    m1 = await llm_tts._get_model(cfg)
    m2 = await llm_tts._get_model(cfg)

    assert m1 is m2
    assert len(calls) == 1
