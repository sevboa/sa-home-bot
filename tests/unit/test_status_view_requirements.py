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
