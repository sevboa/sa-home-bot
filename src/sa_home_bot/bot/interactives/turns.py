from __future__ import annotations

import dataclasses
import functools
import logging

from sa_home_bot.bot.interactives import cabinet as cabinet_mod
from sa_home_bot.bot.interactives import items as items_mod
from sa_home_bot.bot.interactives.base import (
    EFFECT_ACTIVE,
    EFFECT_EXPIRED,
    STATUS_ACTIVE,
    STATUS_DONE,
    STATUS_IDLE,
    Run,
    Scenario,
)
from sa_home_bot.bot.interactives.cabinet import Cabinet
from sa_home_bot.bot.interactives.core import EngineCore
from sa_home_bot.bot.interactives.director import DirectorDecision, ask_director
from sa_home_bot.bot.interactives.offers import AGREED_BY_WORDS_NOTE
from sa_home_bot.bot.interactives.photo_text import PHOTO_SCENE_CAPTION, scene_photo_focus
from sa_home_bot.bot.interactives.places import Room
from sa_home_bot.bot.interactives.registry import REGISTRY
from sa_home_bot.bot.interactives.scene import (
    TurnPlan,
    apply_decision,
    build_scene_note,
    finale_allowed,
    grow_item,
)
from sa_home_bot.proto.addresses import LLM_ADDRESS

log = logging.getLogger(__name__)


