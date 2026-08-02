"""utils/ssh_sessions.py: разбор loginctl, фильтр Remote=yes, terminate-session."""

from __future__ import annotations

from sa_home_bot.utils import ssh_sessions

LIST_OUTPUT = (
    "      1 1000 sevboa -    1078 manager -   no   -\n"
    "      5 1000 sevboa -    1852 user    -   no   -\n"
)

SESSION_1_SHOW = "Name=sevboa\nTTY=\nRemote=no\nType=unspecified\n"
SESSION_5_SHOW = (
    "Name=sevboa\nTTY=pts/0\nRemote=yes\nType=tty\nTimestamp=Mon 2026-08-03 01:08:10 +05\n"
)


def _fake_run(outputs: dict[tuple[str, ...], str]):
    calls: list[tuple[str, ...]] = []

    async def fake(*argv: str) -> str:
        calls.append(argv)
        for key, value in outputs.items():
            if argv[: len(key)] == key:
                return value
        raise AssertionError(f"неожиданный вызов: {argv}")

    return fake, calls


async def test_list_ssh_sessions_keeps_only_remote_yes(monkeypatch):
    fake, calls = _fake_run(
        {
            ("loginctl", "list-sessions"): LIST_OUTPUT,
            ("loginctl", "show-session", "1"): SESSION_1_SHOW,
            ("loginctl", "show-session", "5"): SESSION_5_SHOW,
        }
    )
    monkeypatch.setattr(ssh_sessions, "_run", fake)

    sessions = await ssh_sessions.list_ssh_sessions()

    assert [s.id for s in sessions] == ["5"]
    assert sessions[0].user == "sevboa"
    assert sessions[0].tty == "pts/0"
    assert sessions[0].since == "Mon 2026-08-03 01:08:10 +05"
    assert ("loginctl", "list-sessions", "--no-legend") in calls


async def test_list_ssh_sessions_empty_when_none_remote(monkeypatch):
    fake, _ = _fake_run(
        {
            ("loginctl", "list-sessions"): "      1 1000 sevboa -    1078 manager -   no   -\n",
            ("loginctl", "show-session", "1"): SESSION_1_SHOW,
        }
    )
    monkeypatch.setattr(ssh_sessions, "_run", fake)

    assert await ssh_sessions.list_ssh_sessions() == []


def test_session_describe_format():
    session = ssh_sessions.SshSession(id="5", user="sevboa", tty="pts/0", since="01:08")
    assert session.describe() == "sevboa, pts/0, с 01:08"


async def test_terminate_sessions_calls_loginctl_per_id(monkeypatch):
    fake, calls = _fake_run({("loginctl", "terminate-session"): ""})
    monkeypatch.setattr(ssh_sessions, "_run", fake)

    await ssh_sessions.terminate_sessions(["5", "6"])

    assert calls == [
        ("loginctl", "terminate-session", "5"),
        ("loginctl", "terminate-session", "6"),
    ]
