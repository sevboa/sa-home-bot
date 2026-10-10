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

Этап 59: к радио добавились вещи подвала — вино (``wine``, 59.3) и старый
фотоаппарат (``camera``, 59.6). Они канареечные (``ItemKind.canary``): видны
и действуют только гостям из ``llm.interactives_canary_user_ids``. Черты у них
свободные (``free_traits``): Ведущий пишет приметы своими словами, портрет —
архетип без перекраски. Действие, после которого вещь исчезает, — общий
механизм «Применение» (``ItemAction.consumable``, 49.4): вещь ``consumed``,
эффект гостя (``UserEffect``) — ``user_effect:<ключ>:<гость>`` со сроком.

Действия (Этап 49.3.7) вид объявляет сам — ``ItemKind.actions``: из них
строятся «Действия» под карточкой и в описи /items, опись для Альфреда и
форма тула ``item_action``. Особенные вещи — рычаги поместья: их не
передают (``transferable=False``), а действия доступны всегда — кнопкой
из /items или просьбой Альфреду. Что действие делает — ``effect``:
обработчик в engine.py (``Interactives._ITEM_EFFECTS``).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from sa_home_bot.bot.interactives import radio

RADIO_TYPE = "radio"
WINE_TYPE = "wine"
CAMERA_TYPE = "camera"

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
class ItemAction:
    """Действие с вещью — данные вида.

    ``name`` — как его зовёт тул ``item_action``; ``code`` — callback-код
    кнопки (``it:<id>:<code>``, у радиостанции ``p``/``r`` — со старых
    карточек); ``label`` — кнопка в перечне «Действия»; ``when`` — где
    должна быть вещь, чтобы действие имело смысл (``PLACE_*``; пусто —
    всегда); ``effect``/``arg`` — обработчик в engine.py и его аргумент;
    ``form``/``confirm`` — вопрос формы тула и кнопка согласия; ``tool_ru`` —
    как действие видит Альфред в описи; ``done`` — ответ на нажатие;
    ``event`` — запись в ``item_events``; ``directive`` — что сказать
    Альфреду после; ``pinned_locked`` — в закреплённых чатах (речь
    закреплена, llm.speech_therapy_pinned_chat_ids) недоступно;
    ``consumable`` (Этап 59.3, «Применение» из 49.4) — после действия вещь
    израсходована: ``consumed``, из описи и мест она пропадает."""

    name: str
    code: str
    label: str
    effect: str
    form: str
    confirm: str
    tool_ru: str
    arg: str = ""
    when: str = ""
    done: str = "Готово."
    event: str = ""
    directive: str = ""
    pinned_locked: bool = False
    consumable: bool = False
    # Снимок Альфреда после действия (селфи, фраза по-английски для художника)
    # и подпись к нему; пусто — без снимка.
    snapshot: str = ""
    snapshot_caption: str = ""


