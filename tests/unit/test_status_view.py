import pytest

from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.bot.status_view import (
    DOWNTIME_UNSUPPORTED_TEXT,
    MONITOR_DOWN_TEXT,
    build_downtime_page,
    build_summary_text,
)
from sa_home_bot.proto.messages import ERR_INTERNAL, ERR_UNKNOWN_ACTION, ProtoError
from tests.unit.support.fakes import FakeNodeLink
from tests.unit.swarm_support import callback_codes

OUTAGE = {
    "kind": "unexpected",
    "boot_at": "2026-07-05T00:23:52+00:00",
    "down_at": "2026-07-04T15:12:00+00:00",
    "up_at": None,
    "down_approx": True,
    "downtime_s": None,
}


def _monitor_link(**state) -> FakeNodeLink:
    return FakeNodeLink(
        {
            "uptime_s": 60.0,
            "health": [],
            "disks": [],
            "last_outage": None,
            "thresholds": {},
            "requirements": [],
            **state,
        }
    )


def _host_metric(metric: str, label: str, value: float) -> dict:
    return {
        "component_id": f"host:{metric}",
        "metric": metric,
        "label": label,
        "unit": "%",
        "status": "ok",
        "value": value,
        "alerting_since": None,
    }


async def test_downtime_calls_monitor_of_target_node():
    link = FakeNodeLink(result={"events": [OUTAGE], "offset": 0, "has_next": True})
    text, keyboard = await build_downtime_page(link, node_id="arch-t480")

    action, args, dst = link.calls[0]
    assert action == "downtime"
    assert args == {"offset": 0, "limit": 10}
    assert dst.node == "arch-t480" and dst.service == "monitor"
    assert "⚡" in text or "отключ" in text.lower()
    assert callback_codes(keyboard) == ["st:downtime_page:10:arch-t480"]


async def test_downtime_own_node_without_id_segment():
    link = FakeNodeLink(result={"events": [OUTAGE], "offset": 10, "has_next": False})
    _, keyboard = await build_downtime_page(link, offset=10)
    _, _, dst = link.calls[0]
    assert dst.node is None and dst.service == "monitor"
    assert callback_codes(keyboard) == ["st:downtime_page:0"]


@pytest.mark.parametrize(
    ("error", "expected_text"),
    [
        (ProtoError(ERR_UNKNOWN_ACTION, "нет такого действия"), DOWNTIME_UNSUPPORTED_TEXT),
        (ServiceUnavailableError("нет связи"), MONITOR_DOWN_TEXT),
        (ProtoError(ERR_INTERNAL, "внутренняя ошибка"), MONITOR_DOWN_TEXT),
    ],
)
async def test_downtime_errors_give_honest_text(error, expected_text):
    text, keyboard = await build_downtime_page(FakeNodeLink(error=error), node_id="arch-t480")
    assert text == expected_text
    assert keyboard is None


@pytest.mark.parametrize(
    "requirement",
    [
        {"id": "smartctl", "status": "missing_program", "hint": "sudo apt install smartmontools"},
        {"id": "smartctl", "status": "needs_privilege", "hint": "не хватает прав — nodectl fix"},
    ],
)
async def test_summary_appends_requirement_warning(requirement):
    text = await build_summary_text(_monitor_link(requirements=[requirement]))
    assert f"⚠️ {requirement['hint']}" in text


async def test_summary_quiet_without_requirement_problems():
    assert "⚠️" not in await build_summary_text(_monitor_link())


async def test_summary_renders_host_block_for_vps_node():
    link = _monitor_link(
        host_health=[
            _host_metric("steal_pct", "CPU steal", 0.0),
            _host_metric("disk_used_pct", "диск /", 8.0),
        ]
    )
    text = await build_summary_text(link)
    assert "📊 <b>Хост VPS</b>" in text
    assert " • CPU steal: <b>0%</b>" in text
    assert " • диск / заполнен: <b>8%</b>" in text


async def test_summary_of_unavailable_monitor_is_down_text():
    link = FakeNodeLink()
    link.get_state = _raise_unavailable
    assert await build_summary_text(link) == MONITOR_DOWN_TEXT


async def _raise_unavailable(dst=None):
    raise ServiceUnavailableError("нет связи")
