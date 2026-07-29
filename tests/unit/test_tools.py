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
from sa_home_bot.subscriptions.models import Subscription

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
    subscription=None,
    dismissal=None,
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
        subscription=subscription,
        dismissal=dismissal,
    )


def _sub(*allowed: str) -> Subscription:
    return Subscription(chat_id=CHAT_ID, name="me", allowed_commands=frozenset(allowed))


ADMIN = _sub("*")


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


async def test_get_time_greenwich_is_fixed_utc_not_london_dst(store):
    # Живая находка 2026-07-24: "по Гринвичу" в быту значит "по UTC"
    # (всегда 0), не гражданское время обсерватории (Europe/London уходит
    # в BST летом) — проверяем на летней дате, где разница была бы видна.
    result = await tools.tool_get_time(
        _ctx(store), {"place": "Гринвич", "at": "2026-07-24T20:28:00+03:00"}
    )
    data = json.loads(result)
    assert data["timezone"] == "UTC"
    assert data["utc_offset"] == "+00:00"
    assert data["local_time"] == "2026-07-24 17:28"


async def test_get_time_italy(store):
    result = await tools.tool_get_time(
        _ctx(store), {"place": "Италия", "at": "2026-07-24T20:28:00+03:00"}
    )
    data = json.loads(result)
    assert data["timezone"] == "Europe/Rome"
    assert data["local_time"] == "2026-07-24 19:28"  # летнее CEST = UTC+2


# --- get_time: places (сравнение нескольких мест, разница) ---
#
# Живая находка 2026-07-24: на вопрос "разница между Москвой и Италией"
# модель считала сама и путалась (то 2 часа не в ту сторону, то не в ту) —
# теперь places считает разницу детерминированно, моделью не пересчитывается.


async def test_get_time_places_returns_each_place_and_differences(store):
    result = await tools.tool_get_time(
        _ctx(store),
        {"places": ["Москва", "Италия", "Казахстан"], "at": "2026-07-24T20:28:00+03:00"},
    )
    data = json.loads(result)
    by_place = {p["place"]: p for p in data["places"]}
    assert by_place["Москва"]["utc_offset"] == "+03:00"
    assert by_place["Италия"]["utc_offset"] == "+02:00"
    assert by_place["Казахстан"]["utc_offset"] == "+05:00"
    # Москва впереди Италии на 1 ч (MSK+3 vs CEST+2), Казахстан впереди
    # обоих (+5) — раньше модель сама считала эту разницу и ошибалась.
    # Места отдаются в том же (именительном) виде, в каком их передали —
    # тул не склоняет, это на совести модели при пересказе.
    assert any("Москва впереди Италия на 1 ч" in d for d in data["differences"])
    assert any("Казахстан впереди Москва на 2 ч" in d for d in data["differences"])


async def test_get_time_places_single_entry_behaves_like_place(store):
    # places с одним элементом — тот же плоский формат, что и place.
    result_places = await tools.tool_get_time(
        _ctx(store), {"places": ["Москва"], "at": "2026-07-24T20:28:00+03:00"}
    )
    result_place = await tools.tool_get_time(
        _ctx(store), {"place": "Москва", "at": "2026-07-24T20:28:00+03:00"}
    )
    assert result_places == result_place


async def test_get_time_places_partial_unknown_still_answers_known(store):
    result = await tools.tool_get_time(
        _ctx(store),
        {"places": ["Москва", "Атлантида"], "at": "2026-07-24T20:28:00+03:00"},
    )
    data = json.loads(result)
    assert [p["place"] for p in data["places"]] == ["Москва"]
    assert data["unknown_places"] == ["Атлантида"]


async def test_get_time_places_all_unknown_is_honest_refusal(store):
    result = await tools.tool_get_time(_ctx(store), {"places": ["Атлантида", "Нарния"]})
    assert result.startswith("не знаю часовой пояс")


async def test_get_time_places_same_offset_reports_no_difference(store):
    result = await tools.tool_get_time(
        _ctx(store),
        {"places": ["Казахстан", "Алматы"], "at": "2026-07-24T20:28:00+03:00"},
    )
    data = json.loads(result)
    assert "одинаковое время" in data["differences"][0]


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


# --- права: комплект тулов собирается под подписку собеседника ---


def _names(subscription) -> list[str]:
    return [d["function"]["name"] for d in tools.tools_for(subscription).declarations]


