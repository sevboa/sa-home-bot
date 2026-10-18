from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from sa_home_bot.bot.interactives import radio
from sa_home_bot.bot.interactives.base import (
    STATUS_ACTIVE,
    STATUS_DECLINED,
    STATUS_DONE,
    STATUS_OFFERED,
    Run,
    Scenario,
    iso,
    parse_iso,
)
from sa_home_bot.bot.interactives.core import EngineCore
from sa_home_bot.bot.interactives.keyboards import (
    BTN_PLAY,
    FORM_OFFER,
    OFFER_EXPIRED_SUFFIX,
    offer_suffix,
)
from sa_home_bot.bot.interactives.registry import REGISTRY
from sa_home_bot.bot.interactives.scene import NOTE_TRANSCRIPT_LINES

OFFER_TTL = timedelta(hours=1)


DECLINE_COOLDOWN = timedelta(hours=24)


SPONTANEOUS_MIN_PLAIN_TURNS = 3


SPONTANEOUS_GAP = timedelta(hours=24)


OFFER_NO_WORDS_RE = re.compile(
    r"^\W*(?:ну\W+)?(?:нет|неа|не\s+(?:надо|нужно|стоит|обращай|ходи|сейчас)|забудь|оставь|потом)\b",
    re.I,
)


OFFER_YES_WORDS_RE = re.compile(
    r"^\W*(?:ну\W+)?(?:да|ага|угу|конечно|давай|ок(?:ей)?|хорошо|ладно|"
    r"проверь(?:те)?|сходи(?:те)?|иди(?:те)?|пойд[её]м|спустись|спуститесь|спустимся|"
    r"посмотри(?:те)?|глянь(?:те)?|сходим)\b",
    re.I,
)


AGREED_BY_WORDS_NOTE = (
    "Собеседник только что согласился сходить проверить. Скажи коротко, что "
    "берёшь фонарь и спускаешься, и опиши, что видишь на месте."
)


PHOTO_REQUEST_RE = re.compile(r"\b(?:покажи\w*|сними\w*|снимок|сфот\w*|фот\w*|селфи|кадр\w*)", re.I)


