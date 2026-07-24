"""Инструменты tool-calling для /ai (LLM_INTEGRATION_PLAN.md §7-8): calc,
get_weather, convert_currency, remind. Диспетчер цикла (bot/ai_flow.py)
тестируется отдельно в test_ai_flow.py — здесь только сами обработчики в
изоляции."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from sa_home_bot.bot import tools
from sa_home_bot.config import Settings, WeatherConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store

CHAT_ID = 111


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


@pytest.fixture(autouse=True)
def _clear_module_caches():
    # _GEOCODE_CACHE/_RATES_CACHE — на уровне модуля (кэш на время жизни
    # процесса, см. bot/tools.py), между тестами общий процесс — без сброса
    # второй тест с тем же городом/валютой получил бы результат первого.
    tools._GEOCODE_CACHE.clear()
    tools._RATES_CACHE.clear()
    yield
    tools._GEOCODE_CACHE.clear()
    tools._RATES_CACHE.clear()


def _ctx(
    store,
    settings=None,
    chat_id=CHAT_ID,
    *,
    dialogue_id=1,
    trigger_message_id=1,
    node_link=None,
    history=None,
):
    # ``store`` не используется ToolContext'ом напрямую (только remind
    # ходит по протоколу через node_link, см. bot/tools.py) — параметр
    # сохранён ради существующих вызовов ниже, не задействован здесь.
    del store
    return tools.ToolContext(
        chat_id=chat_id,
        dialogue_id=dialogue_id,
        trigger_message_id=trigger_message_id,
        settings=settings or Settings(),
        node_link=node_link,
        history=history if history is not None else [],
    )


class _FakeNodeLink:
    """Двойник ServiceLink для remind — фиксирует последний вызов command()
    без реального протокола/сети."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict, object]] = []
        self._raises = raises

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}, dst))
        if self._raises is not None:
            raise self._raises
        return {"task_id": 1}


# --- calc ---


async def test_calc_basic_arithmetic(store):
    assert await tools.tool_calc(_ctx(store), {"expression": "2 + 2"}) == "4"


async def test_calc_operator_precedence_and_parens(store):
    assert await tools.tool_calc(_ctx(store), {"expression": "(2 + 3) * 4"}) == "20"


async def test_calc_division_returns_float(store):
    assert await tools.tool_calc(_ctx(store), {"expression": "7 / 2"}) == "3.5"


async def test_calc_power(store):
    assert await tools.tool_calc(_ctx(store), {"expression": "2 ** 10"}) == "1024"


async def test_calc_pi_constant(store):
    assert await tools.tool_calc(_ctx(store), {"expression": "2 * pi"}) == "6.283185"


async def test_calc_e_constant(store):
    assert await tools.tool_calc(_ctx(store), {"expression": "e"}) == "2.718282"


async def test_calc_rounds_long_float_results(store):
    result = await tools.tool_calc(_ctx(store), {"expression": "1 / 3"})
    assert result == "0.333333"


async def test_calc_caret_as_power(store):
    # Живой баг 2026-07-24: модель пишет "1.5^2" (математическая нотация),
    # не питоновское "1.5**2" — тул должен понимать оба.
    assert await tools.tool_calc(_ctx(store), {"expression": "1.5^2"}) == "2.25"
    assert await tools.tool_calc(
        _ctx(store), {"expression": "2 * pi * 1.5^2 + 2 * pi * 1.5 * 2"}
    ) == str(round(2 * math.pi * 1.5**2 + 2 * math.pi * 1.5 * 2, 6))


async def test_calc_cylinder_surface_area_formula(store):
    # Живой баг 2026-07-24: модель раньше не могла посчитать формулу с π
    # через calc вообще (переменные были запрещены) — площадь поверхности
    # цилиндра (r=1.5, h=2): 2*pi*r*(r+h) = 2*pi*1.5*3.5 ≈ 32.9867.
    result = await tools.tool_calc(_ctx(store), {"expression": "2 * pi * 1.5 * (1.5 + 2)"})
    assert result.startswith("32.98")