def _enum(subscription) -> list[str]:
    """Значения what, которые видит модель у swarm_status при этих правах."""
    decl = next(
        d
        for d in tools.tools_for(subscription).declarations
        if d["function"]["name"] == "swarm_status"
    )
    return decl["function"]["parameters"]["properties"]["what"]["enum"]


def test_tools_for_admin_gets_everything():
    assert set(_names(ADMIN)) == {s.name for s in tools.TOOLS}


def test_tools_for_none_is_fail_closed():
    """Подписки нет вовсе — остаются только тулы без прав. Так бывает у
    службы tasks, если подписку удалили, пока задача ждала в очереди."""
    names = _names(None)
    assert "swarm_status" not in names
    assert set(names) == {s.name for s in tools.TOOLS if s.requires is None and s.variants is None}


def test_tools_for_none_gives_no_handler_either():
    """Фильтрация не только в декларациях: выдуманное имя не должно найти
    обработчик (иначе права были бы лишь подсказкой модели)."""
    assert "swarm_status" not in tools.tools_for(None).handlers


def test_swarm_status_hidden_without_any_status_right():
    """Ни /status, ни /nodes, ни торренты — тула нет вовсе, а не «есть, но
    отказывает»: требование «Альфред не умеет, а не отказывает»."""
    assert "swarm_status" not in _names(_sub("ai"))


def test_swarm_status_enum_narrowed_to_granted_variants():
    assert _enum(_sub("nodes")) == ["nodes"]


def test_filtering_does_not_mutate_shared_declaration():
    """enum режется на копии — иначе первый же ограниченный собеседник
    испортил бы декларацию для всех последующих."""
    _enum(_sub("nodes"))
    assert _enum(ADMIN) == ["nodes", "health", "disks"]


# --- swarm_status: сам обработчик (сбор данных через wake_core) ---


_OWN_STATE = {
    "node": "alfred",
    "kind": "server",
    "version": "0.38.1",
    "system_uptime_s": 7200,
    "services": [
        {"name": "monitor", "status": "running"},
        {"name": "torrents", "status": "running"},
    ],
    "peers": [{"id": "winpc", "alive": False, "kind": "workstation"}],
}

_ALFRED_MONITOR = {
    "health": [
        {
            "component_id": "cpu:pkg",
            "kind": "cpu",
            "label": "CPU",
            "status": "ok",
            "temperature_c": 37.0,
        }
    ],
    "disks": [
        {"label": "eMMC", "kind": "emmc", "health": None, "free_bytes": 51000000000},
    ],
    "requirements": [],
}


class _FakeSwarmLink:
    """Двойник ServiceLink: маршрутизирует get_state/command по dst."""

    def __init__(self, states=None, command_result=None, command_raises=None):
        self._states = states or {}
        self._command_result = command_result if command_result is not None else {"torrents": []}
        self._command_raises = command_raises
        self.commands: list[tuple[str, object]] = []
        self.sent_args: list[dict] = []

    async def get_state(self, dst=None):
        key = "own" if dst is None else f"{dst.node}:{dst.service}"
        if key in self._states:
            return self._states[key]
        raise tools.ServiceUnavailableError("нет связи")

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.commands.append((action, dst))
        self.sent_args.append(args or {})
        if self._command_raises is not None:
            raise self._command_raises
        return self._command_result


def _swarm_link(**kwargs):
    states = {"own": _OWN_STATE, "alfred:monitor": _ALFRED_MONITOR}
    states.update(kwargs.pop("states", {}))
    return _FakeSwarmLink(states=states, **kwargs)


async def test_swarm_status_nodes_marks_sleeping_workstation_as_normal(store):
    link = _swarm_link()
    raw = await tools.tool_swarm_status(
        _ctx(store, node_link=link, subscription=ADMIN), {"what": "nodes"}
    )
    nodes = {n["node"]: n for n in json.loads(raw)["nodes"]}
    assert nodes["alfred"]["online"] is True
    assert nodes["alfred"]["services_running"] == 2
    # winpc — рабочая станция: выключена = норма, а не авария (ARCH §11 п. 4).
    assert nodes["winpc"]["online"] is False
    assert nodes["winpc"]["sleeping_is_normal"] is True


