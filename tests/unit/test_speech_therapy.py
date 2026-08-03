"""SpeechTherapist/SpeechTherapyState — вероятностная, излечимая картавость
Альфреда («Логопед», llm/speech_therapy.py). Все проверки на детерминированном
``rand``, чтобы не зависеть от настоящего random.random()."""

from __future__ import annotations

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm.speech_therapy import SpeechTherapist, SpeechTherapyState

# 0.5 < error_probability=1.0 (искажает), но >= _VISIT_PROBABILITY_PER_WORD=0.025
# (визит не срабатывает) — детерминированное «только искажение, без визита».
_CORRUPT_ONLY = 0.5
_ALWAYS = 0.0  # < любого порога — искажает и всегда провоцирует визит
_NEVER = 1.0  # >= error_probability=1.0 (дефолт) — никогда не срабатывает


def _cfg(tmp_path, **overrides) -> LlmConfig:
    overrides.setdefault("speech_therapy_state_path", str(tmp_path / "speech-therapy.json"))
    return LlmConfig(model="qwen2.5:7b", **overrides)


def test_state_load_without_file_returns_defaults(tmp_path):
    state = SpeechTherapyState.load(tmp_path / "missing.json")
    assert state.error_probability == 1.0
    assert state.corrections_total == 0
    assert state.excluded_words == []
    assert state.cured is False


def test_state_save_load_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = SpeechTherapyState(
        error_probability=0.5, corrections_total=500, excluded_words=["привет"], cured=False
    )
    state.save(path)
    loaded = SpeechTherapyState.load(path)
    assert loaded == state


def test_corrupts_word_when_probability_is_full(tmp_path):
    therapist = SpeechTherapist(_cfg(tmp_path), rand=lambda: _CORRUPT_ONLY)
    text, remark, just_cured = therapist.process("сэр Роман", chat_id=1)
    assert text == "сэг Гоман"
    assert remark is None
    assert just_cured is False


def test_never_corrupts_when_rand_above_probability(tmp_path):
    cfg = _cfg(tmp_path)
    therapist = SpeechTherapist(cfg, rand=lambda: _NEVER)
    text, remark, just_cured = therapist.process("сэр Роман", chat_id=1)
    assert text == "сэр Роман"
    assert remark is None
    assert just_cured is False


def test_pinned_chat_always_corrupts_regardless_of_state(tmp_path):
    cfg = _cfg(tmp_path, speech_therapy_pinned_chat_ids=[99])
    therapist = SpeechTherapist(cfg, rand=lambda: _NEVER)
    text, remark, just_cured = therapist.process("сэр Роман", chat_id=99)
    assert text == "сэг Гоман"
    assert remark is None
    assert just_cured is False


def test_pinned_chat_ignores_excluded_words(tmp_path):
    path = tmp_path / "state.json"
    SpeechTherapyState(excluded_words=["роман"]).save(path)
    cfg = _cfg(tmp_path, speech_therapy_pinned_chat_ids=[99])
    cfg = cfg.model_copy(update={"speech_therapy_state_path": str(path)})
    therapist = SpeechTherapist(cfg, rand=lambda: _NEVER)
    text, _, _ = therapist.process("Роман", chat_id=99)
    assert text == "Гоман"


def test_excluded_word_is_never_corrupted_again(tmp_path):
    path = tmp_path / "state.json"
    SpeechTherapyState(excluded_words=["роман"]).save(path)
    cfg = _cfg(tmp_path).model_copy(update={"speech_therapy_state_path": str(path)})
    therapist = SpeechTherapist(cfg, rand=lambda: _ALWAYS)
    text, _, _ = therapist.process("сэр Роман", chat_id=1)
    # "сэр" искажается (не в excluded), "Роман" — уже исключено, не трогаем.
    assert text.startswith("сэг Роман")