class OffersMixin(EngineCore):
    async def _spontaneous(
        self,
        loaded: list[tuple[Scenario, Run | None, bool]],
        chat_id: int,
        user_id: int,
        user_text: str,
    ) -> Scenario | None:
        candidates = [(s, r) for s, r, _ in loaded if s.trigger == "spontaneous"]
        if not candidates:
            return None
        if any(r is not None and r.status in (STATUS_OFFERED, STATUS_ACTIVE) for _, r, _ in loaded):
            return None
        plain = await self._state.plain_turns(chat_id)
        if plain < SPONTANEOUS_MIN_PLAIN_TURNS:
            await self._state.set_plain_turns(chat_id, plain + 1)
            return None
        if PHOTO_REQUEST_RE.search(user_text):
            return None
        now = self._now()
        for scenario, run in candidates:
            if await self._state.is_completed(scenario.id, user_id):
                continue
            if run is not None and run.status != STATUS_DECLINED:
                continue
            if not await self._may_offer(scenario, run, user_id):
                continue
            offered = parse_iso(run.offered_at) if run is not None else None
            if offered is not None and offered + SPONTANEOUS_GAP > now:
                continue
            if self._rng() >= self._settings.llm.interactives_spontaneous_chance:
                continue
            await self._offer(scenario, run, chat_id, user_id, user_text)
            await self._state.set_plain_turns(chat_id, 0)
            return scenario
        return None

    async def _may_offer(self, scenario: Scenario, run: Run | None, user_id: int) -> bool:
        if await self._state.is_completed(scenario.id, user_id):
            return False
        if run is None:
            return True
        if run.status in (STATUS_OFFERED, STATUS_ACTIVE, STATUS_DONE):
            return False
        if run.status == STATUS_DECLINED:
            until = parse_iso(run.declined_until)
            return until is None or until <= self._now()
        return True

    async def _offer(
        self,
        scenario: Scenario,
        run: Run | None,
        chat_id: int,
        user_id: int,
        user_text: str | None,
    ) -> None:
        if run is None:
            run = Run(scenario=scenario.id, chat_id=chat_id, user_id=user_id)
        run.status = STATUS_OFFERED
        run.offered_at = iso(self._now())
        if user_text:
            run.log("Гость", user_text)
        await self._state.save_run(run)
        self._queue(chat_id, scenario.id, FORM_OFFER, user_id)

    async def _expire_offer(self, run: Run | None) -> Run | None:
        if run is None or run.status != STATUS_OFFERED:
            return run
        offered = parse_iso(run.offered_at)
        if offered is not None and offered + OFFER_TTL <= self._now():
            run.status = STATUS_DECLINED
            run.declined_until = iso(self._now() + DECLINE_COOLDOWN)
            await self._state.save_run(run)
        return run

    async def _offer_by_words(
        self, chat_id: int, user_id: int, user_text: str
    ) -> tuple[bool, set[int]]:
        agreed, keep = False, set()
        for entry in await self._live_buttons(chat_id):
            scenario = REGISTRY.get(str(entry.get("scenario")))
            if (
                entry.get("form") != FORM_OFFER
                or scenario is None
                or not scenario.canary
                or entry.get("u") not in (None, user_id)
            ):
                continue
            run = await self._expire_offer(await self._state.load_run(chat_id, scenario.id))
            if run is None or run.status != STATUS_OFFERED or run.user_id != user_id:
                continue
            message_id = int(entry["m"])
            if OFFER_NO_WORDS_RE.match(user_text):
                answer = scenario.offer_buttons[1]
                run.status = STATUS_DECLINED
                run.declined_until = iso(
                    self._now() + (scenario.decline_cooldown or DECLINE_COOLDOWN)
                )
            elif OFFER_YES_WORDS_RE.match(user_text):
                answer = scenario.offer_buttons[0]
                run.status = STATUS_ACTIVE
                agreed = True
            else:
                keep.add(message_id)
                continue
            await self._state.save_run(run)
            if run.status == STATUS_ACTIVE and scenario.start_place is not None:
                await self._move_to(user_id, scenario.start_place)
            await self._forget_buttons(chat_id, message_id)
            await self._notifier.edit_text(
                chat_id, message_id, f"{scenario.offer_text}\n<i>— {answer}</i>"
            )
        return agreed, keep

    async def _click_offer(
        self, run: Run | None, user_id: int, button: str, where: dict[str, Any]
    ) -> tuple[str, str | None, bool]:
        if run is None or run.user_id != user_id:
            return "Эта форма не для вас.", None, False
        if run.status != STATUS_OFFERED:
            return "Уже решено.", None, True
        scenario = REGISTRY[run.scenario]
        offer_text = scenario.offer_text
        run = await self._expire_offer(run)
        if run is None or run.status != STATUS_OFFERED:
            return "Вопрос уже неактуален.", offer_text + OFFER_EXPIRED_SUFFIX, True
        yes_suffix, no_suffix = (offer_suffix(label) for label in scenario.offer_buttons)
        if button == BTN_PLAY:
            run.status = STATUS_ACTIVE
            await self._state.save_run(run)
            if scenario.start_place is not None:
                await self._move_to(user_id, scenario.start_place)
            transcript = "\n".join(run.transcript[-NOTE_TRANSCRIPT_LINES:]) or "—"
            directive = scenario.after_agree or radio.AFTER_AGREE_DIRECTIVE
            await self._speak(run.chat_id, directive.format(transcript=transcript), where)
            return "Хорошо.", offer_text + yes_suffix, True
        run.status = STATUS_DECLINED
        run.declined_until = iso(self._now() + (scenario.decline_cooldown or DECLINE_COOLDOWN))
        await self._state.save_run(run)
        return "Хорошо.", offer_text + no_suffix, True