async def test_calc_rejects_arbitrary_names(store):
    result = await tools.tool_calc(_ctx(store), {"expression": "x + 1"})
    assert result.startswith("ошибка")


async def test_calc_rejects_non_arithmetic_expression(store):
    result = await tools.tool_calc(
        _ctx(store), {"expression": "__import__('os').system('ls')"}
    )
    assert result.startswith("ошибка")


async def test_calc_rejects_empty_expression(store):
    assert await tools.tool_calc(_ctx(store), {"expression": ""}) == "ошибка: пустое выражение"


async def test_calc_division_by_zero(store):
    result = await tools.tool_calc(_ctx(store), {"expression": "1 / 0"})
    assert result.startswith("ошибка")


async def test_calc_rejects_missing_expression(store):
    result = await tools.tool_calc(_ctx(store), {})
    assert result.startswith("ошибка")


# --- get_weather ---


_GEOCODE_RESPONSE = {
    "results": [{"name": "Казань", "latitude": 55.79, "longitude": 49.12, "country": "Россия"}]
}
_FORECAST_RESPONSE = {
    "current": {
        "temperature_2m": 20.5,
        "apparent_temperature": 19.0,
        "wind_speed_10m": 3.0,
        "weather_code": 1,
    }
}


async def test_get_weather_not_configured_by_default(store):
    result = await tools.tool_get_weather(_ctx(store), {})
    assert "не настроена" in result


async def test_get_weather_returns_current_conditions(store, monkeypatch):
    def fake_get_json(url, timeout):
        if "geocoding-api" in url:
            assert "name=%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C" in url  # "Казань" URL-encoded
            return _GEOCODE_RESPONSE
        assert "latitude=55.79" in url and "longitude=49.12" in url
        return _FORECAST_RESPONSE

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Казань"))
    result = await tools.tool_get_weather(_ctx(store, settings), {})
    assert '"temperature_c": 20.5' in result
    assert "Казань, Россия" in result


async def test_get_weather_caches_geocoding_across_calls(store, monkeypatch):
    geocode_calls = 0

    def fake_get_json(url, timeout):
        nonlocal geocode_calls
        if "geocoding-api" in url:
            geocode_calls += 1
            return _GEOCODE_RESPONSE
        return _FORECAST_RESPONSE

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Казань"))
    ctx = _ctx(store, settings)
    await tools.tool_get_weather(ctx, {})
    await tools.tool_get_weather(ctx, {})
    assert geocode_calls == 1  # второй раз — из _GEOCODE_CACHE, без сети


async def test_get_weather_explicit_city_in_args_overrides_home(store, monkeypatch):
    # Живой баг 2026-07-24: тул раньше не принимал город аргументом вообще —
    # на "погода в Алматы" модель честно отвечала "умею только дома".
    def fake_get_json(url, timeout):
        if "geocoding-api" in url:
            assert "name=%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C" in url
            return _GEOCODE_RESPONSE
        return _FORECAST_RESPONSE

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Москва"))  # дом — другой город
    result = await tools.tool_get_weather(_ctx(store, settings), {"city": "Казань"})
    assert "Казань, Россия" in result


async def test_get_weather_falls_back_to_home_city_without_args(store, monkeypatch):
    def fake_get_json(url, timeout):
        if "geocoding-api" in url:
            assert "name=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0" in url  # "Москва"
            return {
                "results": [
                    {"name": "Москва", "latitude": 55.75, "longitude": 37.6, "country": "Россия"}
                ]
            }
        return _FORECAST_RESPONSE

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Москва"))
    result = await tools.tool_get_weather(_ctx(store, settings), {})
    assert "Москва, Россия" in result


async def test_get_weather_city_not_found(store, monkeypatch):
    def fake_get_json(url, timeout):
        return {"results": []}

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Несуществующгород"))
    result = await tools.tool_get_weather(_ctx(store, settings), {})
    assert "не удалось определить координаты" in result


