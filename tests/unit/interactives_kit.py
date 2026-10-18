from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime

from sa_home_bot.bot.interactives import cellar, keyboards, underground
from sa_home_bot.bot.interactives.base import STATUS_ACTIVE, STATUS_OFFERED, InteractiveStore, Run
from sa_home_bot.bot.interactives.director import FEATURES_SYSTEM
from sa_home_bot.bot.interactives.engine import Interactives
from sa_home_bot.bot.interactives.transylvania import Transylvania
from sa_home_bot.config import LlmConfig, Settings
from tests.unit.support.factories import png_bytes
from tests.unit.support.fakes import Clock, FakeNotifier

CANARY = 801
GUEST = 802
KNOCK = "Какая сегодня погода?"
DEFAULT_NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


class Dice:
    def __init__(self, *values: float) -> None:
        self.values = list(values) or [0.99]

    def __call__(self) -> float:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


async def clear_sky() -> int:
    return 0


class ScriptedLink:
    def __init__(
        self,
        *,
        features: tuple[str, ...] | list[str] | None = None,
        progress_done: bool = True,
    ) -> None:
        self.features = list(features) if features is not None else None
        self.progress_done = progress_done
        self.calls: list[tuple[str, dict]] = []
        self.director_replies: list[str] = []
        self.director_inputs: list[str] = []
        self.director_systems: list[str] = []
        self.describe_inputs: list[str] = []
        self.describe_fail = False
        self.generated: list[dict] = []
        self.inspect: dict | None = None

    async def command(self, action, args, dst=None, timeout=None):
        self.calls.append((action, args))
        if action == "chat" and args.get("role") == "director":
            return self._director(args)
        if action == "chat":
            return {"response": "Готово, сэр."}
        if action == "generate_image":
            self.generated.append(args)
            return {
                "png_b64": base64.b64encode(png_bytes()).decode(),
                "width": 8,
                "height": 8,
                "prompt": "p",
                "seed": 7,
                **({"inspect": self.inspect} if self.inspect is not None else {}),
            }
        if action == "chat_progress":
            return {"partial": "draw", "done": self.progress_done}
        if action == "item_portrait":
            return {
                "png_b64": base64.b64encode(png_bytes()).decode(),
                "width": 8,
                "height": 8,
                "seed": args["seed"],
                "full_prompt": args["prompt"],
                "seen": "Старое радио с микрофоном.",
                "missing": [],
            }
        raise AssertionError(action)

    def _director(self, args):
        system = args.get("system")
        content = args["messages"][0]["content"]
        if system == FEATURES_SYSTEM and self.features is not None:
            return {"response": json.dumps({"cabinet_add": self.features}, ensure_ascii=False)}
        if system == underground.DESCRIBE_SYSTEM:
            self.describe_inputs.append(content)
            if self.describe_fail:
                raise TimeoutError
            n = len(self.describe_inputs)
            return {
                "response": json.dumps(
                    {
                        "passage": {
                            "describe": f"Коридор {n} с рыжими потёками",
                            "spots": ["ящик у стены"],
                        },
                        "node": {"describe": f"Перекрёсток {n}, на полу кости", "spots": []},
                    },
                    ensure_ascii=False,
                )
            }
        self.director_systems.append(system)
        self.director_inputs.append(content)
        return {"response": self.director_replies.pop(0) if self.director_replies else "{}"}

    def portraits(self) -> list[dict]:
        return [a for act, a in self.calls if act == "item_portrait"]

    def lines(self) -> list[str]:
        return [
            a["messages"][0]["content"]
            for act, a in self.calls
            if act == "chat" and "system" not in a
        ]


class CabinetLink(ScriptedLink):
    def __init__(self, features=("чучело совы на шкафу", "треснувший портрет")) -> None:
        super().__init__(features=features, progress_done=False)


def make_service(
    store,
    *,
    canary=(CANARY,),
    dice=None,
    clock=None,
    link=None,
    notifier=None,
    llm=None,
    record_speech=True,
    **service_options,
):
    notifier = notifier or FakeNotifier()
    link = link or ScriptedLink()
    options = {
        "now": clock or Clock(DEFAULT_NOW),
        "choose": lambda options: options[0],
        "rng": dice or Dice(0.99),
        "transylvania": Transylvania(fetch=clear_sky),
        **service_options,
    }
    svc = Interactives(
        store,
        notifier,
        Settings(
            llm=LlmConfig(model="m", interactives_canary_user_ids=list(canary), **(llm or {}))
        ),
        lambda: link,
        **options,
    )
    if record_speech:
        svc.spoken = []

        async def speak(chat_id, directive, where):
            svc.spoken.append((directive, where))

        svc._speak = speak
    return svc, notifier, link


async def drain(svc) -> None:
    await asyncio.gather(*list(svc._photo_tasks))


async def run_turn(svc, text=KNOCK, reply="Ответ Альфреда", *, chat=CANARY, user=CANARY):
    plan = await svc.before_turn(chat, user, text, is_private=True)
    await svc.after_turn(plan, reply, dialogue_id=77)
    await svc.flush_forms(chat, plan, dialogue_id=77)
    return plan


async def cellar_run(store, chat=CANARY) -> Run | None:
    return await InteractiveStore(store).load_run(chat, cellar.SCENARIO_ID)


def director_reply(**fields) -> str:
    data = {"active": True, "stage": 0, "effect": None, "directive": None, "finale": False}
    data.update(fields)
    return json.dumps(data, ensure_ascii=False)


def act_reply(result="Ничего не происходит.", **fields) -> str:
    data = {"result": result, "stage": None, "facts_add": [], "facts_remove": []}
    data.update(fields)
    return json.dumps(data, ensure_ascii=False)


async def reach_offer(svc, store):
    for _ in range(3):
        await run_turn(svc)
    svc._rng = Dice(0.0)
    plan = await run_turn(svc)
    assert plan.offered and plan.scenario == cellar.SCENARIO_ID
    assert (await cellar_run(store)).status == STATUS_OFFERED


async def reach_active(svc, store):
    await reach_offer(svc, store)
    answer = await svc.handle_click(CANARY, CANARY, cellar.SCENARIO_ID, keyboards.BTN_PLAY)
    assert answer[0] == "Хорошо."
    assert (await cellar_run(store)).status == STATUS_ACTIVE


async def reach_key_hunt(svc, store, link):
    await reach_active(svc, store)
    link.director_replies = [director_reply(stage=1), director_reply(stage=2)]
    await run_turn(svc)
    await run_turn(svc)
    assert (await cellar_run(store)).stage == 2


async def reach_hatch(svc, store, link):
    await reach_key_hunt(svc, store, link)
    run = await cellar_run(store)
    run.world["key_found"] = True
    await svc._state.save_run(run)
    link.director_replies = [
        director_reply(stage=3, effect="Замок поддался; у входа бутылка вина.")
    ]
    await run_turn(svc, "Ну?")
    assert (await cellar_run(store)).stage == 3
    link.director_replies = [director_reply(stage=4)]
    await run_turn(svc, "Поищи ещё вина")
    assert (await cellar_run(store)).stage == 4
