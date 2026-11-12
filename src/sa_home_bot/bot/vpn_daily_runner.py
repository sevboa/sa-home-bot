from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from typing import Any

from sa_home_bot.bot.away import AwayService
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_errors import UNREACHABLE_ERRORS
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.vpn_daily import build_caption, build_panels, outage_episodes
from sa_home_bot.bot.vpn_nodes import SERVICE_NAME, known_server_label, live_vpn_nodes
from sa_home_bot.bot.vpn_report_chart import render_panel
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.llm_chat import ACTION_CHAT
from sa_home_bot.proto.addresses import LLM_ADDRESS, Address
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.utils.text import clip
from sa_home_bot.utils.timefmt import zone_or_none
from sa_home_bot.vpn import protocol as vpn_protocol

log = logging.getLogger(__name__)

STATE_KEY = "vpn_daily_report_sent"
POLL_S = 60.0
CALL_TIMEOUT_S = 30.0
CONCLUSION_MAX_CHARS = 300
CONCLUSION_INSTRUCTION = (
    "Скажи хозяину одно-два коротких предложения в своём образе о том, как за прошедшие "
    "сутки работал VPN. Сводка: {facts} Цифры не перечисляй, без списков и без приветствия."
)
TAG_PATTERN = re.compile(r"<[^>]+>")


def fire_time(now: datetime, time_text: str, tz: tzinfo) -> datetime:
    hour, minute = (int(part) for part in time_text.split(":"))
    return now.astimezone(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)


def local_day(now: datetime, tz: tzinfo) -> str:
    return now.astimezone(tz).date().isoformat()


def is_due(now: datetime, time_text: str, tz: tzinfo, last_sent: str | None) -> bool:
    return now >= fire_time(now, time_text, tz) and last_sent != local_day(now, tz)


def plain_facts(caption: str) -> str:
    return html.unescape(TAG_PATTERN.sub("", caption))


class VpnDailyReport:
    def __init__(
        self,
        *,
        get_node_link: Callable[[], ServiceLink | None],
        store: Store,
        config: Settings,
        book: Any,
        notifier: Notifier,
    ) -> None:
        self._get_node_link = get_node_link
        self._store = store
        self._config = config
        self._book = book
        self._notifier = notifier

    @property
    def _tz(self) -> tzinfo:
        return zone_or_none(self._config.vpn_report.tz) or UTC

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("vpn_daily: проход не удался")
            await asyncio.sleep(POLL_S)

    async def tick(self, now: datetime | None = None) -> bool:
        now = now or utc_now()
        last = await self._store.get_state(STATE_KEY)
        if not is_due(now, self._config.vpn_report.time, self._tz, last):
            return False
        sent = await self.send_report(now)
        if sent:
            await self._store.set_state(STATE_KEY, local_day(now, self._tz))
        return sent

    async def send_report(self, now: datetime) -> bool:
        node_link = self._get_node_link()
        if node_link is None:
            return False
        data = await self._fetch(node_link)
        if data is None:
            return False
        history, scopes = data
        subs = self._book.accepting(vpn_protocol.EVENT_VPN_DAILY_REPORT)
        if not subs:
            return True
        panels = build_panels(history, scopes, now=now, tz=self._tz, label_of=known_server_label)
        episodes = outage_episodes(
            history, scopes, now=now, tz=self._tz, label_of=known_server_label
        )
        images = [await asyncio.to_thread(render_panel, panel) for panel in panels]
        facts = plain_facts(
            build_caption(history, scopes, label_of=known_server_label, episodes=episodes)
        )
        caption = build_caption(
            history,
            scopes,
            label_of=known_server_label,
            conclusion=await self._compose_conclusion(node_link, facts),
            episodes=episodes,
        )
        delivered = 0
        for sub in subs:
            if await self._notifier.send_media_group(sub.chat_id, images, caption=caption):
                delivered += 1
        return delivered > 0

    async def _fetch(self, node_link: ServiceLink) -> tuple[dict, dict] | None:
        cfg = self._config.vpn_report
        for node in await live_vpn_nodes(node_link):
            dst = Address(node=node, service=SERVICE_NAME)
            try:
                history = await node_link.command(
                    vpn_protocol.ACTION_CHECK_HISTORY,
                    {"hours": cfg.hours, "bucket_min": cfg.bucket_min},
                    dst=dst,
                    timeout=CALL_TIMEOUT_S,
                )
                scopes = await node_link.command(
                    vpn_protocol.ACTION_CHECK_SCOPES, {}, dst=dst, timeout=CALL_TIMEOUT_S
                )
            except UNREACHABLE_ERRORS as exc:
                log.warning("vpn_daily: %s не отдала данные: %s", node, exc)
                continue
            if isinstance(history, dict) and isinstance(scopes, dict):
                return history, scopes
        return None

    async def _compose_conclusion(self, node_link: ServiceLink, facts: str) -> str | None:
        try:
            if await AwayService(self._store, self._config).load() is not None:
                return None
            result = await node_link.command(
                ACTION_CHAT,
                {
                    "messages": [
                        {"role": "user", "content": CONCLUSION_INSTRUCTION.format(facts=facts)}
                    ],
                    "role": "persona",
                    "think": False,
                },
                dst=LLM_ADDRESS,
                timeout=self._config.vpn_report.llm_timeout_s,
            )
            if not isinstance(result, dict) or result.get("done_reason") == "length":
                return None
            raw = str(result.get("response") or "").strip()
            return clip(raw, CONCLUSION_MAX_CHARS, at_word=True) if raw else None
        except Exception:
            log.warning("vpn_daily: заключение не получено", exc_info=True)
            return None