async def test_get_weather_handles_geocoding_network_error(store, monkeypatch):
    def fake_get_json(url, timeout):
        raise OSError("boom")

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Казань"))
    result = await tools.tool_get_weather(_ctx(store, settings), {})
    assert "не удалось определить координаты" in result


async def test_get_weather_handles_forecast_network_error(store, monkeypatch):
    def fake_get_json(url, timeout):
        if "geocoding-api" in url:
            return _GEOCODE_RESPONSE
        raise OSError("boom")

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Казань"))
    result = await tools.tool_get_weather(_ctx(store, settings), {})
    assert "недоступен" in result


# --- convert_currency ---

_RATES_RESPONSE = {"result": "success", "base_code": "USD", "rates": {"USD": 1, "RUB": 78.42}}


async def test_convert_currency_computes_result(store, monkeypatch):
    def fake_get_json(url, timeout):
        assert "open.er-api.com/v6/latest/USD" in url
        return _RATES_RESPONSE

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    result = await tools.tool_convert_currency(
        _ctx(store), {"amount": 100, "from": "usd", "to": "rub"}
    )
    assert '"rate": 78.42' in result
    assert '"result": 7842.0' in result


async def test_convert_currency_caches_rates_across_calls(store, monkeypatch):
    fetch_calls = 0

    def fake_get_json(url, timeout):
        nonlocal fetch_calls
        fetch_calls += 1
        return _RATES_RESPONSE

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    ctx = _ctx(store)
    await tools.tool_convert_currency(ctx, {"amount": 1, "from": "USD", "to": "RUB"})
    await tools.tool_convert_currency(ctx, {"amount": 2, "from": "USD", "to": "RUB"})
    assert fetch_calls == 1  # второй раз — из _RATES_CACHE, без сети


async def test_convert_currency_unknown_target_code(store, monkeypatch):
    monkeypatch.setattr(tools, "_get_json_sync", lambda url, timeout: _RATES_RESPONSE)
    result = await tools.tool_convert_currency(
        _ctx(store), {"amount": 10, "from": "USD", "to": "XXX"}
    )
    assert "неизвестный код валюты" in result


async def test_convert_currency_handles_network_error(store, monkeypatch):
    def fake_get_json(url, timeout):
        raise OSError("boom")

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    result = await tools.tool_convert_currency(
        _ctx(store), {"amount": 10, "from": "USD", "to": "RUB"}
    )
    assert "недоступен" in result


async def test_convert_currency_rejects_non_numeric_amount(store):
    result = await tools.tool_convert_currency(
        _ctx(store), {"amount": "много", "from": "USD", "to": "RUB"}
    )
    assert result.startswith("ошибка")


async def test_convert_currency_rejects_missing_currency_codes(store):
    result = await tools.tool_convert_currency(_ctx(store), {"amount": 10, "from": "USD"})
    assert result.startswith("ошибка")


# --- get_time ---
#
# Живой баг 2026-07-24: модель сама считала разницу часовых поясов (Москва
# vs Казахстан) и ошибалась — тул должен считать её детерминированно, без
# сети (statically place -> IANA timezone + zoneinfo).


async def test_get_time_known_place_with_explicit_instant(store):
    result = await tools.tool_get_time(
        _ctx(store), {"place": "Казахстан", "at": "2026-07-24T20:28:00+03:00"}
    )
    data = json.loads(result)
    assert data["timezone"] == "Asia/Almaty"
    assert data["utc_offset"] == "+05:00"
    # 20:28 UTC+3 == 22:28 UTC+5 (Казахстан на 2 часа впереди Москвы)
    assert data["local_time"] == "2026-07-24 22:28"
    assert data["weekday"] == "пятница"


async def test_get_time_is_case_and_whitespace_insensitive(store):
    result = await tools.tool_get_time(
        _ctx(store), {"place": "  МОСКВА  ", "at": "2026-07-24T20:28:00+03:00"}
    )
    data = json.loads(result)
    assert data["timezone"] == "Europe/Moscow"
    assert data["local_time"] == "2026-07-24 20:28"