async def test_swarm_status_offline_always_on_node_is_not_normal(store):
    own = {**_OWN_STATE, "peers": [{"id": "jeeves", "alive": False, "kind": "vps"}]}
    link = _swarm_link(states={"own": own})
    raw = await tools.tool_swarm_status(
        _ctx(store, node_link=link, subscription=ADMIN), {"what": "nodes"}
    )
    jeeves = next(n for n in json.loads(raw)["nodes"] if n["node"] == "jeeves")
    assert jeeves["sleeping_is_normal"] is False


async def test_swarm_status_health_returns_temperatures(store):
    link = _swarm_link()
    raw = await tools.tool_swarm_status(
        _ctx(store, node_link=link, subscription=ADMIN), {"what": "health", "node": "alfred"}
    )
    entry = json.loads(raw)["health"][0]
    assert entry["node"] == "alfred"
    assert entry["components"][0]["temperature_c"] == 37.0


async def test_swarm_status_disks_returns_free_space(store):
    link = _swarm_link()
    raw = await tools.tool_swarm_status(
        _ctx(store, node_link=link, subscription=ADMIN), {"what": "disks", "node": "alfred"}
    )
    disk = json.loads(raw)["disks"][0]["disks"][0]
    assert disk["label"] == "eMMC"
    assert disk["free_bytes"] == 51000000000


async def test_swarm_status_rejects_variant_without_right(store):
    """Модель может передать значение, которого не было в её enum — тул
    сверяется с подпиской повторно, а не доверяет декларации."""
    link = _swarm_link()
    result = await tools.tool_swarm_status(
        _ctx(store, node_link=link, subscription=_sub("nodes")), {"what": "disks"}
    )
    assert result.startswith("не умею")
    assert link.commands == []


async def test_swarm_status_without_subscription_refuses(store):
    result = await tools.tool_swarm_status(
        _ctx(store, node_link=_swarm_link(), subscription=None), {"what": "nodes"}
    )
    assert result.startswith("не умею")


async def test_swarm_status_unknown_node_lists_known_ones(store):
    link = _swarm_link()
    result = await tools.tool_swarm_status(
        _ctx(store, node_link=link, subscription=ADMIN), {"what": "nodes", "node": "нету"}
    )
    assert "нет такой ноды" in result
    assert "alfred" in result and "winpc" in result


async def test_swarm_status_own_node_down(store):
    link = _FakeSwarmLink(states={})
    result = await tools.tool_swarm_status(
        _ctx(store, node_link=link, subscription=ADMIN), {"what": "nodes"}
    )
    assert result.startswith("недоступно")


# --- torrents: закачки целиком (список, место, magnet, пауза/запуск) ---


def _tor_enum(subscription) -> list[str]:
    """Значения action, которые видит модель у тула torrents."""
    decl = next(
        (
            d
            for d in tools.tools_for(subscription).declarations
            if d["function"]["name"] == "torrents"
        ),
        None,
    )
    if decl is None:
        return []
    return decl["function"]["parameters"]["properties"]["action"]["enum"]


def test_torrents_enum_is_per_action_right():
    """Право на каждое действие своё: посмотреть список — не то же самое,
    что добавить раздачу или остановить чужую закачку."""
    assert _tor_enum(_sub("list@torrents")) == ["list"]
    assert _tor_enum(_sub("list@torrents", "pause@torrents")) == ["list", "pause"]
    assert _tor_enum(ADMIN) == ["list", "space", "search", "add", "pause", "resume"]


def test_torrents_hidden_without_any_torrent_right():
    assert "torrents" not in _names(_sub("status", "nodes"))
    assert "torrents" not in tools.tools_for(_sub("status")).handlers


def test_torrents_group_right_covers_all_actions():
    """«*@torrents» — новое умение службы доступно сразу, без правки конфига."""
    assert _tor_enum(_sub("*@torrents")) == ["list", "space", "search", "add", "pause", "resume"]


async def test_torrents_list_finds_hosting_node(store):
    """Ноду со службой ищем в состоянии роя, а не хардкодим: назначения
    меняются кнопкой в боте, без правки кода."""
    link = _swarm_link(command_result={"torrents": [{"name": "Foo"}], "count": 1})
    raw = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "list"}
    )
    payload = json.loads(raw)
    assert payload["node"] == "alfred"
    assert payload["count"] == 1
    action, dst = link.commands[0]
    assert (action, dst.node, dst.service) == ("list", "alfred", "torrents")


