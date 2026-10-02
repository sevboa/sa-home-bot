"""Сюжетные предметы гостей (Этап 49.3): реестр видов.

Вид — данные персонажа в коде (характер важнее чистоты кода): название,
облик и черты. Экземпляр у гостя — таблица ``items`` (db/schema.sql),
создают его только сценарии, не модель.

Облик предмета держится, только если это проверенная строка в коде
(«Стенд предметов» 2026-10-01): архетип ``RADIO_ARCHETYPE`` рисуется turbo
без ламп 32 из 32 раз, а описанный словами в промпте сцены предмет сцена
«съедает». Поэтому предмет рисуется отдельно портретом (служба llm,
``item_portrait``) и вставляется в сцены пикселями (``generate_image`` с
``paste``).

Черты копятся по ходу сцены (Ведущий, поле ``item_trait``; молчит — код
берёт следующую по лестнице). Рисуются только те, что прошли стенд
49.3.0 (``Trait.en`` не пуст), остальные живут в тексте: Альфред о них
знает и рассказывает, а на картинке радиостанция обычная.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

RADIO_TYPE = "radio"

# Где вещь (Этап 49.3.5). Стоит в кабинете — попадает в его снимки
# (вставкой), в чулане — показывается карточкой.
PLACE_DESK = "desk"
PLACE_STOREROOM = "storeroom"
PLACE_RU = {PLACE_DESK: "на столе в кабинете", PLACE_STOREROOM: "в чулане"}

# Опись вещей поместья (/items) — голос Альфреда. Вещь заведена на гостя
# (у каждого свой кабинет), но для него это вещи поместья, а не его карман.
INVENTORY_TITLE = "<b>Особенные вещи поместья</b>"
INVENTORY_LINE = "{icon} <b>{name}</b> — {where}."
INVENTORY_TRAITS = "<i>Приметы: {traits}.</i>"
INVENTORY_HINT = "Нажмите на вещь — принесу показать."
INVENTORY_EMPTY = "Особенных вещей в поместье пока нет, сэр."


@dataclass(frozen=True)
class Trait:
    """Черта предмета. ``ru`` — как её видят Альфред и Ведущий, ``en`` —
    фраза по-английски (подсказка перекраске), ``drawn`` — turbo рисует её
    на портрете с весом ``ItemKind.trait_weight`` (стенд 49.3.0: без веса
    черты не рисуются вовсе, 1/21)."""

    ru: str
    en: str = ""
    drawn: bool = False


@dataclass(frozen=True)
class Curse:
    """«Уровень проклятия» (стенд 49.3.0): портрет перекрашивается img2img
    моделью ``model`` с LoRA ``lora`` — облик узнаваем, а гниль и свечение
    нарастают с силой. ``steps`` — (сколько черт, сила) по возрастанию."""

    model: str
    lora: str
    weight: float
    prompt: str
    steps: tuple[tuple[int, float], ...]

    def strength(self, count: int) -> float:
        strength = 0.0
        for need, value in self.steps:
            if count >= need:
                strength = value
        return strength


@dataclass(frozen=True)
class ItemKind:
    type: str
    name: str
    archetype: str
    # Признаки для сверки зрением (llm/photo_check.py): «название :: что не
    # считается»; в промахи уходит название.
    checks: tuple[str, ...]
    traits: dict[str, Trait]
    # Порядок черт, когда Ведущий их не дал (смена стадии без item_trait).
    ladder: tuple[str, ...]
    # Подсказка гармонизации при вставке в сцену (img2img 0.35 по кадру с
    # предметом): что за предмет стоит в кадре.
    paste_hint: str
    # Фокус кадра «про предмет» — крупный план вместо общего вида.
    focus_re: re.Pattern[str]
    # Стенд 49.3.0 (лестница): 1.3 — черты копятся видно, фон остаётся серым
    # (вырезке это важно), облик узнаваем (DINO к базе 0.70-0.96); 1.5 по
    # одной черте рисует явнее, но 5 таких черт дают один глаз во весь кадр.
    trait_weight: float = 1.3
    curse: Curse | None = None
    # Просьба снять именно убранный экземпляр («проклятый», «со склада»).
    stored_re: re.Pattern[str] | None = None
    icon: str = "📦"

    def portrait_prompt(self, traits: list[str]) -> str:
        """Промпт turbo с весами compel (служба llm, item_portrait)."""
        drawn = [
            f"({self.traits[k].en}){self.trait_weight:g}"
            for k in traits
            if k in self.traits and self.traits[k].drawn
        ]
        return ", ".join([self.archetype, *drawn])

    def restyle(self, traits: list[str]) -> dict | None:
        """Перекраска портрета под число черт (None — без неё)."""
        if self.curse is None:
            return None
        strength = self.curse.strength(len([k for k in traits if k in self.traits]))
        if not strength:
            return None
        words = [self.traits[k].en for k in traits if k in self.traits and self.traits[k].en]
        return {
            "model": self.curse.model,
            "loras": [[self.curse.lora, self.curse.weight]],
            "strength": strength,
            "prompt": ", ".join([self.archetype, self.curse.prompt, *words]),
        }

    def traits_ru(self, traits: list[str]) -> list[str]:
        return [self.traits[k].ru for k in traits if k in self.traits]

    def drawn_key(self, traits: list[str]) -> tuple[str, ...]:
        """Что видно на портрете — рисуемые черты и сила проклятия: другое —
        новый портрет."""
        key = tuple(k for k in traits if k in self.traits and self.traits[k].drawn)
        restyle = self.restyle(traits)
        return key + ((f"curse{restyle['strength']:g}",) if restyle else ())

    def next_trait(self, traits: list[str]) -> str | None:
        for key in self.ladder:
            if key not in traits:
                return key
        return None


RADIO_ARCHETYPE = (
    "1950s ham radio transceiver, black crinkle metal panel, two analog needle meters, "
    "big knobs, black bakelite handheld push-to-talk microphone on coiled cord"
)

RADIO = ItemKind(
    type=RADIO_TYPE,
    name="Проклятая радиостанция",
    icon="📻",
    archetype=RADIO_ARCHETYPE,
    checks=(
        "old radio set :: a box-shaped receiver or transceiver with knobs and dials",
        "separate handheld microphone on a cord :: a bare cable, a plug or a speaker "
        "grille does NOT count",
        "analog needle meter :: a dial with a visible pointer needle; a digital display "
        "does NOT count",
    ),
    traits={
        "пыль": Trait("покрыта пылью и паутиной", "dusty with cobwebs", drawn=True),
        "трещина": Trait("стекло стрелочного прибора треснуло", "cracked meter glass", drawn=True),
        "дым": Trait("из-под крышки сочится тонкая струйка дыма", "thin smoke"),
        "ржавчина": Trait(
            "по корпусу пошли рыжие пятна ржавчины", "orange rust stains", drawn=True
        ),
        "глаз": Trait(
            "на шкале иногда открывается глаз",
            "a glowing red eye inside the left meter",
            drawn=True,
        ),
        "копоть": Trait("панель в чёрной копоти", "black soot"),
        "стрелка": Trait("стрелка прибора налилась красным", "red meter needles", drawn=True),
    },
    ladder=("пыль", "трещина", "ржавчина", "дым", "копоть", "стрелка", "глаз"),
    paste_hint="vintage ham radio with needle meters and black handheld microphone on the desk",
    # «Прибор», «шкала», «корпус» — так Альфред зовёт радиостанцию в сцене
    # («треснувшее стекло прибора»); без них крупный план шёл без радио.
    # Старые имена («передатчик», «радио», «рация») — гость скажет как угодно.
    focus_re=re.compile(
        r"радиостанц|\bстанци|трансивер|передатчик|радио|раци|устройств\w* связи|"
        r"микрофон|прибор|шкал|корпус",
        re.IGNORECASE,
    ),
    stored_re=re.compile(r"прокля|стар\w+|склад|чулан|кладов", re.IGNORECASE),
    curse=Curse(
        model="revanim",
        lora="rottech",
        weight=0.8,
        prompt="cursed haunted radio, glowing eyes in the meters",
        steps=((1, 0.25), (3, 0.35), (5, 0.45)),
    ),
)

KINDS: dict[str, ItemKind] = {RADIO.type: RADIO}


def find_kind(text: str) -> ItemKind | None:
    """Вид по словам модели («радиостанция», «старый передатчик», «radio»)."""
    text = " ".join(text.split())
    if not text:
        return None
    for kind in KINDS.values():
        if text.lower() in (kind.type, kind.name.lower()) or kind.focus_re.search(text):
            return kind
    return None


def new_cut_key(kind: ItemKind, user_id: int) -> str:
    """Ключ вырезки на ноде llm (llm/item_paste.KEY_RE)."""
    return f"{kind.type}-{user_id}-{uuid.uuid4().hex[:10]}"


def new_seed() -> int:
    return int.from_bytes(uuid.uuid4().bytes[:4], "big")
