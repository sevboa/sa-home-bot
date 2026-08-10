"""RichStreamSession — Telegram Bot API 10.1/10.2 Rich Messages для ответов
Альфреда (этап 34, Фаза 2, IMPLEMENTATION_PLAN.md).

Rich как основной режим ответа (config.py::LlmConfig.response_mode), не
плейсхолдер на потом — с первого коммита никакого смешивания с обычным
plain ``editMessageText`` внутри одного ответа: только
``sendRichMessageDraft`` (пока идёт генерация) и ``sendRichMessage`` (когда
текст окончательный, единственная точка, которая реально персистит
сообщение в историю чата). ``InputRichMessage`` принимает готовый markdown
целиком — модель и так генерирует markdown (llm/prompt.py), собирать
rich-блоки (RichBlockParagraph/RichBlockTable/...) вручную не нужно.

``sendRichMessageDraft`` платформенно ограничен приватным чатом (докстринг
``chat_id`` в aiogram/methods/send_rich_message_draft.py: "target private
chat") — это ограничение Bot API, не наш выбор. В группах/супергруппах эта
сессия используется БЕЗ вызовов on_partial (см. bot/handlers/ai.py) — тогда
единственное, что уезжает в чат, это финальный ``finalize()``: то же
форматирование (таблицы, код, списки), но без анимации стрима.
"""

from __future__ import annotations

import logging
import random

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InputRichMessage, Message, ReplyParameters

from sa_home_bot.bot.notifier import send_with_retry

log = logging.getLogger(__name__)

ALFRED_PREFIX_MD = "**Альфред:** "


class RichStreamSession:
    """Одна сессия — один ответ Альфреда. ``draft_id`` генерируется случайно
    на сессию (не константа на чат!) — ``ActiveAiChats`` (bot/ai_flow.py)
    только хранит task для отмены при остановке бота, НЕ гарантирует, что в
    одном чате не может идти два /ai одновременно (два быстрых сообщения
    подряд диспетчеризуются aiogram каждое своей задачей) — общий draft_id
    у двух параллельных стримов означал бы, что Telegram "анимирует" один
    черновик поверх другого (докстринг SendRichMessageDraft: "changes to
    drafts with the same identifier are animated")."""

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        message_thread_id: int | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_thread_id = message_thread_id
        self._draft_id = random.randint(1, 2**31 - 1)
        self._last_sent: str | None = None

    async def _push_draft(self, markdown_body: str) -> None:
        """Общая отправка черновика — дедуп по последнему отправленному
        (не важно, статус это был или кусок настоящего ответа: одинаковый
        текст дважды подряд не стоит второго round-trip'а), best-effort
        (см. on_partial про то, почему потеря одного тика не критична)."""
        if not markdown_body or markdown_body == self._last_sent:
            return
        self._last_sent = markdown_body
        try:
            await self._bot.send_rich_message_draft(
                chat_id=self._chat_id,
                draft_id=self._draft_id,
                rich_message=InputRichMessage(markdown=ALFRED_PREFIX_MD + markdown_body),
                message_thread_id=self._message_thread_id,
            )
        except TelegramAPIError as exc:
            # Best-effort: черновик — превью, не критично потерять один
            # тик, следующий подтянет уже более свежий текст (в отличие от
            # finalize() ниже, где потерять сообщение молча нельзя).
            log.debug(
                "rich_stream: не удалось обновить черновик (chat=%s): %s", self._chat_id, exc
            )

    async def on_partial(self, text: str, done: bool) -> None:
        """Колбэк для llm_chat.py::run_chat_loop(on_partial=...).

        ``done`` игнорируется здесь: финализация идёт отдельным вызовом
        finalize() с уже постобработанным текстом (strip_math_notation +
        SpeechTherapist, llm/service.py), не с последним куском стрима —
        превью может чуть разойтись с финальным текстом в последний
        момент, это не проблема (черновик эфемерен, реальный текст в чат
        уходит один раз через finalize)."""
        await self._push_draft(text)

    async def push_status(self, text: str) -> None:
        """Курсивная "бегущая строка" (мышление/шаги/тул — bot/ai_flow.py)
        в тот же черновик, что on_partial: эфемерная реплика, которую
        сменит либо следующий статус, либо начало настоящего текста
        ответа, либо finalize() — платформенная семантика черновика
        (30-секундный превью, вытесняемый sendRichMessage) уже даёт
        "заменяется сообщением" бесплатно, без отдельной логики очистки.

        ``text`` — обычный текст без разметки, курсив оборачивается
        здесь же (единая точка форматирования, не на стороне вызывающего)."""
        await self._push_draft(f"_{text}_")

    async def finalize(self, raw: str, reply_to_message_id: int | None = None) -> Message | None:
        """Персистит настоящий ответ Альфреда — с ретраем на 429
        (bot/notifier.py::send_with_retry), как и обычная отправка
        сообщений. Работает одинаково для приватных чатов (после серии
        on_partial) и для групп (единственный вызов, без единого
        on_partial до этого) — группам streaming-черновик недоступен
        платформенно, не по нашему выбору (см. докстринг модуля)."""
        reply = (
            ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)
            if reply_to_message_id is not None
            else None
        )
        rich_message = InputRichMessage(markdown=ALFRED_PREFIX_MD + raw.strip())
        return await send_with_retry(
            self._chat_id,
            "rich-сообщение",
            lambda: self._bot.send_rich_message(
                chat_id=self._chat_id,
                rich_message=rich_message,
                reply_parameters=reply,
                message_thread_id=self._message_thread_id,
            ),
        )
