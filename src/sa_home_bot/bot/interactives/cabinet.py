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
DESCRIBE_MAX = 5
FEATURE_MAX_CHARS = 140
FIRST_FEATURES = 3
PHOTOS_KEEP = 8

CANON_RU = (
    "Кабинет Альфреда — старого дворецкого — в замке в Трансильвании: "
    "письменный стол, на нём старый радиопередатчик, каменный камин, "
    "книжные полки, высокое стрельчатое окно."
)
CANON_EN = "dark gothic study, stone fireplace, bookshelves"
PHOTO_NEGATIVE_EN = "monochrome, grayscale"

PORTRAIT_SUBJECT_EN = (
    "Main subject: Alfred, an elderly butler in a black tailcoat, waist-up, "
    "standing in his study and greeting the viewer with a slight polite bow."
)
PORTRAIT_CAPTION = "Альфред в кабинете"
SELFIE_SUBJECT_EN = (
    "Main subject: Alfred, an elderly butler in a black tailcoat, waist-up, "
    "in his study, looking at the viewer."
)
SELFIE_ACTION_EN = (
    "Main subject: Alfred, an elderly butler in a black tailcoat, in his study: {action}."
)
SELFIE_CAPTION = "Альфред"
SELFIE_POSE_RE = re.compile(
    r"спин|сзади|профил|боком|сбоку|полный рост|во весь рост|в рост|ид[её]т|шага|"
    r"from behind|\bback\b|profile|side view|full[- ]body|full[- ]length|walking",
    re.IGNORECASE,
)
SELFIE_POSE_ROOM_EN = "dark gothic study"
SELFIE_FOCUS_RE = re.compile(
    r"^\s*(?:себя|меня|сам(?:ого)?\s+себя|селфи|автопортрет|альфред(?:а)?|"
    r"(?:мой|свой)\s+(?:портрет|автопортрет))\s*[.!]?\s*$",
    re.IGNORECASE,
)

SCENE_ALFRED_EN = {
    "back": "Alfred, an elderly butler in a black tailcoat, seen from behind, face hidden",
    "side": "Alfred, an elderly butler in a black tailcoat, in side profile, turned away",
}

SELFIE_MOOD_PURPOSE = "selfie_mood"
SELFIE_MOOD_GAP_H = 2
SELFIE_MOOD_DAILY = 3
SELFIE_MOOD_CHANCE = {2: 0.25, 3: 0.6}
SELFIE_EMOTION_FACE_EN = {
    "anger": "stern cold glare, clenched jaw, fire roaring in the fireplace",
    "embarrassment": "blushing, looking away, hand on the back of his head",
    "joy": "laughing heartily, warm light",
    "support": "gentle kind smile, offering a cup of tea toward the viewer",
    "about_him": "",
}
SELFIE_EMOTIONS = tuple(SELFIE_EMOTION_FACE_EN)
SELFIE_MOOD_CAPTION = "Альфред"
SELFIE_ACTION_MAX = 120
SELFIE_MOOD_SYSTEM = (
    "Ты оцениваешь эмоциональный момент разговора с Альфредом — старым "
    "дворецким в замке в Трансильвании. По последней реплике собеседника и "
    "ответу Альфреда реши, стоит ли Альфреду приложить к ответу своё фото. "
    "Отвечай строго одним JSON-объектом."
)
SELFIE_MOOD_INPUT = (
    "Реплика собеседника:\n{user}\n\nОтвет Альфреда:\n{reply}\n\n"
    'Верни JSON: {{"emotion": одно из "anger" (Альфред сердится), '
    '"embarrassment" (смущён), "joy" (радуется, смеётся), "support" '
    '(утешает, поддерживает), "about_him" (разговор о нём самом или о том, '
    'что его окружает: кабинет, замок, его вещи), "none"; "intensity": '
    "целое 0..3, насколько эмоция сильная и фото к месту (3 — яркий момент, "
    'обычный деловой разговор — 0); "action": что Альфред делает в кадре, '
    "по-английски, 3-10 слов}}."
)

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
    scene: list[str] = field(default_factory=list)
    photos: dict[str, int] = field(default_factory=dict)

    def add(self, new: list[str], *, scene: bool = False) -> list[str]:
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
        if not self.scene:
            return False
        self.scene.clear()
        return True

    def visible_features(self) -> list[str]:
        return [f for f in (*self.features, *self.scene) if visible(f)]

    def describe_ru(self) -> str:
        shown = self.visible_features()[-DESCRIBE_MAX:]
        if not shown:
            return self.canon_ru
        return self.canon_ru + " Особенности: " + "; ".join(shown) + "."

    @property
    def place(self) -> str:
        return LOCATION

    @property
    def is_study(self) -> bool:
        return True

    @property
    def name_ru(self) -> str:
        return "кабинет"

    @property
    def in_ru(self) -> str:
        return "в кабинете"

    @property
    def canon_ru(self) -> str:
        return CANON_RU

    @property
    def canon_en(self) -> str:
        return CANON_EN

    @property
    def windowed(self) -> bool:
        return True

    @property
    def caption(self) -> str:
        return "Кабинет"

    @property
    def scene_caption(self) -> str:
        return "В кабинете"

    @property
    def subject_ru(self) -> str:
        return PHOTO_SUBJECT_ROOM

    @property
    def pose_room_en(self) -> str:
        return SELFIE_POSE_ROOM_EN

    def light_en(self, outside: Any, *, closeup: bool = False) -> str:
        return outside.en(closeup=closeup)

    def localize_selfie(self, text: str) -> str:
        return text

    def localize_ru(self, text: str) -> str:
        return text

    def is_general(self, focus: str) -> bool:
        return False

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


