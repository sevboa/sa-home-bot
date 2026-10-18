from __future__ import annotations

from sa_home_bot.bot.interactives import cellar, places
from sa_home_bot.bot.interactives.base import STATUS_ACTIVE, STATUS_OFFERED
from sa_home_bot.bot.interactives.registry import REGISTRY
from sa_home_bot.bot.interactives.underground_flow import UndergroundMixin


class DebugMixin(UndergroundMixin):
    DEBUG_USAGE = "Использование: /interactives cellar | reset cellar|catacombs|all | where"

    async def debug_cellar(self, chat_id: int, user_id: int) -> str:
        if not self.canary_ok(user_id):
            return self.DEBUG_USAGE
        scenario = REGISTRY[cellar.SCENARIO_ID]
        run = await self._state.load_run(chat_id, scenario.id)
        if run is not None and run.status in (STATUS_OFFERED, STATUS_ACTIVE):
            return "Подвал уже на ходу (форма висит или сцена идёт). Начать заново — reset cellar."
        if await self._state.is_completed(scenario.id, user_id):
            return "Подвал уже пройден. Начать заново — reset cellar."
        await self._offer(scenario, run, chat_id, user_id, None)
        await self.flush_forms(chat_id)
        return "Стук отправлен."

    async def debug_reset(self, chat_id: int, user_id: int, scope: str) -> str:
        if not self.canary_ok(user_id):
            return self.DEBUG_USAGE
        if scope not in places.RESET_SCOPES:
            return "Что стереть: reset cellar | catacombs | all"
        async with self._frames.lock:
            removed = await places.reset_progress(self._store, user_id, scope, chat_id)
        await self.dismiss_buttons(chat_id, user_id)
        return f"Сброшено ({scope}): {len(removed)} запис." if removed else "Нечего сбрасывать."

    async def debug_where(self, user_id: int) -> str:
        if not self.canary_ok(user_id):
            return self.DEBUG_USAGE
        raw = await self._store.get_state(places.at_key(user_id))
        if not raw:
            return "alfred_at не задан: Альфред в кабинете."
        return raw + await self.underground_debug(user_id)
