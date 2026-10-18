from __future__ import annotations

import pytest

from sa_home_bot.bot import tools
from sa_home_bot.bot.tools import currency
from tests.unit.tools_support import (
    make_ctx as _ctx,
)


@pytest.fixture(autouse=True)
def _clear_rates_cache():
    currency._RATES_CACHE.clear()
    yield
    currency._RATES_CACHE.clear()


# --- convert_currency ---

_RATES_RESPONSE = {"result": "success", "base_code": "USD", "rates": {"USD": 1, "RUB": 78.42}}


async def test_convert_currency_computes_result(monkeypatch):
    def fake_get_json(url, timeout, headers=None):
        assert "open.er-api.com/v6/latest/USD" in url
        return _RATES_RESPONSE

    monkeypatch.setattr("sa_home_bot.utils.http.get_json", fake_get_json)
    result = await tools.tool_convert_currency(_ctx(), {"amount": 100, "from": "usd", "to": "rub"})
    assert '"rate": 78.42' in result
    assert '"result": 7842.0' in result


async def test_convert_currency_caches_rates_across_calls(monkeypatch):
    fetch_calls = 0

    def fake_get_json(url, timeout, headers=None):
        nonlocal fetch_calls
        fetch_calls += 1
        return _RATES_RESPONSE

    monkeypatch.setattr("sa_home_bot.utils.http.get_json", fake_get_json)
    ctx = _ctx()
    await tools.tool_convert_currency(ctx, {"amount": 1, "from": "USD", "to": "RUB"})
    await tools.tool_convert_currency(ctx, {"amount": 2, "from": "USD", "to": "RUB"})
    assert fetch_calls == 1  # второй раз — из _RATES_CACHE, без сети


async def test_convert_currency_unknown_target_code(monkeypatch):
    monkeypatch.setattr(
        "sa_home_bot.utils.http.get_json", lambda url, timeout, headers=None: _RATES_RESPONSE
    )
    result = await tools.tool_convert_currency(_ctx(), {"amount": 10, "from": "USD", "to": "XXX"})
    assert "неизвестный код валюты" in result


async def test_convert_currency_handles_network_error(monkeypatch):
    def fake_get_json(url, timeout, headers=None):
        raise OSError("boom")

    monkeypatch.setattr("sa_home_bot.utils.http.get_json", fake_get_json)
    result = await tools.tool_convert_currency(_ctx(), {"amount": 10, "from": "USD", "to": "RUB"})
    assert "недоступен" in result


@pytest.mark.parametrize(
    "args",
    [{"amount": "много", "from": "USD", "to": "RUB"}, {"amount": 10, "from": "USD"}],
    ids=["non-numeric-amount", "missing-currency-code"],
)
async def test_convert_currency_rejects(args):
    assert (await tools.tool_convert_currency(_ctx(), args)).startswith("ошибка")