TAKE_PHOTO_DECLARATION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "take_photo",
        "description": (
            "Сфотографировать то, что ты сейчас видишь у себя в кабинете, и "
            "прислать собеседнику: когда он просит фото («скинь фото», «покажи, "
            "что у тебя там», «как там твой кабинет») или когда рядом происходит "
            "что-то, что стоит показать. Себя в кабинете (автоспуском или в "
            "зеркало) — тоже этим тулом, с selfie=true: «сфоткай себя», «скинь "
            "селфи», «покажись». Снимок придёт сам примерно через "
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
                "selfie": {
                    "type": "boolean",
                    "description": (
                        "true — в кадре ты сам: просят снять тебя, а не кабинет. "
                        "focus тогда — что ты делаешь и где стоишь («у окна с "
                        "бокалом вина», «за столом с пером»); пусто — просто ты "
                        "в кабинете."
                    ),
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

TOOL_PHOTO_STARTED = "Снимок делается; Альфред отдаст его с подписью."
TOOL_PHOTO_SENT = (
    "Снимок кабинета уже отправлен собеседнику (ничего не изменилось с прошлого). "
    "Где ты: {where} Сейчас {now}."
)
TOOL_PHOTO_SENT_SEEN = (
    " На снимке: {description} Одной-двумя фразами отреагируй на него — не пересказывай."
)

LAST_PHOTO_KEY = "last_photo:{chat_id}"
LAST_PHOTO_TTL_H = 12

PHOTO_SEEN_NOTE = (
    "На твоём последнем снимке (отправлен {at}) видно: {description} "
    "Если спросят о нём — детали называй по снимку; сам не пересказывай."
)
PHOTO_SEEN_MISSING_NOTE = " Того, что ты обещал снять ({missing}), на нём нет — ты это уже заметил."
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
PHOTO_SUBJECT_SELF = "себя в кабинете (автоспуском)"
PHOTO_SUBJECT_SELF_DOING = "себя (автоспуском): {focus}"
PHOTO_STATUS_AIMING = "Альфред наводит фотоаппарат"
PHOTO_STATUS_DEVELOPING = "Альфред проявляет снимок"
PHOTO_LINE_MAX = 900
PHOTO_CAPTION_MAX = 1024
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