@dataclass(frozen=True)
class UserEffect:
    """Эффект гостя (``user_effect:<ключ>:<гость>``, Этап 59.3): пока он
    действует, ``text`` уходит скрытой заметкой в system Альфреда на каждый
    ход гостя; ``hours`` — срок (None — пока вещь не вернут); ``ended`` —
    одна заметка Альфреду, когда срок вышел; ``item_type`` — вещь, чья
    заметка (``note``) подставляется в ``text`` как ``{note}`` (« (модель)»
    или пусто)."""

    key: str
    text: str
    hours: float | None = None
    ended: str = ""
    item_type: str = ""


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
    actions: tuple[ItemAction, ...] = field(default_factory=tuple)
    # Особенная вещь — рычаг поместья, не передаётся (49.4 «Передача»).
    transferable: bool = False
    # --- Этап 59: вещи подвала. Всё ниже по умолчанию даёт радио как было. ---
    # Канареечная вещь: видна и действует только гостям из канареечного списка.
    canary: bool = False
    # Черты — свободный текст Ведущего («этикетка выцвела, виден год — 1887»),
    # а не ключи словаря ``traits``: в портрете они не участвуют.
    free_traits: bool = False
    # Тексты вещи; пусто — как у радио (radio.ITEM_*). Место → строка.
    where_inv: dict[str, str] = field(default_factory=dict)  # опись /items и manor_items
    where_card: dict[str, str] = field(default_factory=dict)  # низ подписи карточки
    where_menu: dict[str, str] = field(default_factory=dict)  # шапка перечня действий
    card_caption: str = ""  # шаблон карточки: {name} {traits} {fault} {where}
    menu_caption: str = ""  # шаблон перечня действий: {name} {where}
    note_card: str = ""  # заметка в карточке: {fault}; пусто — «Беда с ней»
    note_tool: str = ""  # заметка в описи Альфреду: {fault}
    consumed_text: str = ""  # подпись карточки после расхода
    # Находка (59.3/59.6): запасные приметы/заметка, пока Ведущий не придумал
    # свои, и что Ведущему придумать (блок ко входу, поле ``found_item``).
    fallback_traits: tuple[str, ...] = ()
    fallback_notes: tuple[str, ...] = ()
    found_ask: str = ""

    def where_ru(self, place: str) -> str:
        return self.where_inv.get(place) or PLACE_RU.get(place, place)

    def card_where(self, place: str) -> str:
        if place in self.where_card:
            return self.where_card[place]
        return radio.ITEM_CARD_INSTALLED if place == PLACE_DESK else radio.ITEM_CARD_STORED

    def menu_where(self, place: str) -> str:
        if place in self.where_menu:
            return self.where_menu[place]
        return radio.ITEM_PLACE_INSTALLED if place == PLACE_DESK else radio.ITEM_PLACE_STORED

    def available(self, place: str) -> list[ItemAction]:
        """Действия, которые сейчас имеют смысл (по месту вещи)."""
        return [a for a in self.actions if not a.when or a.when == place]

    def action(self, key: str) -> ItemAction | None:
        """Действие по callback-коду или имени тула."""
        key = key.strip().lower()
        for action in self.actions:
            if key in (action.code, action.name):
                return action
        return None

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
        if self.free_traits:
            return [k for k in traits if k]
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
    # Стоит старая радиостанция — картавость (speech_clear выключен).
    actions=(
        ItemAction(
            name="put",
            code="p",
            label=radio.ITEM_PUT_LABEL,
            when=PLACE_STOREROOM,
            effect="radio_desk",
            arg="put",
            form=radio.RETURN_FORM_TEXT,
            confirm="Вернуть",
            tool_ru=radio.RADIO_ACTION_PUT,
            done="Поставлено.",
            event="installed",
            directive=radio.AFTER_RETURN_DIRECTIVE,
            pinned_locked=True,
        ),
        ItemAction(
            name="remove",
            code="r",
            label=radio.ITEM_REMOVE_LABEL,
            when=PLACE_DESK,
            effect="radio_desk",
            arg="remove",
            form=radio.REINSTALL_FORM_TEXT,
            confirm="Поставить новую",
            tool_ru=radio.RADIO_ACTION_REMOVE,
            done="Убрано.",
            event="removed",
            directive=radio.AFTER_SWAP_DIRECTIVE,
            pinned_locked=True,
        ),
    ),
    curse=Curse(
        model="revanim",
        lora="rottech",
        weight=0.8,
        prompt="cursed haunted radio, glowing eyes in the meters",
        steps=((1, 0.25), (3, 0.35), (5, 0.45)),
    ),
)

# --- Этап 59.3: вино ---

# Сколько Альфред хмелеет от бутылки (решение плана 59.3: по умолчанию 3 ч).
TIPSY_HOURS = 3.0
# Этап 59.6: размер снимков Альфреда со старым фотоаппаратом (вместо 64).
CAMERA_PHOTO_PX = 128

