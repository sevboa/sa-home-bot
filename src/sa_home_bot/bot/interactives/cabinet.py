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
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sa_home_bot.db.store import Store

LOCATION = "cabinet"
FEATURES_MAX = 12
# Сколько особенностей Альфред видит в описании «Где ты» (свежие, только
# видимые): длинный список превращал ответ тула в свалку.
DESCRIBE_MAX = 5
FEATURE_MAX_CHARS = 140
FIRST_FEATURES = 3
PHOTOS_KEEP = 8

CANON_RU = (
    "Кабинет Альфреда — старого дворецкого — в замке в Трансильвании: "
    "письменный стол, на нём старый радиопередатчик, каменный камин, "
    "книжные полки, высокое стрельчатое окно."
)
# Для снимка: коротко, самое узнаваемое первым — промпт ограничен 77 токенами.
# Без «castle»: промптер переносил его в промпт, и GhostMix+eldritch рисовал
# замок снаружи вместо стола (живая находка 2026-10-01, A/B 3 из 3).
# Цвета мебели — до камня: «black candlestick» + «gothic stone walls» в дневном
# свете давали почти серый кадр (кадр 286, 2026-10-02); «vibrant colors» из
# шаблона стоит в хвосте и не перевешивает. Стенд ~/refbench/grey на mycraft:
# насыщенность 0.13 → 0.39-0.86. Без «golden light» — он перебивал время суток.
CANON_EN = (
    "old butler study room, warm brown wooden desk, deep red carpet, "
    "gothic stone walls, stone fireplace, bookshelves"
)
# Снимкам кабинета — к негативу (служба дописывает к своему). При guidance 1.5
# негатив весит мало, но не мешает.
PHOTO_NEGATIVE_EN = "monochrome, grayscale"

# Портрет Альфреда к приветствию (2026-10-05): сам Альфред в своём кабинете —
# LoRA облика (llm/imagegen.py, LORAS["alfred"]). Вес — стенд 2026-10-05:
# 0.4 — лицо слабеет, 1.0 — всё тянет в крупный портрет и комната теряется.
ALFRED_LORA = ("alfred", 0.7)
PORTRAIT_SUBJECT_EN = (
    "Main subject: Alfred, an elderly butler in a black tailcoat, waist-up, "
    "standing in his study and greeting the viewer with a slight polite bow."
)
PORTRAIT_CAPTION = "Альфред в кабинете"

# Альфред в кадре события сцены (2026-10-05, решение владельца): участник
# происходящего, но СПИНОЙ или БОКОМ — лицо не главное (LoRA облика на лице
# тянет кадр в портрет). Ключ — поле "alfred" решения Ведущего. Фраза короткая:
# место в 77 токенах CLIP делят с событием и кабинетом.
SCENE_ALFRED_EN = {
    "back": "Alfred, an elderly butler in a black tailcoat, seen from behind, face hidden",
    "side": "Alfred, an elderly butler in a black tailcoat, in side profile, turned away",
}

# Чего не снять фотоаппаратом: запахи и звуки. Такие особенности Ведущему
# запрещены промптом, а уже записанные (и прорвавшиеся) не идут в кадр и в
# новые особенности — место в 77 токенах промпта снимка дорого.
_NON_VISUAL_RE = re.compile(
    r"запах|аромат|пахн|пахл|\bвон[ьяи]|зловон|смрад|благоухан|\bдух\b|"
    r"звук|звуч|звон|слыш|шёпот|шепот|шорох|хихик|скрип|тиши|стук|гул[ак]?\b",
    re.IGNORECASE,
)


def visible(text: str) -> bool:
    return not _NON_VISUAL_RE.search(text)


