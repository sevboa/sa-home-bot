from __future__ import annotations

import random
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from sa_home_bot.utils.files import load_model, save_model

if TYPE_CHECKING:
    from sa_home_bot.config import LlmConfig

_WORD_RE = re.compile(r"[А-Яа-яЁё]+")
_HAS_R_RE = re.compile(r"[рР]")
_CORRUPT_MAP = str.maketrans({"р": "г", "Р": "Г"})

_WORDS_PER_VISIT = 20
_VISIT_CHANCE = 0.5
_VISIT_PROBABILITY_PER_WORD = _VISIT_CHANCE / _WORDS_PER_VISIT
_PROBABILITY_STEP = 0.001

_REMARK_TEMPLATES: tuple[str, ...] = (
    "Не «{corrupt}», а «{word}»!",
    "«{corrupt}»? Р-р-р-р-р! Ещё раз: «{word}»!",
    "Не «гы», а «р-р-р»! Повторите: «{word}», не «{corrupt}»!",
    "Стоп-стоп-стоп. «{corrupt}» — это позор. «{word}» — вот так, через нёбо!",
    "Язык дрожит, а не проваливается! «{word}», а не «{corrupt}»!",
    "Скажите вместе со мной: р-р-р-р-«{word}»! Не «{corrupt}»!",
    "«{corrupt}» — это уже не смешно. Пробуем «{word}» ещё раз.",
    "Раскат нужен, а не «гы»! «{word}», а не «{corrupt}»!",
    "Опять «{corrupt}»! Зубы почти сомкнуты, воздух через язык — «{word}»!",
    "Не глотайте букву! «{word}», а не куцее «{corrupt}»!",
    "Кончик языка к нёбу — и «{word}», не «{corrupt}»!",
    "Ай-яй-яй, «{corrupt}». Повторим по слогам: «{word}»!",
    "Р-р-р-р! Слышите вибрацию? Вот так «{word}», а не «{corrupt}»!",
    "Скажите «р-р-р-р-р»! Не «гы», а «р-р-р-р-р»!",
    "Скажите «эрррр»! Не «гы», а раскатисто!",
    "Так, тр-р-р-ренируем язык, а не филоним!",
    "Одна вибрация — не считается! Дайте раскат подлиннее!",
    "Рычим, а не гыкаем! Ещё раз, с чувством!",
    "Кончик языка — вверх, воздух — через середину. И р-р-р-р!",
    "Не глотайте букву, милейший! Р-р-р-р-развёрнуто!",
    "Опять сачкуете? Тр-р-р-р, и никаких «гы»!",
    "Язык дрожит, а не проваливается назад! Повторить упражнение!",
    "Ну что за «гы»! Р-р-р-р-р, с расстановкой!",
    "Домашнее задание не делали, я смотрю. Р-р-р-р-р, ещё десять раз!",
    "Зубы сомкнуты, губы расслаблены — и раскатисто: р-р-р-р!",
    "Не спешите глотать звук — тяните: р-р-р-р-р-р!",
    "Тр-р-р-евога! Опять мимо. Сначала!",
    "Хвалю за старание, но это было «гы». Нужно «р-р-р»!",
    "Ещё раз, и на этот раз без «гыканья»!",
)


def _corrupt_word(word: str) -> str:
    return word.translate(_CORRUPT_MAP)


def _pick_remark(rand_value: float, word: str) -> str:
    idx = min(len(_REMARK_TEMPLATES) - 1, int(rand_value * len(_REMARK_TEMPLATES)))
    body = _REMARK_TEMPLATES[idx].format(corrupt=_corrupt_word(word), word=word)
    return f"🗣 <b>Логопед:</b> <i>{body}</i>"


class SpeechTherapyState(BaseModel):
    error_probability: float = 1.0
    corrections_total: int = 0
    excluded_words: list[str] = Field(default_factory=list)
    cured: bool = False
    clear_user_ids: list[int] = Field(default_factory=list)


class SpeechTherapist:
    def __init__(self, cfg: LlmConfig, *, rand: Callable[[], float] = random.random) -> None:
        self._cfg = cfg
        self._rand = rand
        self._state = load_model(SpeechTherapyState, cfg.speech_therapy_state_path)

    def snapshot(self) -> dict[str, Any]:
        return {
            "error_probability": self._state.error_probability,
            "corrections_total": self._state.corrections_total,
            "cured": self._state.cured,
            "clear_users": len(self._state.clear_user_ids),
        }

    def is_clear(self, user_id: int) -> bool:
        return user_id in self._state.clear_user_ids

    def set_clear(self, user_id: int, clear: bool) -> bool:
        if clear == self.is_clear(user_id):
            return False
        if clear:
            self._state.clear_user_ids.append(user_id)
        else:
            self._state.clear_user_ids.remove(user_id)
        save_model(self._state, self._cfg.speech_therapy_state_path)
        return True

    def process(
        self, text: str, chat_id: int | None, user_id: int | None = None
    ) -> tuple[str, str | None, bool]:
        pinned = chat_id is not None and chat_id in self._cfg.speech_therapy_pinned_chat_ids
        if not pinned and self._state.cured:
            return text, None, False
        speaker = user_id if user_id is not None else chat_id
        if not pinned and speaker is not None and self.is_clear(speaker):
            return text, None, False

        result = list(text)
        visited = False
        just_cured = False
        remark: str | None = None
        changed = False

        for m in _WORD_RE.finditer(text):
            word = m.group(0)
            if not _HAS_R_RE.search(word):
                continue
            lower = word.lower()

            if pinned:
                result[m.start() : m.end()] = _corrupt_word(word)
                changed = True
                continue

            excluded = lower in self._state.excluded_words
            if not excluded and self._rand() < self._state.error_probability:
                result[m.start() : m.end()] = _corrupt_word(word)
                changed = True

            if not visited and not excluded and self._rand() < _VISIT_PROBABILITY_PER_WORD:
                visited = True
                changed = True
                self._state.excluded_words.append(lower)
                self._state.error_probability = round(
                    max(0.0, self._state.error_probability - _PROBABILITY_STEP), 6
                )
                self._state.corrections_total += 1
                remark = _pick_remark(self._rand(), word)
                if self._state.error_probability <= 0.0 and not self._state.cured:
                    self._state.cured = True
                    just_cured = True

        if changed:
            save_model(self._state, self._cfg.speech_therapy_state_path)

        final_text = "".join(result)
        return final_text, remark, just_cured