# Архетипы новых вещей — на стенд 59.0 (портрет turbo, как у радио): пока
# строки по образцу RADIO_ARCHETYPE, не проверенные 32 из 32.
WINE_ARCHETYPE = (
    "single old dusty dark green wine bottle, faded paper label, cork sealed with "
    "black wax, cobwebs on the shoulder"
)
CAMERA_ARCHETYPE = (
    "single vintage 1950s twin-lens reflex camera, worn black leather body, brass "
    "lens ring, folding viewfinder hood, leather neck strap"
)

WINE_ACTION_DRINK = (
    "распить бутылку вместе с собеседником (бутылка одна: после этого её не "
    "станет, а ты ненадолго захмелеешь)"
)
WINE_FORM_TEXT = "Распить бутылку вина?"
# Снимок после распития (селфи): Альфред с бокалом, чуть раскрасневшийся.
WINE_SNAPSHOT_EN = "holding a glass of dark red wine, cheeks slightly flushed, a faint smile"
AFTER_DRINK_DIRECTIVE = (
    "Вы с собеседником только что распили бутылку выдержанного вина из "
    "подвала: ты налил себе бокал и пригубил. Скажи две-три фразы, в образе: "
    "оцени вкус и возраст вина, поблагодари собеседника за компанию и дай "
    "понять, что от бокала ты чуть захмелел. Не говори про «эффект» и сроки."
)
WINE_CARD_CAPTION = "<b>{name}</b>\nБутылка из подвала замка. {traits}{fault}\n{where}"
WINE_ACTIONS_CAPTION = "<b>{name}</b>\n{where}\nЧто с ней сделать?"
WINE_NOTE_CARD = "На этикетке: {fault}."
WINE_NOTE_TOOL = " На этикетке: {fault}."
WINE_CONSUMED = "Бутылка распита."
WINE_FOUND_ASK = (
    "Бутылка вина у входа в подвал теперь вещь Альфреда. Опиши её в поле "
    "found_item: traits — 2-3 приметы короткими фразами (выцветшая этикетка, "
    "год, пыль, пробка, сургуч), note — что написано на этикетке (название и "
    "год, одной фразой). Эту бутылку Альфред потом может распить."
)
TIPSY_TEXT = (
    "Ты выпил вина вместе с собеседником и слегка навеселе: речь чуть мягче и "
    "многословнее обычного, ты теплее, разговорчивее и позволяешь себе "
    "лишнюю шутку или откровенность, изредка слегка сбиваешься, но остаёшься "
    "дворецким с достоинством. Не говори про «эффект», «игру» и время "
    "действия."
)
TIPSY_ENDED = (
    "Хмель выветрился, ты снова трезв. Можешь упомянуть об этом одной "
    "фразой, с лёгким смущением."
)

WINE = ItemKind(
    type=WINE_TYPE,
    name="Бутылка вина",
    icon="🍷",
    archetype=WINE_ARCHETYPE,
    checks=(
        "wine bottle :: a glass bottle with a long neck and a cork or cap; a jar or a "
        "glass does NOT count",
        "paper label :: a label glued on the bottle",
    ),
    traits={},
    ladder=(),
    paste_hint="old dusty wine bottle",
    focus_re=re.compile(r"бутылк|бутылоч|вин[оауеым]\b|вина\b|этикетк", re.IGNORECASE),
    canary=True,
    free_traits=True,
    where_inv={PLACE_STOREROOM: "стоит в подвале, у входа"},
    where_card={
        PLACE_STOREROOM: "Стоит в подвале у входа. Бутылка одна — если пожелаете, откроем."
    },
    where_menu={PLACE_STOREROOM: "Сейчас стоит в подвале у входа."},
    card_caption=WINE_CARD_CAPTION,
    menu_caption=WINE_ACTIONS_CAPTION,
    note_card=WINE_NOTE_CARD,
    note_tool=WINE_NOTE_TOOL,
    consumed_text=WINE_CONSUMED,
    fallback_traits=(
        "покрыта густой пылью",
        "этикетка выцвела, читается лишь год",
        "пробка залита чёрным сургучом",
        "стекло тёмно-зелёное, с пузырьками",
        "на плече бутылки паутина",
    ),
    fallback_notes=("«Шато Мортем», 1887", "«Вальдкеллер», год стёрся", "«Тёмный Дракон», 1923"),
    found_ask=WINE_FOUND_ASK,
    actions=(
        ItemAction(
            name="drink",
            code="d",
            label="Распить",
            when=PLACE_STOREROOM,
            effect="user_effect",
            arg="tipsy",
            form=WINE_FORM_TEXT,
            confirm="Распить",
            tool_ru=WINE_ACTION_DRINK,
            done="Распито.",
            event="consumed",
            directive=AFTER_DRINK_DIRECTIVE,
            consumable=True,
            snapshot=WINE_SNAPSHOT_EN,
            snapshot_caption="Альфред с бокалом",
        ),
    ),
)

