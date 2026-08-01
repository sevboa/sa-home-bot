"""`sa-home-bot init` — генерация config.toml/юнита без git-checkout.

Фиксирует контракт: неинтерактивный режим либо использует дефолты/генерирует
токен, либо падает с понятным сообщением (а не молча создаёт нерабочий рой),
интерактивный — реально спрашивает через input(), существующий файл не летит
в мусорку без --force.
"""

from __future__ import annotations

import tomllib

import pytest

from sa_home_bot import setup_wizard
from sa_home_bot.cli import main


def _parser_args(argv: list[str]):
    parser = setup_wizard.argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    setup_wizard.add_init_subparser(subparsers)
    return parser.parse_args(argv)


def test_non_interactive_without_join_generates_token(tmp_path):
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "data-dir"
    args = _parser_args([
        "init", "--non-interactive",
        "--config", str(config_path),
        "--data-dir", str(data_dir),
        "--node-id", "mycraft",
        "--no-systemd-unit",
    ])

    assert setup_wizard.run_init(args) == 0

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["node"]["id"] == "mycraft"
    assert parsed["node"]["kind"] == "workstation"
    assert parsed["swarm"]["join"] == ""
    assert len(parsed["swarm"]["token"]) > 20
    assert (data_dir / "data").is_dir()


def test_non_interactive_with_join_requires_token(tmp_path, capsys):
    args = _parser_args([
        "init", "--non-interactive",
        "--config", str(tmp_path / "config.toml"),
        "--data-dir", str(tmp_path / "data-dir"),
        "--join", "tcp://192.168.0.101:8710",
        "--no-systemd-unit",
    ])

    assert setup_wizard.run_init(args) == 2
    assert not (tmp_path / "config.toml").exists()
    assert "--token" in capsys.readouterr().err


def test_non_interactive_with_join_and_token_writes_join(tmp_path):
    config_path = tmp_path / "config.toml"
    args = _parser_args([
        "init", "--non-interactive",
        "--config", str(config_path),
        "--data-dir", str(tmp_path / "data-dir"),
        "--join", "tcp://192.168.0.101:8710",
        "--token", "sharedsecret",
        "--assignments", "monitor, apps",
        "--listen", "tcp://192.168.0.103:8710",
        "--kind", "server",
        "--no-systemd-unit",
    ])

    assert setup_wizard.run_init(args) == 0
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["swarm"] == {"token": "sharedsecret", "join": "tcp://192.168.0.101:8710"}
    assert parsed["node"]["assignments"] == ["monitor", "apps"]
    assert parsed["node"]["listen"] == ["tcp://192.168.0.103:8710"]
    assert parsed["node"]["kind"] == "server"


def test_refuses_to_overwrite_without_force(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("# уже стоит своя нода\n", encoding="utf-8")
    args = _parser_args([
        "init", "--non-interactive",
        "--config", str(config_path),
        "--data-dir", str(tmp_path / "data-dir"),
        "--no-systemd-unit",
    ])

    assert setup_wizard.run_init(args) == 1
    assert config_path.read_text(encoding="utf-8") == "# уже стоит своя нода\n"


def test_force_overwrites_existing_config(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("# старьё\n", encoding="utf-8")
    args = _parser_args([
        "init", "--non-interactive", "--force",
        "--config", str(config_path),
        "--data-dir", str(tmp_path / "data-dir"),
        "--node-id", "mycraft",
        "--no-systemd-unit",
    ])

    assert setup_wizard.run_init(args) == 0
    assert "mycraft" in config_path.read_text(encoding="utf-8")


def test_interactive_prompts_fill_in_missing_values(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard.sys.stdin, "isatty", lambda: True)
    answers = iter([
        "mycraft",       # node id
        "workstation",   # kind
        "monitor",       # assignments
        "",              # listen
        "",              # join (первая нода)
        "n",             # не генерировать токен...
        "pasted-token",  # ...а вставить свой
    ])
    monkeypatch.setattr("builtins.input", lambda *_a: next(answers))

    config_path = tmp_path / "config.toml"
    args = _parser_args([
        "init",
        "--config", str(config_path),
        "--data-dir", str(tmp_path / "data-dir"),
        "--no-systemd-unit",
    ])

    assert setup_wizard.run_init(args) == 0
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["node"]["id"] == "mycraft"
    assert parsed["node"]["assignments"] == ["monitor"]
    assert parsed["swarm"]["token"] == "pasted-token"


def test_cli_init_writes_config_loadable_by_settings(tmp_path):
    config_path = tmp_path / "config.toml"
    exit_code = main([
        "init", "--non-interactive",
        "--config", str(config_path),
        "--data-dir", str(tmp_path / "data-dir"),
        "--node-id", "mycraft",
        "--token", "t",
        "--no-systemd-unit",
    ])
    assert exit_code == 0

    from sa_home_bot.config import Settings

    settings = Settings.load(str(config_path))
    assert settings.node.id == "mycraft"
    assert settings.swarm.token == "t"


def test_render_systemd_unit_uses_absolute_paths(tmp_path):
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "data-dir"

    content = setup_wizard._render_systemd_unit(
        exec_path="/home/x/.local/bin/sa-home-bot",
        config_path=config_path,
        data_dir=data_dir,
    )

    assert f"WorkingDirectory={data_dir}" in content
    assert f"--config {config_path}" in content
    assert "Environment=PATH=/home/x/.local/bin:" in content


@pytest.mark.skipif(
    not setup_wizard.sys.platform.startswith("linux"), reason="юнит пишется только на Linux"
)
def test_init_writes_systemd_unit_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    args = _parser_args([
        "init", "--non-interactive",
        "--config", str(tmp_path / "config.toml"),
        "--data-dir", str(tmp_path / "data-dir"),
        "--node-id", "mycraft",
        "--token", "t",
    ])

    assert setup_wizard.run_init(args) == 0
    unit_path = tmp_path / ".config" / "systemd" / "user" / "sa-home-node.service"
    assert unit_path.exists()
    assert str(tmp_path / "data-dir") in unit_path.read_text(encoding="utf-8")