@dataclass
class Cabinet:
    user_id: int
    features: list[str] = field(default_factory=list)
    # Что появилось в кабинете по ходу сцены (cabinet_add Ведущего): живёт,
    # пока сцена идёт, и уходит вместе с ней — иначе после квеста кабинет
    # навсегда оставался в тумане, шёпоте и пепельных пальцах (живая
    # находка 2026-09-30).
    scene: list[str] = field(default_factory=list)
    # Снимки по набору состояния (свет/погода + особенности) → id картинки:
    # тот же набор — повторный показ без генерации.
    photos: dict[str, int] = field(default_factory=dict)

    def add(self, new: list[str], *, scene: bool = False) -> list[str]:
        """Дописать особенности; дубликаты и пустое — мимо. Лимит — старые
        уходят первыми. ``scene`` — только на время сцены. Возвращает
        реально добавленные."""
        target = self.scene if scene else self.features
        known = {f.casefold() for f in (*self.features, *self.scene)}
        added = []
        for raw in new:
            text = " ".join(str(raw).split())[:FEATURE_MAX_CHARS].rstrip(" .")
            if not text or text.casefold() in known or not visible(text):
                continue
            known.add(text.casefold())
            target.append(text)
            added.append(text)
        del target[:-FEATURES_MAX]
        return added

    def end_scene(self) -> bool:
        """Сцена кончилась — её следы уходят. True — было что убрать."""
        if not self.scene:
            return False
        self.scene.clear()
        return True

    def visible_features(self) -> list[str]:
        return [f for f in (*self.features, *self.scene) if visible(f)]

    def describe_ru(self) -> str:
        shown = self.visible_features()[-DESCRIBE_MAX:]
        if not shown:
            return CANON_RU
        return CANON_RU + " Особенности: " + "; ".join(shown) + "."

    def state_key(self, outside_key: str) -> str:
        digest = hashlib.sha1("\n".join((*self.features, *self.scene)).encode()).hexdigest()[:10]
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
                scene=[str(f) for f in data.get("scene") or []],
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
            "полминуты, и тогда ты его опишешь; до этого ничего не говори. "
            "Нарисовать что-то по просьбе — это generate_image, не этот тул."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": (
                        "Что снять крупно, своими словами (например «меч на "
                        "стене», «вид из окна», «кот на кресле»). Обязательно, "
                        "если просят снять что-то конкретное. Пусто — общий "
                        "вид кабинета."
                    ),
                },
                "caption": {
                    "type": "string",
                    "description": "Короткая подпись к снимку по-русски, 1-5 слов",
                },
                "expect": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "1-3 главных вещи, которые должны быть на снимке, — то, что "
                        'ты уже назвал собеседнику (например ["собака у камина"]). '
                        "Пусто — просто вид кабинета."
                    ),
                },
            },
        },
    },
}

# Ход на этом кончается (bot/tools.py::tool_take_photo → end_turn): модель
# этот текст уже не читает, он для журнала тулов. Реплика Альфреда придёт
# подписью к снимку.
TOOL_PHOTO_STARTED = "Снимок делается; Альфред отдаст его с подписью."
TOOL_PHOTO_SENT = (
    "Снимок кабинета уже отправлен собеседнику (ничего не изменилось с прошлого). "
    "Где ты: {where} Сейчас {now}."
)
# Повторный показ: есть, что на нём видно, — описывать по снимку.
TOOL_PHOTO_SENT_SEEN = (
    " На снимке: {description} Одной-двумя фразами отреагируй на него — не пересказывай."
)
# --- сверка снимка (Этап 49.2.1, llm/photo_check.py) ---

# app_state: что вышло на последнем снимке чата — видит только Альфред
# (заметка к ходу), не Ведущий и не запись кабинета.
LAST_PHOTO_KEY = "last_photo:{chat_id}"
LAST_PHOTO_TTL_H = 12

