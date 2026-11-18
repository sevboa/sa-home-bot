from __future__ import annotations

from sa_home_bot.bot.away import MAX_AWAY, SET_BY_ADMIN, AwayError, AwayService, local_tz
from sa_home_bot.silence import SilenceService, SilenceState

SILENCE_AWAY_REASON = "тишина видеокарты"


def until_text(state: SilenceState) -> str:
    if state.until is None:
        return "до /silent off"
    return "до " + state.until.astimezone(local_tz()).strftime("%d.%m %H:%M")


async def start_silence(
    silence: SilenceService,
    away: AwayService | None,
    minutes: int | None,
    set_by: str,
) -> SilenceState:
    state = await silence.start(minutes, set_by=set_by)
    if away is not None:
        target = state.until or away.now() + MAX_AWAY
        current = await away.load()
        try:
            if current is None:
                await away.start(target, set_by=SET_BY_ADMIN, reason=SILENCE_AWAY_REASON)
            elif current.until < target:
                await away.extend(target - current.until)
        except AwayError:
            pass
    return state


async def away_for_silence(away: AwayService | None) -> bool:
    if away is None:
        return False
    current = await away.load()
    return current is not None and current.reason == SILENCE_AWAY_REASON
