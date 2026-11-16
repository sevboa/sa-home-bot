from __future__ import annotations

import asyncio
import json
import urllib.request

from sa_home_bot.utils.http import JSON_FETCH_ERRORS

SWITCH_TIMEOUT_S = 5.0


def switch_blockers(status: dict, webui_model: str, webui_quiet_s: float) -> list[str]:
    blockers: list[str] = []
    serving = status.get("serving") or []
    active = status.get("active") or 0
    if active > 0:
        if serving:
            blockers.append("модели: " + ", ".join(str(item) for item in serving))
        else:
            blockers.append(f"модели: {active} запросов")
    if status.get("waiting"):
        blockers.append("модели: ждут запуска")
    companion = status.get("companion")
    if companion is not None and (companion.get("active") or 0) > 0:
        blockers.append("Альфред отвечает")
    if webui_model:
        ago = (status.get("requested_ago_s") or {}).get(webui_model)
        if ago is not None and ago < webui_quiet_s:
            blockers.append(f"webui: писали {int(ago)} с назад")
    return blockers


async def fetch_switch_status(url: str) -> dict | None:
    def load() -> dict:
        with urllib.request.urlopen(url, timeout=SWITCH_TIMEOUT_S) as response:
            return json.load(response)

    try:
        raw = await asyncio.to_thread(load)
    except JSON_FETCH_ERRORS:
        return None
    return raw if isinstance(raw, dict) else None