async def test_get_time_defaults_to_now_without_at(store, monkeypatch):
    fixed_now = datetime(2026, 7, 24, 17, 28, tzinfo=UTC)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(tools, "datetime", _FixedDatetime)
    result = await tools.tool_get_time(_ctx(store), {"place": "Москва"})
    data = json.loads(result)
    # 17:28 UTC == 20:28 UTC+3 (Москва)
    assert data["local_time"] == "2026-07-24 20:28"


async def test_get_time_unknown_place_is_honest_refusal(store):
    result = await tools.tool_get_time(_ctx(store), {"place": "Атлантида"})
    assert result.startswith("не знаю часовой пояс")


async def test_get_time_rejects_missing_place(store):
    result = await tools.tool_get_time(_ctx(store), {})
    assert result.startswith("ошибка")


async def test_get_time_rejects_naive_at(store):
    result = await tools.tool_get_time(
        _ctx(store), {"place": "Москва", "at": "2026-07-24T20:28:00"}
    )
    assert result.startswith("ошибка")


async def test_get_time_rejects_malformed_at(store):
    result = await tools.tool_get_time(
        _ctx(store), {"place": "Москва", "at": "не дата"}
    )
    assert result.startswith("ошибка")


# --- remind ---


async def test_remind_creates_task_for_future_time(store):
    link = _FakeNodeLink()
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(
        _ctx(store, node_link=link), {"when": when, "text": "полить цветы"}
    )
    assert "задача поставлена" in result
    assert len(link.calls) == 1
    action, args, dst = link.calls[0]
    assert action == tools.task_protocol.ACTION_CREATE
    assert dst.node == tools.task_protocol.NODE_ID
    assert dst.service == tools.task_protocol.SERVICE_NAME
    assert args["dst_node"] == tools.LLM_NODE
    assert args["dst_service"] == tools.LLM_SERVICE
    assert args["action"] == tools.task_protocol.ACTION_CHAT_LOOP
    assert args["meta"]["kind"] == tools.task_protocol.TASK_KIND_LLM_CHAT
    assert args["meta"]["chat_id"] == CHAT_ID
    assert "полить цветы" in args["args"]["messages"][-1]["content"]


async def test_remind_includes_history_snapshot_in_directive(store):
    link = _FakeNodeLink()
    history = [{"role": "user", "content": "привет"}, {"role": "assistant", "content": "привет!"}]
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await tools.tool_remind(
        _ctx(store, node_link=link, history=history), {"when": when, "text": "напомни"}
    )
    messages = link.calls[0][1]["args"]["messages"]
    assert messages[:2] == history


async def test_remind_rejects_past_time(store):
    link = _FakeNodeLink()
    when = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(_ctx(store, node_link=link), {"when": when, "text": "поздно"})
    assert result.startswith("ошибка")
    assert link.calls == []


async def test_remind_rejects_invalid_iso(store):
    result = await tools.tool_remind(
        _ctx(store, node_link=_FakeNodeLink()), {"when": "завтра", "text": "текст"}
    )
    assert result.startswith("ошибка")


async def test_remind_rejects_missing_text(store):
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(
        _ctx(store, node_link=_FakeNodeLink()), {"when": when, "text": ""}
    )
    assert result.startswith("ошибка")


async def test_remind_without_chat_id(store):
    result = await tools.tool_remind(
        _ctx(store, chat_id=None, node_link=_FakeNodeLink()), {"when": "x", "text": "x"}
    )
    assert "вне диалога" in result


async def test_remind_without_node_link(store):
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(_ctx(store, node_link=None), {"when": when, "text": "x"})
    assert "служба задач недоступна" in result


async def test_remind_reports_error_when_task_service_unreachable(store):
    link = _FakeNodeLink(raises=tools.ServiceUnavailableError("boom"))
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(_ctx(store, node_link=link), {"when": when, "text": "x"})
    assert result.startswith("внутренняя ошибка")