# --- Этап 59.6: старый фотоаппарат ---

CAMERA_FORM_USE = (
    "Снимать этим аппаратом? Снимки выйдут чётче, сэр, но аппарат старый: "
    "он иногда видит то, чего нет."
)
CAMERA_FORM_BACK = "Вернуть прежний аппарат?"
CAMERA_ACTION_USE = "начать снимать этим аппаратом (снимки чётче, но он иногда видит то, чего нет)"
CAMERA_ACTION_BACK = "вернуть прежний аппарат"
AFTER_CAMERA_USE_DIRECTIVE = (
    "Ты повесил на шею старый фотоаппарат из проявочной. Скажи одну-две "
    "фразы, в образе: теперь ты снимаешь им, и кадры выйдут чётче, но "
    "предупреди, что аппарат иногда видит то, чего нет. Не говори про "
    "пиксели и разрешение."
)
AFTER_CAMERA_BACK_DIRECTIVE = (
    "Ты убрал старый фотоаппарат в чулан и снова снимаешь прежним. Скажи одну "
    "короткую фразу, в образе, с лёгким облегчением."
)
CAMERA_CARD_CAPTION = (
    "<b>{name}</b>\nСтарый фотоаппарат из проявочной под замком. {traits}{fault}\n{where}"
)
CAMERA_ACTIONS_CAPTION = "<b>{name}</b>\n{where}\nЧто с ним сделать?"
CAMERA_NOTE_CARD = "Аппарат: {fault}."
CAMERA_NOTE_TOOL = " Аппарат: {fault}."
CAMERA_FOUND_ASK = (
    "Альфред нашёл старый фотоаппарат, который сам когда-то оставил здесь. "
    "Придумай его в поле found_item: note — производитель и модель, одной "
    "фразой (выдуманные, правдоподобные для старой техники), traits — 1-3 "
    "приметы корпуса короткими фразами (потёртая кожа, трещина на "
    "видоискателе, медные накладки)."
)
CAMERA_TEXT = (
    "Ты снимаешь старым фотоаппаратом из проявочной{note}: снимки выходят "
    "заметно чётче, но этот аппарат иногда видит то, чего нет. Если на снимке "
    "оказалось что-то странное, лишнее или искажённое, ты не удивляешься, а "
    "списываешь это на причуды аппарата. Не говори про пиксели, настройки и "
    "разрешение."
)

