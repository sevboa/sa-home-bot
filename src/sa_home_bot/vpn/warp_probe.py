from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from sa_home_bot.domain.hysteresis import reconcile_band
from sa_home_bot.domain.models import ALERTING, OK
from sa_home_bot.domain.policy import BAND_OVER, BAND_UNDER
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.utils.process import run
from sa_home_bot.vpn.protocol import EVENT_VPN_WARP_DOWN, EVENT_VPN_WARP_RECOVERED
from sa_home_bot.vpn.service_common import log

WARP_PROBE_URL = "https://www.google.com/generate_204"
WARP_PROBE_EXPECTED_CODE = "204"
WARP_CLEAR_THRESHOLD = 1


class WarpProbeMixin:
    async def _warp_path_alive(self) -> bool:
        warp = self._cfg.warp
        argv = [
            "curl",
            "-s",
            "-m",
            str(int(warp.probe_timeout_s)),
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "--socks5-hostname",
            f"127.0.0.1:{warp.probe_port}",
            WARP_PROBE_URL,
        ]
        try:
            result = await run(argv, timeout=warp.probe_timeout_s + 3.0)
        except (OSError, TimeoutError):
            return False
        return result.returncode == 0 and result.stdout_text.strip() == WARP_PROBE_EXPECTED_CODE

    async def probe_warp_once(self, now: datetime | None = None) -> None:
        warp = self._cfg.warp
        if warp is None:
            return
        now = now or utc_now()
        alive = await self._warp_path_alive()
        step = reconcile_band(
            BAND_UNDER if alive else BAND_OVER,
            prev_status=self._warp_status,
            prev_count=self._warp_count,
            alerting_since=self._warp_since,
            consecutive_to_alert=warp.probe_fail_threshold,
            consecutive_to_clear=WARP_CLEAR_THRESHOLD,
            now=now,
        )
        self._warp_alive = alive
        self._warp_status = step.status
        self._warp_count = step.consecutive_count
        self._warp_since = step.alerting_since
        if step.transitioned_to == ALERTING:
            await self._emit(EVENT_VPN_WARP_DOWN, self._where())
        elif step.transitioned_to == OK:
            await self._emit(EVENT_VPN_WARP_RECOVERED, self._where())

    def warp_state(self) -> dict[str, Any] | None:
        if self._cfg.warp is None or self._warp_alive is None:
            return None
        return {"alive": self._warp_alive, "down": self._warp_status == ALERTING}

    async def warp_probe_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.warp.probe_interval_s)
            try:
                await self.probe_warp_once()
            except Exception:
                log.exception("vpn: сбой пробы WARP")
