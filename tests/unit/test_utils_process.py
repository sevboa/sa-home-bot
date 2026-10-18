from __future__ import annotations

import asyncio
import sys

import pytest

from sa_home_bot.utils.process import run


async def test_run_captures_streams_and_code():
    script = "import sys; print('out'); print('err ', file=sys.stderr); sys.exit(3)"
    result = await run([sys.executable, "-c", script])
    assert result.returncode == 3
    assert result.stdout_text == "out\n"
    assert result.stderr_text == "err"


async def test_run_feeds_input():
    script = "import sys; print(sys.stdin.read().upper())"
    result = await run([sys.executable, "-c", script], input=b"abc")
    assert result.stdout_text.strip() == "ABC"


async def test_run_devnull_streams_are_empty():
    result = await run(
        [sys.executable, "-c", "print(1)"],
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


async def test_run_timeout_and_missing_binary():
    with pytest.raises(TimeoutError):
        await run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
    with pytest.raises(OSError):
        await run(["/nonexistent/binary"])
