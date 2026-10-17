from __future__ import annotations

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm.speech_therapy import SpeechTherapist, SpeechTherapyState
from sa_home_bot.utils.files import load_model, save_model

_CORRUPT_ONLY = 0.5
_ALWAYS = 0.0
_NEVER = 1.0


def _cfg(tmp_path, **overrides) -> LlmConfig:
    overrides.setdefault("speech_therapy_state_path", str(tmp_path / "speech-therapy.json"))
    return LlmConfig(model="qwen2.5:7b", **overrides)


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
    save_model(SpeechTherapyState(excluded_words=["роман"]), path)
    cfg = _cfg(tmp_path, speech_therapy_pinned_chat_ids=[99])
    cfg = cfg.model_copy(update={"speech_therapy_state_path": str(path)})
    therapist = SpeechTherapist(cfg, rand=lambda: _NEVER)
    text, _, _ = therapist.process("Роман", chat_id=99)
    assert text == "Гоман"


def test_excluded_word_is_never_corrupted_again(tmp_path):
    path = tmp_path / "state.json"
    save_model(SpeechTherapyState(excluded_words=["роман"]), path)
    cfg = _cfg(tmp_path).model_copy(update={"speech_therapy_state_path": str(path)})
    therapist = SpeechTherapist(cfg, rand=lambda: _ALWAYS)
    text, _, _ = therapist.process("сэр Роман", chat_id=1)
    assert text.startswith("сэг Роман")


def test_visit_registers_correction_and_excludes_word(tmp_path):
    cfg = _cfg(tmp_path)
    therapist = SpeechTherapist(cfg, rand=lambda: _ALWAYS)
    text, remark, just_cured = therapist.process("сэр", chat_id=1)
    assert text == "сэг"
    assert remark == "🗣 <b>Логопед:</b> <i>Не «сэг», а «сэр»!</i>"
    assert just_cured is False

    snapshot = therapist.snapshot()
    assert snapshot["corrections_total"] == 1
    assert snapshot["error_probability"] == 0.999

    loaded = load_model(SpeechTherapyState, cfg.speech_therapy_state_path)
    assert loaded.excluded_words == ["сэр"]


def test_at_most_one_visit_per_message(tmp_path):
    cfg = _cfg(tmp_path)
    therapist = SpeechTherapist(cfg, rand=lambda: _ALWAYS)
    text, remark, _ = therapist.process("сэр Роман работал", chat_id=1)
    assert "🗣" not in text
    assert remark is not None
    assert remark.count("🗣") == 1
    assert therapist.snapshot()["corrections_total"] == 1


def test_becomes_cured_after_thousand_corrections(tmp_path):
    path = tmp_path / "state.json"
    save_model(SpeechTherapyState(error_probability=0.001, corrections_total=999), path)
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
    save_model(SpeechTherapyState(error_probability=0.001, corrections_total=999), path)
    cfg = _cfg(tmp_path).model_copy(update={"speech_therapy_state_path": str(path)})
    therapist = SpeechTherapist(cfg, rand=lambda: _ALWAYS)

    _, _, first = therapist.process("сэр", chat_id=1)
    assert first is True

    _, _, second = therapist.process("другое сообщение с рекой", chat_id=1)
    assert second is False


def test_cured_chat_no_longer_corrupted(tmp_path):
    path = tmp_path / "state.json"
    save_model(SpeechTherapyState(cured=True), path)
    cfg = _cfg(tmp_path).model_copy(update={"speech_therapy_state_path": str(path)})
    therapist = SpeechTherapist(cfg, rand=lambda: _ALWAYS)

    text, remark, just_cured = therapist.process("сэр Роман", chat_id=1)

    assert text == "сэр Роман"
    assert remark is None
    assert just_cured is False


def test_cured_pinned_chat_still_corrupted(tmp_path):
    path = tmp_path / "state.json"
    save_model(SpeechTherapyState(cured=True), path)
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


def test_all_remark_templates_format_cleanly_and_differ():
    from sa_home_bot.llm.speech_therapy import _REMARK_TEMPLATES, _pick_remark

    seen = set()
    for i in range(len(_REMARK_TEMPLATES)):
        remark = _pick_remark((i + 0.5) / len(_REMARK_TEMPLATES), "робот")
        assert remark.startswith("🗣 <b>Логопед:</b> <i>")
        assert remark.endswith("</i>")
        assert "{corrupt}" not in remark
        assert "{word}" not in remark
        seen.add(remark)
    assert len(seen) == len(_REMARK_TEMPLATES)


def test_text_without_r_words_is_untouched_and_state_not_saved(tmp_path):
    path = tmp_path / "state.json"
    cfg = _cfg(tmp_path).model_copy(update={"speech_therapy_state_path": str(path)})
    therapist = SpeechTherapist(cfg, rand=lambda: _ALWAYS)
    text, remark, just_cured = therapist.process("спасибо всем", chat_id=1)
    assert text == "спасибо всем"
    assert remark is None
    assert just_cured is False
    assert not path.exists()


def test_clear_user_gets_clean_text_and_no_remark(tmp_path):
    therapist = SpeechTherapist(_cfg(tmp_path), rand=lambda: _ALWAYS)
    therapist.set_clear(42, True)
    assert therapist.process("программа работает", chat_id=-100, user_id=42) == (
        "программа работает",
        None,
        False,
    )
    assert therapist.snapshot()["corrections_total"] == 0


def test_clear_falls_back_to_chat_id_without_user_id(tmp_path):
    therapist = SpeechTherapist(_cfg(tmp_path), rand=lambda: _CORRUPT_ONLY)
    therapist.set_clear(42, True)
    assert therapist.process("пора", chat_id=42)[0] == "пора"
    assert therapist.process("пора", chat_id=7)[0] == "пога"


def test_clear_is_per_user_in_group_chat(tmp_path):
    therapist = SpeechTherapist(_cfg(tmp_path), rand=lambda: _CORRUPT_ONLY)
    therapist.set_clear(42, True)
    assert therapist.process("пора", chat_id=-100, user_id=42)[0] == "пора"
    assert therapist.process("пора", chat_id=-100, user_id=7)[0] == "пога"


def test_pinned_chat_beats_clear_user(tmp_path):
    therapist = SpeechTherapist(
        _cfg(tmp_path, speech_therapy_pinned_chat_ids=[42]), rand=lambda: _NEVER
    )
    therapist.set_clear(42, True)
    assert therapist.process("пора", chat_id=42, user_id=42)[0] == "пога"


def test_set_clear_is_idempotent_and_persisted(tmp_path):
    cfg = _cfg(tmp_path)
    therapist = SpeechTherapist(cfg)
    assert therapist.set_clear(42, True) is True
    assert therapist.set_clear(42, True) is False
    assert SpeechTherapist(cfg).is_clear(42) is True
    assert therapist.set_clear(42, False) is True
    assert SpeechTherapist(cfg).is_clear(42) is False
