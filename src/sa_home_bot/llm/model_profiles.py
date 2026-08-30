"""Профили моделей — контракт рассуждения, подключаемый вместе с моделью.

Раньше знание «как у этой модели включается рассуждение» приходилось руками
разносить по четырём связанным полям ``[llm]`` в конфиге бота
(``mode`` / ``think_style`` / ``single_call_think`` / ``think_chat``), хотя
крутится модель на другой машине. Теперь служба llm по имени своей модели
подтягивает профиль из ``model-profiles.toml`` (пакетный + локальный override
рядом с ``config.toml``) и сама переводит намерение-уровень рассуждения
(``off`` / ``low`` / ``medium`` / ``high``, его выдаёт router-проход —
llm/prompt.py) в параметр Ollama ``think``.

Формат TOML и смысл полей — в шапке ``model-profiles.toml``.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

# Порядок по возрастанию «глубины»: индекс = уровень, который выдаёт router.
REASON_LEVELS: tuple[str, ...] = ("off", "low", "medium", "high")

_THINK_CONTROLS = frozenset({"flag", "flag_implicit_on", "effort", "none"})

# Жёсткий фоллбэк на случай, если в файле почему-то нет записи _default
# (битый локальный override, обрезанный пакет).
_HARDCODED_DEFAULT = "_default"


@dataclass(frozen=True)
class ModelProfile:
    """Один профиль. ``match`` пуст только у фоллбэк-записи ``_default``."""

    name: str
    match: tuple[str, ...] = ()
    think_control: str = "flag"
    router: bool = True
    num_ctx: int = 8192
    thinking_hidden: bool = False
    notes: str = ""

    def think_arg(self, reason: str) -> bool | str | None:
        """Значение поля ``think`` запроса к Ollama для намерения ``reason``.

        ``None`` — ключ ``think`` не отправлять вовсе (не то же, что ``False``:
        см. живую находку про скрытое рассуждение gemma, config.py).
        """
        on = reason != "off"
        ctl = self.think_control
        if ctl == "none":
            return None
        if ctl == "flag_implicit_on":
            # off — явный False (глушит скрытое рассуждение); on — не мешаем
            # модели флагом (уровень она всё равно не понимает, а явный
            # think=true отвергает с 400).
            return None if on else False
        if ctl == "effort":
            # Уровень уходит как есть строкой; off → быстрый проход без reasoning.
            return reason if on else False
        # "flag" и любой нераспознанный контроль — обычный булев тумблер.
        return on

    def summary(self) -> ModelProfileSummary:
        return ModelProfileSummary(
            name=self.name,
            router=self.router,
            thinking_hidden=self.thinking_hidden,
            num_ctx=self.num_ctx,
        )


@dataclass(frozen=True)
class ModelProfileSummary:
    """Урезанная сводка профиля — едет боту в ``describe()`` (proto/messages.py).

    Боту от профиля нужно ровно две вещи: нужен ли router-проход и уводит ли
    модель рассуждение в невидимый буфер (для UX «задумался»). Перевод
    уровня в параметр Ollama остаётся на стороне службы.
    """

    name: str
    router: bool
    thinking_hidden: bool
    num_ctx: int

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "router": self.router,
            "thinking_hidden": self.thinking_hidden,
            "num_ctx": self.num_ctx,
        }

    @classmethod
    def from_payload(cls, raw: dict[str, object]) -> ModelProfileSummary:
        return cls(
            name=str(raw["name"]),
            router=bool(raw.get("router", True)),
            thinking_hidden=bool(raw.get("thinking_hidden", False)),
            num_ctx=int(raw.get("num_ctx", 8192)),  # type: ignore[arg-type]
        )


@dataclass
class _Registry:
    profiles: list[ModelProfile] = field(default_factory=list)

    def resolve(self, model: str) -> tuple[ModelProfile, bool]:
        """(профиль, matched). matched=False → сработал фоллбэк ``_default``."""
        needle = model.lower()
        for p in self.profiles:
            if p.match and any(m.lower() in needle for m in p.match):
                return p, True
        for p in self.profiles:
            if not p.match:
                return p, False
        return ModelProfile(name=_HARDCODED_DEFAULT), False


def _profile_from_raw(raw: dict[str, object], *, source: str) -> ModelProfile | None:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        log.warning("model-profiles (%s): запись без name — пропущена", source)
        return None
    control = str(raw.get("think_control", "flag"))
    if control not in _THINK_CONTROLS:
        log.warning(
            "model-profiles (%s): профиль %r — неизвестный think_control %r, беру 'flag'",
            source, name, control,
        )
        control = "flag"
    match_raw = raw.get("match", [])
    match = tuple(str(m) for m in match_raw) if isinstance(match_raw, list) else ()
    return ModelProfile(
        name=name,
        match=match,
        think_control=control,
        router=bool(raw.get("router", True)),
        num_ctx=int(raw.get("num_ctx", 8192)),  # type: ignore[arg-type]
        thinking_hidden=bool(raw.get("thinking_hidden", False)),
        notes=str(raw.get("notes", "")),
    )


def _parse(text: str, *, source: str) -> list[ModelProfile]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        log.warning("model-profiles (%s): не разобрать TOML — игнорирую (%s)", source, exc)
        return []
    entries = data.get("profile", [])
    if not isinstance(entries, list):
        log.warning("model-profiles (%s): нет массива [[profile]] — игнорирую", source)
        return []
    out: list[ModelProfile] = []
    for raw in entries:
        if isinstance(raw, dict):
            parsed = _profile_from_raw(raw, source=source)
            if parsed is not None:
                out.append(parsed)
    return out


def _packaged_text() -> str:
    return (
        resources.files("sa_home_bot.llm")
        .joinpath("model-profiles.toml")
        .read_text(encoding="utf-8")
    )


def _merge(base: list[ModelProfile], extra: list[ModelProfile]) -> list[ModelProfile]:
    """Локальные записи: с тем же name — заменяют, новые — перед ``_default``."""
    merged = list(base)
    by_name = {p.name: i for i, p in enumerate(merged)}
    default_at = next((i for i, p in enumerate(merged) if not p.match), len(merged))
    for p in extra:
        if p.name in by_name:
            merged[by_name[p.name]] = p
        else:
            merged.insert(default_at, p)
            default_at += 1
    return merged


def load_profiles(local_path: Path | None = None) -> _Registry:
    """Пакетные профили + (если есть) локальный ``model-profiles.toml``."""
    profiles = _parse(_packaged_text(), source="пакет")
    if local_path is not None and local_path.exists():
        local = _parse(local_path.read_text(encoding="utf-8"), source=str(local_path))
        if local:
            profiles = _merge(profiles, local)
            log.info(
                "model-profiles: локальный %s — %d записей", local_path, len(local)
            )
    return _Registry(profiles=profiles)
