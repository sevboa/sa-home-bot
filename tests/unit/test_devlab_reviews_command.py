from __future__ import annotations

from pathlib import Path

from sa_home_bot.config import Settings
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.snapshot import reviews_command


def test_reviews_command_empty(tmp_path: Path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("test-token\n")
    settings = Settings.model_validate({"swarm": {"token": "x"}})

    def call(url, token, name, arguments):
        assert name == protocol.TOOL_REVIEWS
        assert arguments == {}
        return {"reviews": []}

    code = reviews_command(
        settings,
        token_path=str(token_file),
        call=call,
    )
    assert code == 0


def test_reviews_command_with_model_and_since(tmp_path: Path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("test-token\n")
    settings = Settings.model_validate({"swarm": {"token": "x"}})

    def call(url, token, name, arguments):
        assert name == protocol.TOOL_REVIEWS
        assert arguments == {"model": "model-a", "since": "2026-01-01"}
        return {"reviews": []}

    code = reviews_command(
        settings,
        model="model-a",
        since="2026-01-01",
        token_path=str(token_file),
        call=call,
    )
    assert code == 0


def test_reviews_command_with_data(tmp_path: Path, capsys, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("test-token\n")
    settings = Settings.model_validate({"swarm": {"token": "x"}})

    def call(url, token, name, arguments):
        assert name == protocol.TOOL_REVIEWS
        return {
            "reviews": [
                {
                    protocol.FIELD_MODEL: "model-a",
                    protocol.FIELD_VERDICT: protocol.REVIEW_ACCEPTED,
                    protocol.FIELD_ISSUES: [],
                    protocol.PAYLOAD_TOKENS: 100,
                },
                {
                    protocol.FIELD_MODEL: "model-a",
                    protocol.FIELD_VERDICT: protocol.REVIEW_ACCEPTED_WITH_FIXES,
                    protocol.FIELD_ISSUES: [
                        {protocol.FIELD_TAG: protocol.ISSUE_LOOP, protocol.FIELD_NOTE: "x"}
                    ],
                    protocol.PAYLOAD_TOKENS: 200,
                },
                {
                    protocol.FIELD_MODEL: "model-b",
                    protocol.FIELD_VERDICT: protocol.REVIEW_ACCEPTED,
                    protocol.FIELD_ISSUES: [
                        {protocol.FIELD_TAG: protocol.ISSUE_MISSED_CASE, protocol.FIELD_NOTE: "y"}
                    ],
                    protocol.PAYLOAD_TOKENS: 300,
                },
            ]
        }

    code = reviews_command(
        settings,
        token_path=str(token_file),
        call=call,
    )
    assert code == 0
    out = capsys.readouterr().out.strip()
    lines = out.splitlines()
    assert lines[0] == "модель, заданий, без правок %, частые метки, токенов/задание"
    assert lines[1] == "model-a, 2, 50%, loop (1), 150"
    assert lines[2] == "model-b, 1, 100%, missed_case (1), 300"


def test_reviews_command_token_error(tmp_path: Path, capsys):
    token_file = tmp_path / "nonexistent"
    settings = Settings.model_validate({"swarm": {"token": "x"}})

    def call(url, token, name, arguments):
        raise AssertionError("call should not be invoked")

    code = reviews_command(
        settings,
        token_path=str(token_file),
        call=call,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "Ошибка: токен" in err


def test_reviews_command_hub_unavailable(tmp_path: Path, capsys):
    token_file = tmp_path / "token"
    token_file.write_text("test-token\n")
    settings = Settings.model_validate({"swarm": {"token": "x"}})

    from sa_home_bot.devlab.snapshot import HubUnavailable

    def call(url, token, name, arguments):
        raise HubUnavailable("Connection refused")

    code = reviews_command(
        settings,
        token_path=str(token_file),
        call=call,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "Ошибка: хаб недоступен" in err


def test_reviews_command_snapshot_error(tmp_path: Path, capsys):
    token_file = tmp_path / "token"
    token_file.write_text("test-token\n")
    settings = Settings.model_validate({"swarm": {"token": "x"}})

    from sa_home_bot.devlab.snapshot import SnapshotError

    def call(url, token, name, arguments):
        raise SnapshotError("hub error")

    code = reviews_command(
        settings,
        token_path=str(token_file),
        call=call,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "Ошибка: hub error" in err
    assert "test-token" not in err
