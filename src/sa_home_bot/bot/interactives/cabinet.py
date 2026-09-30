"""Кабинет Альфреда (Этап 49.2) — единственная локация сцены радио и место,
где Альфред находится вне интерактивов («скинь фото» — снимок кабинета сейчас).

Три слоя (решение пользователя 2026-09-30):

- **канон** — ниже, общий для всех: что кабинет есть и что в нём всегда;
- **особенности гостя** — у каждого гостя свой кабинет: детали придумывает
  Ведущий по нестрогому промпту, строгих списков вариантов не храним; новые
  дописываются с учётом прежних и не противоречат им;
- **переменное** — время суток и погода в Трансильвании (transylvania.py),
  не хранится, считается при каждом снимке.

Хранение — ``app_state`` ``location:cabinet:<гость>`` (тот же паттерн, что
у интерактивов, base.py). Снимки рисуются каждый раз заново — комнате можно
немного отличаться, главное чтобы детали были в кадре и узнавались (решение
пользователя 2026-09-30); повторный показ — только если ничего не
изменилось (тот же свет/погода и те же особенности).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sa_home_bot.db.store import Store

LOCATION = "cabinet"
FEATURES_MAX = 12
FEATURE_MAX_CHARS = 140
FIRST_FEATURES = 3
PHOTOS_KEEP = 8

CANON_RU = (
    "Кабинет Альфреда — старого дворецкого — в замке в Трансильвании: "
    "письменный стол, на нём старый радиопередатчик, каменный камин, "
    "книжные полки, высокое стрельчатое окно."
)
# Для снимка: коротко, самое узнаваемое первым — промпт ограничен 77 токенами.
CANON_EN = "old butler study in a transylvanian castle, wooden desk, stone fireplace, bookshelves"


@dataclass
class Cabinet:
    user_id: int
    features: list[str] = field(default_factory=list)
    # Снимки по набору состояния (свет/погода + особенности) → id картинки:
    # тот же набор — повторный показ без генерации.
    photos: dict[str, int] = field(default_factory=dict)

    def add(self, new: list[str]) -> list[str]:
        """Дописать особенности; дубликаты и пустое — мимо. Лимит — старые
        уходят первыми. Возвращает реально добавленные."""
        known = {f.casefold() for f in self.features}
        added = []
        for raw in new:
            text = " ".join(str(raw).split())[:FEATURE_MAX_CHARS].rstrip(" .")
            if not text or text.casefold() in known:
                continue
            known.add(text.casefold())
            self.features.append(text)
            added.append(text)
        del self.features[:-FEATURES_MAX]
        return added

    def describe_ru(self) -> str:
        if not self.features:
            return CANON_RU
        return CANON_RU + " Особенности: " + "; ".join(self.features) + "."

    def state_key(self, outside_key: str) -> str:
        digest = hashlib.sha1("\n".join(self.features).encode()).hexdigest()[:10]
        return f"{outside_key}#{digest}"

    def remember_photo(self, key: str, image_id: int) -> None:
        self.photos.pop(key, None)
        self.photos[key] = image_id
        for old in list(self.photos)[:-PHOTOS_KEEP]:
            del self.photos[old]


def _key(user_id: int) -> str:
    return f"location:{LOCATION}:{user_id}"


async def load(store: Store, user_id: int) -> Cabinet:
    raw = await store.get_state(_key(user_id))
    if raw:
        try:
            data: dict[str, Any] = json.loads(raw)
            return Cabinet(
                user_id=user_id,
                features=[str(f) for f in data.get("features") or []],
                photos={str(k): int(v) for k, v in (data.get("photos") or {}).items()},
            )
        except (ValueError, TypeError, AttributeError):
            pass
    return Cabinet(user_id=user_id)


async def save(store: Store, cabinet: Cabinet) -> None:
    await store.set_state(_key(cabinet.user_id), json.dumps(asdict(cabinet), ensure_ascii=False))


# --- тул take_photo (engine.Interactives.tool_take_photo) ---

TAKE_PHOTO_DECLARATION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "take_photo",
        "description": (
            "Сфотографировать то, что ты сейчас видишь у себя в кабинете, и "
            "прислать собеседнику: когда он просит фото («скинь фото», «покажи, "
            "что у тебя там», «как там твой кабинет») или когда рядом происходит "
            "что-то, что стоит показать. Снимок придёт сам примерно через "
            "полминуты — скажи, что сейчас снимешь, и не описывай его заранее "
            "подробно. Нарисовать что-то по просьбе — это generate_image, не этот тул."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": (
                        "Что снять крупно, своими словами (например «передатчик "
                        "на столе», «камин»). Пусто — общий вид кабинета."
                    ),
                },
                "caption": {
                    "type": "string",
                    "description": "Короткая подпись к снимку по-русски, 1-5 слов",
                },
            },
        },
    },
}

TOOL_PHOTO_STARTED = (
    "Снимок делается и придёт собеседнику сам через полминуты. Скажи коротко, "
    "что сейчас снимешь. Где ты: {where} Сейчас {now}."
)
TOOL_PHOTO_SENT = (
    "Снимок кабинета уже отправлен собеседнику (ничего не изменилось с прошлого). "
    "Где ты: {where} Сейчас {now}."
)
TOOL_PHOTO_BUSY = "Прошлый снимок ещё проявляется — скажи, что он вот-вот будет."
TOOL_PHOTO_LIMIT = "Плёнка на сегодня кончилась — скажи, что снимешь завтра."
TOOL_PHOTO_UNAVAILABLE = "недоступно: отсюда снимок не прислать"
