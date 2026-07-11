"""status_view.build_summary_text: рендер missing_requirements предупреждением."""

from sa_home_bot.bot.status_view import build_summary_text


class FakeMonitorLink:
    def __init__(self, missing: list[str]) -> None:
        self._missing = missing

    async def get_state(self, dst=None):
        return {
            "uptime_s": 60.0,
            "health": [],
            "disks": [],
            "last_outage": None,
            "thresholds": {},
            "missing_requirements": self._missing,
        }


async def test_summary_appends_missing_requirement_warning():
    text = await build_summary_text(FakeMonitorLink(["sudo apt install smartmontools (…)"]))
    assert "⚠️ sudo apt install smartmontools" in text


async def test_summary_quiet_without_missing_requirements():
    text = await build_summary_text(FakeMonitorLink([]))
    assert "⚠️" not in text