class TurnsMixin(EngineCore):
    async def before_turn(
        self, chat_id: int, user_id: int | None, user_text: str, *, is_private: bool
    ) -> TurnPlan:
        # Новый ход гостя: счётчик действий обнуляется, реплику запоминаем —
        # по ней Ведущий в тулах act проверяет согласие (Этап 59.7).
        self._acts_new_turn(chat_id, user_text)
        plan = TurnPlan(
            chat_id=chat_id,
            user_id=user_id,
            user_text=user_text,
            speech_clear=await self.speech_clear(user_id),
            speech_clear_now=functools.partial(self.speech_clear, user_id),
            note=await self.photo_note(chat_id),
        )
        # Канарейка отвечает на предложение словами (59.7): «да»/«нет» — как
        # кнопки; о другом — предложение живёт свой час, кнопки остаются.
        agreed, keep = (
            await self._offer_by_words(chat_id, user_id, user_text)
            if is_private and user_id is not None and self.canary_ok(user_id)
            else (False, set())
        )
        # Гость пишет, не нажав кнопок, — прежние формы проигнорированы.
        await self.dismiss_buttons(chat_id, user_id, keep=keep)
        if not is_private or user_id is None or self._pinned(chat_id):
            return plan
        if agreed:
            plan.note = (
                f"{AGREED_BY_WORDS_NOTE}\n\n{plan.note}" if plan.note else AGREED_BY_WORDS_NOTE
            )
        if await self._state.is_opted_out(chat_id):
            return plan
        canary = self.canary_ok(user_id)
        if canary:
            # Эффекты вещей (59.3/59.6): хмель, старый фотоаппарат — пока
            # действуют, текст уходит в system Альфреда каждый ход гостя.
            effects = await self._effects_note(user_id)
            if effects:
                plan.note = f"{effects}\n\n{plan.note}" if plan.note else effects
        base_note = plan.note
        if canary:
            # Альфред мог вернуться в кабинет, пока гость молчал (59.1).
            back = await self._tend_position(user_id)
            if back:
                plan.note = f"{back}\n\n{plan.note}" if plan.note else back
        loaded: list[tuple[Scenario, Run | None, bool]] = []
        for scenario in self.scenarios_for(user_id):
            run = await self._state.load_run(chat_id, scenario.id)
            run = await self._expire_offer(run)
            triggered = bool(scenario.trigger_re.search(user_text))

            if run is not None and run.status == STATUS_IDLE and triggered:
                run.status = STATUS_ACTIVE  # согласие уже было — продолжаем молча
                await self._state.save_run(run)
                if canary and scenario.stage_places:
                    # Альфред вернулся к делу: туда, где сцена остановилась
                    # (и заметка «поднялся в кабинет» этому ходу уже не нужна).
                    stage = min(run.stage, scenario.last_stage)
                    await self._move_to(user_id, scenario.stage_places[stage])
                    plan.note = base_note
            if run is not None and run.status == STATUS_ACTIVE:
                plan.scenario = scenario.id
                plan.scene = True
                if canary and not scenario.canary:
                    # Сцена радио идёт в кабинете, где бы Альфред ни был;
                    # канареечные сцены место держат сами (stage_places,
                    # move_to, walk катакомб).
                    await self._go_home(user_id, announce=False)
                cab = await self._room(chat_id, user_id)
                if cab.windowed:
                    outside = await self._transylvania.outside(self._now())
                    place = f"{cab.describe_ru()} Сейчас {outside.ru()}."
                else:
                    place = cab.describe_ru()
                scene_note = build_scene_note(scenario, run, place)
                if canary:
                    # Место в сцене уже сказано выше; здесь только правило (59.7).
                    scene_note += "\n\n" + self._where_rule(cab)
                plan.note = f"{scene_note}\n\n{plan.note}" if plan.note else scene_note
                plan.force_swap_form = (
                    run.finale and not run.finale_form_sent and not scenario.finish_after_finale
                )
                return plan
            loaded.append((scenario, run, triggered))
        for scenario, run, triggered in loaded:
            if triggered and await self._may_offer(scenario, run, user_id):
                await self._offer(scenario, run, chat_id, user_id, user_text)
                plan.scenario = scenario.id
                plan.offered = True
                if canary:
                    await self._state.set_plain_turns(chat_id, 0)
                    await self._add_where_note(plan, chat_id, user_id)
                return plan
        if canary:
            offered = await self._spontaneous(loaded, chat_id, user_id, user_text)
            if offered is not None:
                plan.scenario = offered.id
                plan.offered = True
            await self._add_where_note(plan, chat_id, user_id)
        return plan

    async def _effects_note(self, user_id: int) -> str | None:
        """Скрытая заметка Альфреду о действующих эффектах гостя
        (``user_effect:<ключ>:<гость>``, items.EFFECTS). Срок вышел — эффект
        снимается, и Альфред один раз получает заметку о конце (хмель прошёл).
        Только канарейкам: вызывающий проверяет ``canary_ok``."""
        now = self._now()
        parts: list[str] = []
        for effect in items_mod.EFFECTS.values():
            state = await self._state.timed_effect(effect.key, user_id, now)
            if state == EFFECT_ACTIVE:
                note = ""
                if effect.item_type:
                    owned = await self._store.items_of(user_id, effect.item_type)
                    note = (owned[0].get("note") or "").rstrip(". ") if owned else ""
                parts.append(effect.text.format(note=f" ({note})" if note else ""))
            elif state == EFFECT_EXPIRED:
                await self._state.clear_effect(effect.key, user_id)
                if effect.ended:
                    parts.append(effect.ended)
        return "\n".join(parts) or None

    async def camera_on(self, user_id: int | None) -> bool:
        """Снимает ли Альфред для гостя старым фотоаппаратом (59.6). Эффект
        действует только пока гость в канареечном списке."""
        if not self.canary_ok(user_id):
            return False
        return (
            await self._state.timed_effect("camera", int(user_id or 0), self._now())
            == EFFECT_ACTIVE
        )

    async def photo_size(self, user_id: int | None) -> int | None:
        """Размер снимков Альфреда для гостя: ``items.CAMERA_PHOTO_PX`` со
        старым фотоаппаратом, иначе None — службе llm достаётся прежний
        (``imagegen_size``). Все снимки гостя (кабинет, подвал, катакомбы,
        сцены, take_photo, селфи) берут размер отсюда."""
        return items_mod.CAMERA_PHOTO_PX if await self.camera_on(user_id) else None

    async def after_turn(
        self,
        plan: TurnPlan,
        reply: str,
        *,
        dialogue_id: int | None,
        message_thread_id: int | None = None,
    ) -> None:
        if plan.scenario is None:
            return
        scenario = REGISTRY[plan.scenario]
        run = await self._state.load_run(plan.chat_id, scenario.id)
        if run is None:
            return
        if plan.offered:
            run.log("Альфред", reply)
            await self._state.save_run(run)
            return
        if not plan.scene or run.status != STATUS_ACTIVE:
            return
        # Тул act уже занёс реплику гостя в журнал перед своим действием.
        if run.world.pop("guest_logged", None) != plan.user_text:
            run.log("Гость", plan.user_text)
        run.log("Альфред", reply)
        # Прошлое событие Альфред уже пересказал этим ответом.
        run.pending_effect = None
        cab = await self._room(run.chat_id, run.user_id)
        if scenario.canary:
            # Где Альфред — Ведущему (cellar.extra_input).
            run.world["here"] = cab.canon.name_ru if isinstance(cab, Room) else "кабинет"
        decision = await self._ask_director(scenario, run, cab)
        # Был act — исходы действий уже даны Альфреду в самом ходу, событие
        # после хода их не придумывает (Этап 59.7).
        acted = bool(run.world.pop("acted", None))
        if acted and decision is not None and decision.effect:
            decision = dataclasses.replace(decision, effect=None)
        # Ведущий придумал приметы найденной вещи (59.3/59.6) — дописываем.
        await self._apply_found_item(run, decision)
        stage_before, finale_before = run.stage, run.finale
        if decision is not None and decision.cabinet_add:
            # Перечитываем перед записью: снимок в фоне мог дописать своё.
            cab = await self._room(run.chat_id, run.user_id)
            # Первые особенности (кабинет ещё пуст) — постоянные, прочие —
            # следы сцены.
            if cab.add(list(decision.cabinet_add), scene=bool(cab.features)):
                await self._save_room(cab, run)
        if decision is not None and run.world.get("act_key") and decision.stage > run.stage:
            # Стадию уже сдвинул act этого хода: вторая за ход — рано, Альфред
            # ещё не получил подсказки новой стадии.
            decision = dataclasses.replace(decision, stage=run.stage)
        effect = apply_decision(scenario, run, decision, choose=self._choose)
        if effect:
            run.log("Событие", effect)
            run.pending_effect = effect
        key_moment = (
            run.stage != stage_before
            or run.finale != finale_before
            # Стадию или финал в этом ходу сдвинул тул act (кадр сцены — тот же).
            or bool(run.world.pop("act_key", None))
        )
        if scenario.canary:
            await self._after_canary_turn(
                scenario, run, decision, stage_before=stage_before, finale_before=finale_before
            )
        kind = items_mod.KINDS.get(scenario.item_kind or "")
        if kind is not None and run.status == STATUS_ACTIVE:
            if run.item_seed is None:
                run.item_seed = items_mod.new_seed()
            trait = grow_item(kind, run, decision, key_moment=key_moment)
            if trait is not None:
                log.info("interactives: у передатчика новая черта %r (chat=%s)", trait, run.chat_id)
        focus = scene_photo_focus(run, decision, key_moment=key_moment)
        scene_caption = PHOTO_SCENE_CAPTION
        if scenario.canary:
            # Кадр рисует фоновая задача и читает комнату (в том числе
            # временную, из мира сцены) из хранилища — сначала запись.
            await self._state.save_run(run)
            scene_caption = (await self._room(run.chat_id, run.user_id)).scene_caption
        if (
            focus is not None
            and run.status == STATUS_ACTIVE
            and not await self._photo_limit_reached(run.chat_id)
        ):
            started = await self._start_photo(
                run.chat_id,
                run.user_id,
                focus=focus,
                caption=scene_caption,
                happening=run.last_effect,
                mood=run.mood,
                alfred=decision.alfred if decision is not None else None,
                outside=await self._transylvania.outside(self._now()),
                message_thread_id=message_thread_id,
                trigger_message_id=None,
                dialogue_id=dialogue_id,
            )
            if started:
                run.photo_turn = run.turns_total
        await self._state.save_run(run)

    async def _after_canary_turn(
        self,
        scenario: Scenario,
        run: Run,
        decision: DirectorDecision | None,
        *,
        stage_before: int,
        finale_before: bool,
    ) -> None:
        """Ход канареечной сцены после решения Ведущего (59.1/59.2): исход
        обыска ушёл Ведущему, перенос Альфреда, финал и завершение. Всё это
        — решения кода, Ведущий лишь предлагает (``move_to``, ``finale``)."""
        user_id = run.user_id
        run.world.pop("search_outcome", None)  # Ведущий его уже получил
        if run.status == STATUS_IDLE:
            # Гость ушёл в свою тему — Альфред поднимается и скажет об этом сам.
            await self._end_scene_in_room(run)
            await self._go_home(user_id, announce=True)
            return
        # Финал мог наступить в этом же ходу через act: флаги уже выданы, а
        # завершается сцена только со следующего хода (Альфред успеет рассказать).
        fresh_finale = bool(run.world.pop("act_finale", None))
        await self._canary_progress(
            scenario,
            run,
            decision.move_to if decision is not None else None,
            stage_before=stage_before,
            finale_before=finale_before and not fresh_finale,
        )
        if run.finale and finale_before and not fresh_finale and scenario.finish_after_finale:
            run.status = STATUS_DONE
            await self._end_scene_in_room(run)

    async def _canary_progress(
        self,
        scenario: Scenario,
        run: Run,
        move: dict[str, str] | None,
        *,
        stage_before: int,
        finale_before: bool,
    ) -> None:
        """Следствия сдвига стадии/финала канареечной сцены — решения кода:
        вещи стадии, перенос Альфреда, флаги финала. Общая для хода сцены
        (_after_canary_turn) и тула act (Этап 59.7: применяется сразу)."""
        user_id = run.user_id
        for stage, kind_type in scenario.stage_items:
            if stage_before < stage <= run.stage:
                # Вошёл в подвал — бутылка вина теперь вещь гостя (59.3).
                await self._give_found_item(run, items_mod.KINDS[kind_type], origin="stage")
        if scenario.stage_places and run.stage != stage_before:
            await self._move_to(user_id, scenario.stage_places[min(run.stage, scenario.last_stage)])
        if move and run.stage in scenario.move_stages and not run.finale:
            await self._apply_move(scenario, run, move)
        if run.finale and not finale_before:
            # Финал наступил: сцена пройдена и открыто новое (флаги гостя).
            await self._state.mark_completed(scenario.id, user_id)
            for flag in scenario.finale_flags:
                await self._state.set_effect(flag, user_id, "1")

    async def _ask_director(
        self, scenario: Scenario, run: Run, cab: Cabinet
    ) -> DirectorDecision | None:
        node_link = self._get_node_link()
        if node_link is None:
            return None
        outside = await self._transylvania.outside(self._now())
        room = None
        if isinstance(cab, Room):
            room = (cab.canon.of_ru, cab.canon.in_ru)
        return await ask_director(
            node_link,
            LLM_ADDRESS,
            self._settings.llm.request_timeout_s,
            scenario,
            run,
            finale_allowed=finale_allowed(scenario, run),
            place=cab.describe_ru(),
            outside=outside.ru() if cab.windowed else None,
            need_features=(
                0 if cab.features or isinstance(cab, Room) else cabinet_mod.FIRST_FEATURES
            ),
            room=room,
        )
