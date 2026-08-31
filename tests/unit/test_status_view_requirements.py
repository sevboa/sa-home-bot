"""status_view.build_summary_text: рендер requirements-проблем предупреждением."""

from sa_home_bot.bot.status_view import build_summary_text


class FakeMonitorLink:
    def __init__(self, requirements: list[dict]) -> None:
        self._requirements = requirements

    async def get_state(self, dst=None):
        return {
            "uptime_s": 60.0,
            "health": [],
            "disks": [],
            "last_outage": None,
            "thresholds": {},
            "requirements": self._requirements,
        }


async def test_summary_appends_requirement_warning():
    hint = "sudo apt install smartmontools (…)"
    text = await build_summary_text(
        FakeMonitorLink([{"id": "smartctl", "status": "missing_program", "hint": hint}])
    )
    assert "⚠️ sudo apt install smartmontools" in text


async def test_summary_appends_privilege_warning():
    hint = "не хватает прав — nodectl fix"
    text = await build_summary_text(
        FakeMonitorLink([{"id": "smartctl", "status": "needs_privilege", "hint": hint}])
    )
    assert "⚠️ не хватает прав — nodectl fix" in text


async def test_summary_quiet_without_requirement_problems():
    text = await build_summary_text(FakeMonitorLink([]))
    assert "⚠️" not in text


async def test_summary_renders_host_block_for_vps_node():
    link = FakeMonitorLink([])

    async def get_state(dst=None):
        return {
            "uptime_s": 60.0,
            "health": [],
            "disks": [],
            "last_outage": None,
            "thresholds": {},
            "requirements": [],
            "host_health": [
                {"component_id": "host:steal_pct", "metric": "steal_pct", "label": "CPU steal",
                 "unit": "%", "status": "ok", "value": 0.0, "alerting_since": None},
                {"component_id": "host:disk_used_pct", "metric": "disk_used_pct",
                 "label": "диск /", "unit": "%", "status": "ok", "value": 8.0,
                 "alerting_since": None},
            ],
        }

    link.get_state = get_state
    text = await build_summary_text(link)
    assert "📊 <b>Хост VPS</b>" in text
    assert " • CPU steal: <b>0%</b>" in text
    assert " • диск / заполнен: <b>8%</b>" in text