async def test_torrents_space_passes_through(store):
    link = _swarm_link(command_result={"dirs": [{"path": "/mnt/data/pr", "free_bytes": 1}]})
    raw = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "space"}
    )
    assert json.loads(raw)["dirs"][0]["free_bytes"] == 1
    assert link.commands[0][0] == "space"


async def test_torrents_add_sends_magnet_and_save_path(store):
    link = _swarm_link(command_result={"name": "Foo", "save_path": "/mnt/data/pr"})
    await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {
            "action": "add",
            "magnet": "magnet:?xt=urn:btih:abc",
            "save_path": "/mnt/data/pr",
            "name": "Foo",
        },
    )
    assert link.sent_args[0] == {
        "source": "magnet:?xt=urn:btih:abc",
        "save_path": "/mnt/data/pr",
        "name": "Foo",
    }


async def test_torrents_add_takes_a_search_result_url_too(store):
    """Ссылка из выдачи своего же поиска — это не magnet, а адрес метафайла
    на трекере; проверку «а можно ли с этого сайта» делает служба."""
    link = _swarm_link(command_result={"name": "Foo", "save_path": "/mnt/data/pr"})
    await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {
            "action": "add",
            "magnet": "https://rutracker.org/forum/dl.php?t=1",
            "save_path": "/mnt/data/pr",
        },
    )
    assert link.sent_args[0]["source"] == "https://rutracker.org/forum/dl.php?t=1"


async def test_torrents_add_refuses_a_source_that_is_not_a_link(store):
    """Base64-файл модели взяться неоткуда — его человек присылает сам."""
    link = _swarm_link()
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {"action": "add", "magnet": "ZDg6YW5ub3VuY2U="},
    )
    assert result.startswith("ошибка")
    assert link.commands == []


async def test_torrents_search_sends_query(store):
    link = _swarm_link(command_result={"results": [{"name": "Foo", "seeders": 10}], "count": 1})
    raw = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {"action": "search", "query": "задача трёх тел"},
    )
    assert json.loads(raw)["count"] == 1
    assert link.commands[0][0] == "search"
    assert link.sent_args[0] == {"query": "задача трёх тел"}


async def test_torrents_search_without_query_asks_for_it(store):
    link = _swarm_link()
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "search"}
    )
    assert result.startswith("ошибка")
    assert link.commands == []


async def test_torrents_add_without_save_path_points_at_space(store):
    """Сервер протокола ответил бы «нет обязательного параметра: save_path» —
    формально верно, но не говорит модели, где взять значение."""
    link = _swarm_link()
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {"action": "add", "magnet": "magnet:?xt=urn:btih:abc"},
    )
    assert result.startswith("ошибка")
    assert "space" in result
    assert link.commands == []


async def test_torrents_pause_sends_name(store):
    link = _swarm_link(command_result={"paused": ["Foo.S01"], "count": 1})
    raw = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "pause", "name": "Foo.S01"}
    )
    assert json.loads(raw)["paused"] == ["Foo.S01"]
    assert link.sent_args[0] == {"name": "Foo.S01"}


async def test_torrents_pause_without_name_asks_for_it(store):
    link = _swarm_link()
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "pause"}
    )
    assert result.startswith("ошибка")
    assert link.commands == []


async def test_torrents_service_refusal_reads_as_answer(store):
    """Отказ службы (мало места, неоднозначное имя) — готовый ответ модели с
    объяснением, что делать дальше, а не «внутренняя ошибка»."""
    link = _swarm_link(command_raises=tools.ProtoError("bad_request", "мало места в /mnt/data/pr"))
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {"action": "add", "magnet": "magnet:?xt=urn:btih:abc", "save_path": "/mnt/data/pr"},
    )
    assert result == "не вышло: мало места в /mnt/data/pr"


async def test_torrents_unreachable_service_degrades_to_text(store):
    """§7.3: спящая нода — обычный результат для модели, а не сбой цикла."""
    link = _swarm_link(command_raises=tools.ServiceUnavailableError("нода спит"))
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "list"}
    )
    assert result.startswith("недоступно")


async def test_torrents_service_absent_in_swarm(store):
    own = {**_OWN_STATE, "services": [{"name": "monitor", "status": "running"}]}
    link = _swarm_link(states={"own": own})
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "list"}
    )
    assert result.startswith("недоступно")


async def test_torrents_rejects_action_without_right(store):
    """Модель может назвать действие, которого не было в её enum — тул
    сверяется с подпиской повторно, а не доверяет декларации."""
    link = _swarm_link()
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=_sub("list@torrents")),
        {"action": "pause", "name": "все"},
    )
    assert result.startswith("не умею")
    assert link.commands == []


