from __future__ import annotations

from typing import Any

from sa_home_bot.bot import (
    voice_mode,
)
from sa_home_bot.bot.tools.core import ActionRight, ToolContext, ToolSpec, VariantRights, text_arg
from sa_home_bot.proto.addresses import (
    LLM_SERVICE,
)

DISMISS_MODEL = "model"
DISMISS_SLEEP = "sleep"
DISMISS_OFF = "off"


# --- dismiss: «ты свободен» — погасить модель и машину под ней ---
#
# Единственный тул, который НИЧЕГО не делает в момент вызова (см.
# DismissalBox): гасить Ollama прямо здесь — значит оборвать ещё не
# сгенерированный ответ модели, то есть прощание, ради которого всё и
# затевалось. Тул записывает намерение, исполняет его bot/handlers/ai.py
# после отправки ответа.
#
# Машина здесь не параметр и не результат поиска по рою — это ровно та
# нода, с которой ТОЛЬКО ЧТО разговаривали (LLM_NODE, тот же адрес, что у
# самого диалога в bot/ai_flow.py). Иначе «выключись» в руках модели
# означало бы «выключи любую машину роя», включая ту, на которой живёт сам
# бот, — а этого не должно быть даже как опечатки.


async def tool_dismiss(ctx: ToolContext, args: dict[str, Any]) -> str:
    mode = text_arg(args, "mode")
    refusal = _DISMISS_VARIANTS.refusal_for(ctx.subscription, mode)
    if refusal is not None:
        return refusal
    if ctx.dismissal is None:
        # Отложенная задача (служба tasks) или иной не-живой вызов: некому
        # исполнить намерение после ответа — честно говорим «сейчас нет»,
        # а не обещаем выключение, которого не будет.
        return "недоступно: распустить себя можно только в живом разговоре"
    ctx.dismissal.mode = mode
    if mode == DISMISS_MODEL:
        return "принято: модель будет выгружена сразу после твоего ответа — попрощайся"
    machine = "выключена" if mode == DISMISS_OFF else "усыплена"
    return (
        f"принято: сразу после твоего ответа модель будет выгружена, а машина — {machine}. "
        "Попрощайся сейчас — это твоя последняя реплика в этом разговоре."
    )


_DISMISS_VARIANTS = VariantRights(
    param="mode",
    rights=(
        # Право на то же самое, что и кнопки на карточке ноды/службы у
        # человека: погасить модель — sleep@llm, усыпить и выключить машину —
        # suspend@node / poweroff@node.
        (DISMISS_MODEL, ActionRight("sleep", LLM_SERVICE)),
        (DISMISS_SLEEP, ActionRight("suspend", "node")),
        (DISMISS_OFF, ActionRight("poweroff", "node")),
    ),
)

_DECL_DISMISS: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "dismiss",
        "description": (
            "Уйти на покой: выгрузить себя из памяти машины и, если просят, "
            "усыпить или выключить её — штатно, как гасят свет, уходя. "
            "Вызывай, когда тебя ОТПУСКАЮТ: «свободен», «больше не нужен», "
            "«иди спать», «выключись», а также последним шагом просьбы "
            "«сделай то-то и выключись» (сначала дело, потом это). Просто "
            "«спасибо» или «пока» посреди разговора — не повод; сомневаешься "
            "— переспроси словами.\n"
            "Уход случится сразу ПОСЛЕ твоего ответа, не мгновенно: вызови "
            "инструмент и в той же реплике попрощайся — второго хода не "
            "будет, вернёт тебя только новое обращение.\n"
            "Значения mode перечислены в enum: то, чего там нет, ты не умеешь."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    # enum подставляется под права собеседника (см. tools_for).
                    "enum": [v for v, _ in _DISMISS_VARIANTS.rights],
                    "description": (
                        "model — выгрузить только модель, машина продолжает "
                        "работать (по умолчанию, если просто отпустили); "
                        "sleep — усыпить машину («иди спать»); "
                        "off — выключить машину («выключись», «выключи комп»)"
                    ),
                },
            },
            "required": ["mode"],
        },
    },
}