PHOTO_SEEN_NOTE = (
    "На твоём последнем снимке (отправлен {at}) видно: {description} "
    "Если спросят о нём — детали называй по снимку; сам не пересказывай."
)
PHOTO_SEEN_MISSING_NOTE = " Того, что ты обещал снять ({missing}), на нём нет — ты это уже заметил."
# Подпись к снимку — реплика Альфреда на то, что на нём вышло (живая находка
# 2026-10-02: описание «до снимка» Альфред выдумывал, а пересказ после —
# выходил длинным). Коротко и вместе с фото, одним сообщением.
PHOTO_READY_DIRECTIVE = (
    "Ты сфотографировал {subject} и отдаёшь собеседнику готовый снимок. "
    "На снимке: {description} Одной-двумя короткими фразами, в образе, скажи, "
    "что снимок готов, и отреагируй на то, что на нём вышло. Не пересказывай "
    "снимок и не говори о том, что собирался снять. Не упоминай генераторы, "
    "нейросети и модели — только фотоаппарат, плёнку, свет. Где ты: {where}"
)
PHOTO_READY_MISS_DIRECTIVE = (
    "Ты сфотографировал {subject}, уверенный, что в кадре {missing}, и отдаёшь "
    "снимок собеседнику. Глядя на него, ты видишь, что этого нет — на снимке: "
    "{description} Одной-двумя короткими фразами, в образе, удивись и предложи "
    "переснять. Не упоминай генераторы, нейросети и модели. Где ты: {where}"
)
PHOTO_READY_MISS_AGAIN_DIRECTIVE = (
    "Ты снова сфотографировал {subject}, уверенный, что в кадре {missing}, и "
    "снова этого на снимке нет — на нём: {description} Одной-двумя короткими "
    "фразами, в образе, пошути про капризный фотоаппарат или плёнку и предложи "
    "попробовать ещё раз. Не упоминай генераторы, нейросети и модели. Где ты: {where}"
)
PHOTO_SUBJECT_ROOM = "свой кабинет"
# Статусы, пока снимок делается (черновик хода, bot/handlers/ai.py): промптер
# пишет запрос генератору — «наводит», рисование и подпись — «проявляет».
PHOTO_STATUS_AIMING = "Альфред наводит фотоаппарат"
PHOTO_STATUS_DEVELOPING = "Альфред проявляет снимок"
# Подпись Telegram — до 1024 символов вместе с «Альфред:».
PHOTO_LINE_MAX = 900
# Обещанный снимок не дошёл (mycraft упала, бот перезапустили посреди
# съёмки) — сказать, а не молчать (живая находка 2026-10-02): выдуманной
# бедой с камерой, плёнкой или проявкой (решение пользователя), случайной.
# Голосом рассказчика: Альфред сейчас недоступен, картавость не наложить.
PHOTO_LOST_TEXTS = (
    "<i>Альфред возвращается из тёмной комнаты смущённым: снимок передержан в "
    "растворе и почернел. Попросите его снять ещё раз.</i>",
    "<i>Альфред случайно открыл дверь тёмной комнаты — снимок засвечен. "
    "Попросите его снять ещё раз.</i>",
    "<i>Альфред с досадой разглядывает чёрный кадр: он не снял крышку с "
    "объектива. Попросите его снять ещё раз.</i>",
    "<i>Плёнку в фотоаппарате заело, и кадр порвался. Альфред разводит руками — "
    "попросите его снять ещё раз.</i>",
    "<i>Проявитель оказался выдохшимся — на снимке лишь бледные пятна. "
    "Попросите Альфреда снять ещё раз.</i>",
    "<i>Затвор фотоаппарата заклинило на полпути. Альфред смущённо кашляет — "
    "попросите его снять ещё раз.</i>",
)
# app_state: обещанные снимки в работе {chat_id: {thread, reply_to}} — после
# рестарта бота Interactives.recover() скажет о пропавших.
PHOTO_PENDING_KEY = "photo_pending"
PHOTO_MISS_DIRECTIVE = (
    "Ты только что сфотографировал и отправил собеседнику снимок, уверенный, "
    "что в кадре {missing}. Посмотрев на снимок, ты видишь, что этого на нём "
    "нет — на снимке: {description} Коротко, в образе удивись: ты был уверен, "
    "что снял это. Предложи переснять. Не упоминай генераторы, нейросети и "
    "модели — только фотоаппарат, плёнку, свет."
)
PHOTO_MISS_AGAIN_DIRECTIVE = (
    "Ты снова сфотографировал, уверенный, что в кадре {missing}, и снова этого "
    "на снимке нет — на снимке: {description} Коротко, в образе пошути про "
    "капризный фотоаппарат или плёнку и предложи попробовать ещё раз. Не "
    "упоминай генераторы, нейросети и модели."
)

TOOL_PHOTO_BUSY = (
    "Прошлый снимок ещё проявляется — скажи, что он вот-вот будет. "
    "Не зови take_photo снова в этом ответе: новый снимок — когда придёт этот."
)
TOOL_PHOTO_LIMIT = "Плёнка на сегодня кончилась — скажи, что снимешь завтра."
TOOL_PHOTO_UNAVAILABLE = "недоступно: отсюда снимок не прислать"
