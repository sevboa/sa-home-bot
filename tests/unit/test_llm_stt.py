"""Распознавание голосовых (llm/stt.py) — faster-whisper замокан целиком:
модуль не тянется как зависимость теста, проверяем только нашу обвязку
(временный файл создаётся/удаляется, сегменты склеиваются, модель
кэшируется по процессу)."""

from __future__ import annotations

import pytest

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm import stt as llm_stt


class _FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModel:
    def __init__(self, segments, *, raise_on_transcribe: Exception | None = None) -> None:
        self._segments = segments
        self._raise = raise_on_transcribe
        self.calls: list[str] = []

    def transcribe(self, path, language=None, vad_filter=None):
        self.calls.append(path)
        if self._raise is not None:
            raise self._raise
        return self._segments, {"language": language}


def _cfg(tmp_path) -> LlmConfig:
    return LlmConfig(
        stt_tmp_dir=tmp_path / "stt-tmp",
        stt_model_dir=tmp_path / "stt-models",
    )


async def test_transcribe_voice_joins_segment_text_and_removes_temp_file(tmp_path, monkeypatch):
    model = _FakeModel([_FakeSegment("привет "), _FakeSegment(" мир"), _FakeSegment("")])

    async def fake_get_model(cfg):
        return model

    monkeypatch.setattr(llm_stt._model, "get", fake_get_model)

    cfg = _cfg(tmp_path)
    text = await llm_stt.transcribe_voice(b"fake-ogg-bytes", cfg)

    assert text == "привет мир"
    assert len(model.calls) == 1
    # Временный файл удалён после транскрипции — каталог остался пустым.
    assert list(cfg.stt_tmp_dir.iterdir()) == []


async def test_transcribe_voice_empty_segments_returns_empty_string(tmp_path, monkeypatch):
    model = _FakeModel([])

    async def fake_get_model(cfg):
        return model

    monkeypatch.setattr(llm_stt._model, "get", fake_get_model)

    text = await llm_stt.transcribe_voice(b"...", _cfg(tmp_path))
    assert text == ""


async def test_transcribe_voice_removes_temp_file_even_on_exception(tmp_path, monkeypatch):
    model = _FakeModel([], raise_on_transcribe=RuntimeError("битый файл"))

    async def fake_get_model(cfg):
        return model

    monkeypatch.setattr(llm_stt._model, "get", fake_get_model)

    cfg = _cfg(tmp_path)
    with pytest.raises(RuntimeError):
        await llm_stt.transcribe_voice(b"...", cfg)

    assert list(cfg.stt_tmp_dir.iterdir()) == []
