"""Логопед: вероятностная, излечимая картавость Альфреда (р→г/Р→Г).

Заменяет прежнюю безусловную побуквенную замену (`prompt.py::apply_speech_defect`,
удалена) на стейтфул-механику. Состояние — общее для процесса службы llm:
вероятность искажения слова с «р» стартует со 100% и снижается на 0.1% за
каждую «коррекцию» логопеда, пока не дойдёт до 0% (полное излечение,
разовое — назад не возвращается). Коррекция выбранного слова также
навсегда исключает его из будущих искажений.

Единица счёта — слово (`[А-Яа-яЁё]+`), не буква. На каждом слове-кандидате
с «р» (кроме уже исключённых) — независимая проверка визита логопеда с
фиксированной вероятностью `_VISIT_PROBABILITY_PER_WORD`, не более одного
визита за вызов `process()` (= одно сообщение). Эта вероятность НЕ зависит
от текущей error_probability — иначе процесс самозамедляется: чем ниже
вероятность ошибки, тем реже случаются искажения, тем реже накапливался бы
порог визита, и полное излечение потребовало бы порядка сотен тысяч слов
(посчитано и согласовано с пользователем как непрактичное). Константы ниже
подобраны как эквивалент исходной идеи «каждые 20 слов — 50% шанс визита»,
но без переносимого между сообщениями счётчика.

Закреплённые (pinned) чаты (`LlmConfig.speech_therapy_pinned_chat_ids`) —
искажают ВСЕ слова с «р» всегда, включая уже исключённые глобально; не
участвуют в общем прогрессе лечения и не читают/не пишут `excluded_words`/
`error_probability`/`cured`."""

from __future__ import annotations

import contextlib
import os
import random
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from sa_home_bot.config import LlmConfig

_WORD_RE = re.compile(r"[А-Яа-яЁё]+")
_HAS_R_RE = re.compile(r"[рР]")
_CORRUPT_MAP = str.maketrans({"р": "г", "Р": "Г"})

# Подобраны эмпирически (согласовано с пользователем 2026-08-03): исходная
# идея «каждые 20 слов — 50% шанс визита логопеда» без переносимого
# счётчика превращается в независимую проверку на каждом слове-кандидате.
_WORDS_PER_VISIT = 20
_VISIT_CHANCE = 0.5
_VISIT_PROBABILITY_PER_WORD = _VISIT_CHANCE / _WORDS_PER_VISIT  # 0.025
_PROBABILITY_STEP = 0.001  # 1000 коррекций 100% → 0%

# Фразы ремарки на визит логопеда — карикатура на придирчивого специалиста
# (решение пользователя 2026-08-04, по мотивам «скажи р-р-р-р, скажи эррр,
# не гыыы»). Первые — привязаны к конкретному слову ({corrupt}/{word} — как
# вышло/как надо), дальше — общие придирки без слова. ``.format()`` безопасен
# для обеих категорий: шаблоны без плейсхолдеров просто игнорируют лишние
# kwargs. Индекс [0] — канонiчная формулировка (см. тесты, детерминированные
# на rand=0.0 всегда попадают именно в неё).
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
    # Имя — жирным, как у остальных персонажей (bot/handlers/ai.py::
    # ALFRED_PREFIX = "<b>Альфред:</b> "), сама реплика — курсивом (решение
    # пользователя 2026-08-04).
    return f"🗣 <b>Логопед:</b> <i>{body}</i>"


class SpeechTherapyState(BaseModel):
    error_probability: float = 1.0
    corrections_total: int = 0
    excluded_words: list[str] = Field(default_factory=list)
    cured: bool = False

    @classmethod
    def load(cls, path: str | Path) -> SpeechTherapyState:
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.model_validate_json(p.read_bytes())

    def save(self, path: str | Path) -> None:
        """Атомарная запись: temp-файл в том же каталоге + `os.replace`."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(self.model_dump_json(indent=2))
            os.replace(tmp_name, p)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise


class SpeechTherapist:
    def __init__(self, cfg: LlmConfig, *, rand: Callable[[], float] = random.random) -> None:
        self._cfg = cfg
        self._rand = rand
        self._state = SpeechTherapyState.load(cfg.speech_therapy_state_path)

    def snapshot(self) -> dict[str, Any]:
        return {
            "error_probability": self._state.error_probability,
            "corrections_total": self._state.corrections_total,
            "cured": self._state.cured,
        }

    def process(self, text: str, chat_id: int | None) -> tuple[str, str | None, bool]:
        """Синхронно (см. докстринг модуля — никакого await внутри, это
        инвариант атомарности между параллельными вызовами run_command).

        Возвращает (искажённый текст БЕЗ ремарки логопеда, ремарка визита
        или None, только что вылечился ли Альфред этим вызовом — True ровно
        на переходе cured False→True). Ремарка отдаётся отдельно от текста —
        вызывающий (llm/service.py) решает, что с ней делать; раньше она
        дописывалась в тот же текст, что ломало HTML-форматирование выше по
        стеку (bot/handlers/ai.py экранирует ВЕСЬ текст персонажа как plain
        text) и превращало ремарку в хвост одного с ответом сообщения вместо
        отдельной реплики (решение пользователя 2026-08-03)."""
        pinned = chat_id is not None and chat_id in self._cfg.speech_therapy_pinned_chat_ids
        if not pinned and self._state.cured:
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
            self._state.save(self._cfg.speech_therapy_state_path)

        final_text = "".join(result)
        return final_text, remark, just_cured