CAMERA = ItemKind(
    type=CAMERA_TYPE,
    name="Старый фотоаппарат",
    icon="📷",
    archetype=CAMERA_ARCHETYPE,
    checks=(
        "old camera :: a photographic camera body with a lens; a radio or a box without "
        "a lens does NOT count",
        "round lens :: a round glass lens on the front of the camera",
    ),
    traits={},
    ladder=(),
    paste_hint="vintage reflex camera",
    focus_re=re.compile(r"фотоаппарат|фотик|фотокамер|камер[аыуе]\b|аппарат", re.IGNORECASE),
    canary=True,
    free_traits=True,
    where_inv={
        PLACE_DESK: "в деле: Альфред снимает им",
        PLACE_STOREROOM: "лежит в чулане",
    },
    where_card={
        PLACE_DESK: "Сейчас снимаю им: кадры выходят чётче, хотя аппарат порой видит лишнее.",
        PLACE_STOREROOM: "Лежит в чулане — если пожелаете, буду снимать им.",
    },
    where_menu={PLACE_DESK: "Сейчас снимаю им.", PLACE_STOREROOM: "Сейчас лежит в чулане."},
    card_caption=CAMERA_CARD_CAPTION,
    menu_caption=CAMERA_ACTIONS_CAPTION,
    note_card=CAMERA_NOTE_CARD,
    note_tool=CAMERA_NOTE_TOOL,
    fallback_traits=(
        "кожа корпуса потёрта до блеска",
        "медные накладки в зелёных пятнах",
        "трещина на матовом стекле видоискателя",
        "ремешок засален и перетёрт",
    ),
    fallback_notes=(
        "Цейс Иконта, модель «Икофлекс», 1938 года",
        "«Рейнхольд-Лукс», двухобъективная зеркальная, 1952 года",
        "«Альбрехт и сыновья», складная, около 1930 года",
    ),
    found_ask=CAMERA_FOUND_ASK,
    actions=(
        ItemAction(
            name="use",
            code="s",
            label="Снимать им",
            when=PLACE_STOREROOM,
            effect="user_effect",
            arg="camera",
            form=CAMERA_FORM_USE,
            confirm="Снимать им",
            tool_ru=CAMERA_ACTION_USE,
            done="Теперь снимаю им.",
            event="installed",
            directive=AFTER_CAMERA_USE_DIRECTIVE,
        ),
        ItemAction(
            name="back",
            code="v",
            label="Вернуть прежний",
            when=PLACE_DESK,
            effect="user_effect",
            arg="-camera",
            form=CAMERA_FORM_BACK,
            confirm="Вернуть",
            tool_ru=CAMERA_ACTION_BACK,
            done="Вернул прежний.",
            event="removed",
            directive=AFTER_CAMERA_BACK_DIRECTIVE,
        ),
    ),
)

KINDS: dict[str, ItemKind] = {RADIO.type: RADIO, WINE.type: WINE, CAMERA.type: CAMERA}

# Эффекты гостя от вещей (общий механизм «Применение», 49.4; ключ —
# ``user_effect:<ключ>:<гость>``, ``ItemAction.arg``).
EFFECTS: dict[str, UserEffect] = {
    "tipsy": UserEffect("tipsy", TIPSY_TEXT, hours=TIPSY_HOURS, ended=TIPSY_ENDED),
    "camera": UserEffect("camera", CAMERA_TEXT, item_type=CAMERA_TYPE),
}


def find_kind(text: str, kinds: Iterable[ItemKind] | None = None) -> ItemKind | None:
    """Вид по словам модели («радиостанция», «старый передатчик», «radio»).
    ``kinds`` — среди каких видов искать (по умолчанию все; движок отдаёт
    только видимые гостю — канареечные вещи прочим не существуют). Точное имя
    или тип — раньше любого узора, а из узоров первыми идут канареечные виды
    с узкими словами: «корпус фотоаппарата» не должно уходить в радио по
    слову «корпус»."""
    text = " ".join(text.split())
    if not text:
        return None
    pool = list(KINDS.values() if kinds is None else kinds)
    for kind in pool:
        if text.lower() in (kind.type, kind.name.lower()):
            return kind
    for kind in sorted(pool, key=lambda k: not k.canary):
        if kind.focus_re.search(text):
            return kind
    return None


def new_cut_key(kind: ItemKind, user_id: int) -> str:
    """Ключ вырезки на ноде llm (llm/item_paste.KEY_RE)."""
    return f"{kind.type}-{user_id}-{uuid.uuid4().hex[:10]}"


def new_seed() -> int:
    return int.from_bytes(uuid.uuid4().bytes[:4], "big")