# --- voice_mode: тумблер голосовых ответов в этом чате ---
#
# Личная настройка формата ОТВЕТА, не системная операция — requires=None,
# как у calc/get_weather: ничего про систему не раскрывает, доступна без
# подписки. Состояние — per-chat в app_state (bot/voice_mode.py), эффект
# немедленный (в отличие от dismiss, тут не нужна отложенная исполнение
# после текущего ответа — сам этот ответ ещё может уйти текстом, следующий
# уже будет голосом).


async def tool_voice_mode(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.store is None or ctx.chat_id is None:
        return "недоступно: голосовой режим привязан к разговору, а его сейчас нет"
    mode = text_arg(args, "mode")
    if mode not in ("voice", "text"):
        return f"не умею: {mode or 'без уточнения'}"
    await voice_mode.set_enabled(ctx.store, ctx.chat_id, mode == "voice")
    return (
        "принято: теперь отвечаю голосовыми, пока не попросишь вернуть текст"
        if mode == "voice"
        else "принято: возвращаюсь к обычным текстовым ответам"
    )


_DECL_VOICE_MODE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "voice_mode",
        "description": (
            "Переключить формат твоих ответов В ЭТОМ ЧАТЕ: текстом или "
            "голосовым сообщением. Вызывай по прямой просьбе собеседника — "
            "«отвечай голосом», «давай голосовыми», «переходи на войсы» "
            "(mode=voice), либо «вернись к тексту», «хватит войсов», «пиши "
            "текстом» (mode=text). Это переключатель на весь разговор до "
            "следующей такой просьбы, не разовое действие на одно сообщение."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["voice", "text"],
                    "description": "voice — отвечать голосом; text — отвечать текстом",
                },
            },
            "required": ["mode"],
        },
    },
}


# --- recall_tool_result: полный текст сокращённого результата тула ---
#
# Живая находка 2026-09-04: длинные результаты тулов (в первую очередь
# web_search) сокращаются перед тем, как уйти модели (см. llm_chat.py::
# _inline_or_cache) — иначе несколько таких подряд в одном раунде
# tool-calling добивали окно контекста модели (num_ctx в llm/model-
# profiles.toml) и она возвращала пустой ответ. Полный текст никуда не
# девается — лежит в ctx.tool_result_cache под тем же id, что указан в
# пометке «сокращено»; этот тул — единственный способ модели его достать,
# если превью не хватило для ответа.


async def tool_recall_tool_result(ctx: ToolContext, args: dict[str, Any]) -> str:
    recall_id = args.get("id")
    if not isinstance(recall_id, str) or not recall_id.strip():
        return "ошибка: не указан id (он есть в пометке «сокращено» у результата инструмента)"
    full = ctx.tool_result_cache.get(recall_id.strip())
    if full is None:
        return "ошибка: под этим id ничего не сохранено — неверный id или он из более раннего хода"
    return full


_DECL_RECALL_TOOL_RESULT: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "recall_tool_result",
        "description": (
            "Получить ПОЛНЫЙ, несокращённый результат инструмента, который "
            "был вызван раньше в этом же разговоре и пришёл с пометкой "
            "«сокращено для экономии контекста». Зови, только если для "
            "ответа не хватило деталей из превью — id бери прямо из этой "
            "пометки, не выдумывай его."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "id из пометки «сокращено» у более раннего результата тула",
                }
            },
            "required": ["id"],
        },
    },
}


DISMISS = ToolSpec(
    name="dismiss",
    handler=tool_dismiss,
    declaration=_DECL_DISMISS,
    variants=_DISMISS_VARIANTS,
    telegram_only=True,
)

VOICE_MODE = ToolSpec(
    name="voice_mode",
    handler=tool_voice_mode,
    declaration=_DECL_VOICE_MODE,
    telegram_only=True,
)

RECALL_TOOL_RESULT = ToolSpec(
    name="recall_tool_result",
    handler=tool_recall_tool_result,
    declaration=_DECL_RECALL_TOOL_RESULT,
    telegram_only=True,
)