async def test_torrents_without_subscription_refuses(store):
    result = await tools.tool_torrents(
        _ctx(store, node_link=_swarm_link(), subscription=None), {"action": "list"}
    )
    assert result.startswith("не умею")


# --- dismiss: «ты свободен» — намерение, а не действие ---


def _dismiss_enum(subscription) -> list[str]:
    decl = next(
        (
            d
            for d in tools.tools_for(subscription).declarations
            if d["function"]["name"] == "dismiss"
        ),
        None,
    )
    if decl is None:
        return []
    return decl["function"]["parameters"]["properties"]["mode"]["enum"]


def test_dismiss_modes_gated_by_the_same_rights_as_buttons():
    assert _dismiss_enum(_sub("sleep@llm")) == ["model"]
    assert _dismiss_enum(_sub("suspend@node")) == ["sleep"]
    assert _dismiss_enum(ADMIN) == ["model", "sleep", "off"]


def test_dismiss_hidden_without_any_power_right():
    assert "dismiss" not in _names(_sub("chat@llm", "status"))


async def test_dismiss_only_records_intent(store):
    """Гасить модель прямо в туле нельзя: ответ — прощание, ради которого всё
    и затевалось, — в этот момент ещё не сгенерирован."""
    box = tools.DismissalBox()
    link = _swarm_link()
    result = await tools.tool_dismiss(
        _ctx(store, node_link=link, subscription=ADMIN, dismissal=box), {"mode": "off"}
    )
    assert box.mode == "off"
    assert link.commands == []  # ничего не выключено ЗДЕСЬ
    assert result.startswith("принято")


async def test_dismiss_without_box_is_honest(store):
    """У службы tasks исполнить намерение после ответа некому — обещать
    выключение, которого не будет, нельзя."""
    result = await tools.tool_dismiss(
        _ctx(store, subscription=ADMIN, dismissal=None), {"mode": "off"}
    )
    assert result.startswith("недоступно")


async def test_dismiss_rejects_mode_without_right(store):
    box = tools.DismissalBox()
    result = await tools.tool_dismiss(
        _ctx(store, subscription=_sub("sleep@llm"), dismissal=box), {"mode": "off"}
    )
    assert result.startswith("не умею")
    assert box.mode is None


async def test_dismiss_without_subscription_refuses(store):
    box = tools.DismissalBox()
    result = await tools.tool_dismiss(
        _ctx(store, subscription=None, dismissal=box), {"mode": "model"}
    )
    assert result.startswith("не умею")
    assert box.mode is None


# --- web_search: интернет через службу net ---


async def test_web_search_requires_search_right():
    assert "web_search" not in _names(_sub("status", "nodes"))
    assert "web_search" in _names(_sub("search@net"))


async def test_web_search_calls_net_service(store):
    link = _FakeSwarmLink(
        command_result={"query": "погода", "results": [{"title": "T", "url": "u"}], "count": 1}
    )
    raw = await tools.tool_web_search(
        _ctx(store, node_link=link, subscription=ADMIN), {"query": "погода"}
    )
    assert json.loads(raw)["count"] == 1
    action, dst = link.commands[0]
    assert (action, dst.node, dst.service) == ("search", "alfred", "net")


async def test_web_search_empty_results_reads_as_plain_text(store):
    """Пустая выдача — не ошибка и не JSON: модели проще не пересказывать
    пустой список, а прямо сказать, что ничего не нашлось."""
    link = _FakeSwarmLink(command_result={"query": "чепуха", "results": [], "count": 0})
    result = await tools.tool_web_search(
        _ctx(store, node_link=link, subscription=ADMIN), {"query": "чепуха"}
    )
    assert "ничего не нашлось" in result


async def test_web_search_unavailable_degrades_to_text(store):
    link = _FakeSwarmLink(command_raises=tools.ServiceUnavailableError("net спит"))
    result = await tools.tool_web_search(
        _ctx(store, node_link=link, subscription=ADMIN), {"query": "q"}
    )
    assert result.startswith("недоступно")


async def test_web_search_without_query(store):
    result = await tools.tool_web_search(
        _ctx(store, node_link=_FakeSwarmLink(), subscription=ADMIN), {}
    )
    assert result.startswith("ошибка")