def test_visit_registers_correction_and_excludes_word(tmp_path):
    cfg = _cfg(tmp_path)
    therapist = SpeechTherapist(cfg, rand=lambda: _ALWAYS)
    text, remark, just_cured = therapist.process("сэр", chat_id=1)
    assert text == "сэг"
    assert remark is not None
    assert "🗣" in remark
    assert "не «сэг», а «сэр»!" in remark
    assert just_cured is False

    snapshot = therapist.snapshot()
    assert snapshot["corrections_total"] == 1
    assert snapshot["error_probability"] == 0.999

    loaded = SpeechTherapyState.load(cfg.speech_therapy_state_path)
    assert loaded.excluded_words == ["сэр"]


def test_at_most_one_visit_per_message(tmp_path):
    cfg = _cfg(tmp_path)
    therapist = SpeechTherapist(cfg, rand=lambda: _ALWAYS)
    text, remark, _ = therapist.process("сэр Роман работал", chat_id=1)
    assert "🗣" not in text  # ремарка не подмешивается в текст ответа
    assert remark is not None
    assert remark.count("🗣") == 1
    assert therapist.snapshot()["corrections_total"] == 1


def test_becomes_cured_after_thousand_corrections(tmp_path):
    path = tmp_path / "state.json"
    SpeechTherapyState(error_probability=0.001, corrections_total=999).save(path)
    cfg = _cfg(tmp_path).model_copy(update={"speech_therapy_state_path": str(path)})
    therapist = SpeechTherapist(cfg, rand=lambda: _ALWAYS)

    text, remark, just_cured = therapist.process("сэр", chat_id=1)

    assert remark is not None
    assert just_cured is True
    snapshot = therapist.snapshot()
    assert snapshot["cured"] is True
    assert snapshot["error_probability"] == 0.0
    assert snapshot["corrections_total"] == 1000


def test_just_cured_is_true_only_on_the_transition(tmp_path):
    path = tmp_path / "state.json"
    SpeechTherapyState(error_probability=0.001, corrections_total=999).save(path)
    cfg = _cfg(tmp_path).model_copy(update={"speech_therapy_state_path": str(path)})
    therapist = SpeechTherapist(cfg, rand=lambda: _ALWAYS)

    _, _, first = therapist.process("сэр", chat_id=1)
    assert first is True

    _, _, second = therapist.process("другое сообщение с рекой", chat_id=1)
    assert second is False


def test_cured_chat_no_longer_corrupted(tmp_path):
    path = tmp_path / "state.json"
    SpeechTherapyState(cured=True).save(path)
    cfg = _cfg(tmp_path).model_copy(update={"speech_therapy_state_path": str(path)})
    therapist = SpeechTherapist(cfg, rand=lambda: _ALWAYS)

    text, remark, just_cured = therapist.process("сэр Роман", chat_id=1)

    assert text == "сэр Роман"
    assert remark is None
    assert just_cured is False


def test_cured_pinned_chat_still_corrupted(tmp_path):
    path = tmp_path / "state.json"
    SpeechTherapyState(cured=True).save(path)
    cfg = _cfg(tmp_path, speech_therapy_pinned_chat_ids=[99])
    cfg = cfg.model_copy(update={"speech_therapy_state_path": str(path)})
    therapist = SpeechTherapist(cfg, rand=lambda: _NEVER)

    text, _, _ = therapist.process("сэр Роман", chat_id=99)

    assert text == "сэг Гоман"


def test_chat_id_none_behaves_as_regular_unpinned_chat(tmp_path):
    cfg = _cfg(tmp_path)
    therapist = SpeechTherapist(cfg, rand=lambda: _CORRUPT_ONLY)
    text, remark, just_cured = therapist.process("сэр", chat_id=None)
    assert text == "сэг"
    assert remark is None
    assert just_cured is False


def test_text_without_r_words_is_untouched_and_state_not_saved(tmp_path):
    path = tmp_path / "state.json"
    cfg = _cfg(tmp_path).model_copy(update={"speech_therapy_state_path": str(path)})
    therapist = SpeechTherapist(cfg, rand=lambda: _ALWAYS)
    text, remark, just_cured = therapist.process("спасибо всем", chat_id=1)
    assert text == "спасибо всем"
    assert remark is None
    assert just_cured is False
    assert not path.exists()
