from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Callable

from sa_home_bot.config import VpnSeriesConfig
from sa_home_bot.node.vpn_probe_state import ProbeSlot
from sa_home_bot.vpn_series.measure import Sample, SeriesResult, classify


def build_curl_argv(slot: ProbeSlot, url: str, duration_s: float) -> list[str]:
    ip_path = shutil.which("ip") or "ip"
    curl = ["curl", "-s", "-o", "-", "-m", str(int(duration_s))]
    if slot.transport == "reality":
        curl += ["--socks5-hostname", f"127.0.0.1:{slot.socks_port}"]
    curl.append(url)
    return ["sudo", "-n", ip_path, "netns", "exec", slot.netns, *curl]


async def stream_samples(
    argv: list[str],
    *,
    duration_s: float,
    tick_s: float,
    spawn: Callable[..., asyncio.subprocess.Process] = asyncio.create_subprocess_exec,
) -> tuple[list[Sample], int | None]:
    proc = await spawn(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    samples: list[Sample] = []
    total = 0
    start = time.monotonic()
    next_tick = start + tick_s
    deadline = start + duration_s + 5.0
    process_ended = False

    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                proc.kill()
                await proc.wait()
                return samples, None

            if now >= next_tick:
                samples.append((now - start, total))
                next_tick += tick_s

            if not process_ended:
                try:
                    timeout = min(tick_s, deadline - now)
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(65536), timeout=timeout
                    )
                except TimeoutError:
                    continue

                if not chunk:
                    process_ended = True
                    continue

                total += len(chunk)
                samples.append((time.monotonic() - start, total))
            else:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=min(tick_s, deadline - now))
                except TimeoutError:
                    continue
                break

        return samples, proc.returncode
    except Exception:
        proc.kill()
        await proc.wait()
        raise


async def run_single(
    slot: ProbeSlot,
    cfg: VpnSeriesConfig,
    *,
    spawn: Callable[..., asyncio.subprocess.Process] = asyncio.create_subprocess_exec,
) -> SeriesResult:
    argv = build_curl_argv(slot, cfg.url, cfg.duration_s)
    samples, rc = await stream_samples(
        argv,
        duration_s=cfg.duration_s,
        tick_s=cfg.tick_s,
        spawn=spawn,
    )
    return classify(
        samples, freeze_idle_s=cfg.freeze_idle_s, min_kbps=cfg.min_kbps, curl_exit=rc
    )


async def run_burst(
    slot: ProbeSlot,
    cfg: VpnSeriesConfig,
    *,
    spawn: Callable[..., asyncio.subprocess.Process] = asyncio.create_subprocess_exec,
) -> list[SeriesResult]:
    tasks = [run_single(slot, cfg, spawn=spawn) for _ in range(cfg.burst_connections)]
    return await asyncio.gather(*tasks)
