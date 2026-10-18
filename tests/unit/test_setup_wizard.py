from __future__ import annotations

import tomllib
from pathlib import Path

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


def test_service_sockets_are_absolute_not_relative_to_config_dir(tmp_path):
    config_path = tmp_path / "unrelated-dir" / "config.toml"
    data_dir = tmp_path / "data-dir"
    args = _parser_args([
        "init", "--non-interactive",
        "--config", str(config_path),
        "--data-dir", str(data_dir),
        "--token", "t",
        "--no-systemd-unit",
    ])

    assert setup_wizard.run_init(args) == 0
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))

    assert parsed["node"]["socket"] == str(data_dir / "data" / "node.sock")
    assert parsed["monitor"]["socket"] == str(data_dir / "data" / "monitor.sock")
    assert parsed["monitor"]["db_path"] == str(data_dir / "data" / "monitor.sqlite")
    assert parsed["apps"]["socket"] == str(data_dir / "data" / "apps.sock")
    assert parsed["torrents"]["socket"] == str(data_dir / "data" / "torrents.sock")
    assert parsed["memory"]["socket"] == str(data_dir / "data" / "memory.sock")
    assert parsed["tasks"]["socket"] == str(data_dir / "data" / "tasks.sock")
    assert parsed["net"]["socket"] == str(data_dir / "data" / "net.sock")
    assert parsed["llm"]["socket"] == str(data_dir / "data" / "llm.sock")
    assert parsed["database"]["path"] == str(data_dir / "data" / "sentinel.sqlite")
    for section, fields in parsed.items():
        if section in ("node", "swarm"):
            continue
        for field, value in fields.items():
            if field in ("socket", "db_path", "path"):
                assert value.startswith("/"), f"{section}.{field} должен быть абсолютным"


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


def test_non_interactive_defaults_listen_to_all_interfaces(tmp_path):
    config_path = tmp_path / "config.toml"
    args = _parser_args([
        "init", "--non-interactive",
        "--config", str(config_path),
        "--data-dir", str(tmp_path / "data-dir"),
        "--token", "t",
        "--kind", "workstation",
        "--no-systemd-unit",
    ])

    assert setup_wizard.run_init(args) == 0
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["node"]["listen"] == ["tcp://0.0.0.0:8710"]


def test_non_interactive_vps_listen_stays_empty_but_warns(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    args = _parser_args([
        "init", "--non-interactive",
        "--config", str(config_path),
        "--data-dir", str(tmp_path / "data-dir"),
        "--token", "t",
        "--kind", "vps",
        "--no-systemd-unit",
    ])

    assert setup_wizard.run_init(args) == 0
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["node"]["listen"] == []
    assert "не сможет вступить в рой" in capsys.readouterr().err


def test_interactive_refuses_empty_listen_unless_confirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard.sys.stdin, "isatty", lambda: True)
    answers = iter([
        "",
        "n",
        "tcp://100.64.0.5:8710",
        "n",
        "y",
    ])
    monkeypatch.setattr("builtins.input", lambda *_a: next(answers))

    config_path = tmp_path / "config.toml"
    args = _parser_args([
        "init",
        "--config", str(config_path),
        "--data-dir", str(tmp_path / "data-dir"),
        "--node-id", "jeeves2",
        "--kind", "vps",
        "--assignments", "",
        "--no-systemd-unit",
    ])

    assert setup_wizard.run_init(args) == 0
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["node"]["listen"] == ["tcp://100.64.0.5:8710"]


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
        "mycraft",
        "workstation",
        "monitor",
        "n",
        "n",
        "pasted-token",
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
    assert parsed["node"]["listen"] == ["tcp://0.0.0.0:8710"]
    assert parsed["swarm"]["join"] == ""
    assert parsed["swarm"]["token"] == "pasted-token"


def test_interactive_existing_swarm_without_known_address_requires_token(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard.sys.stdin, "isatty", lambda: True)
    answers = iter([
        "mycraft",
        "server",
        "",
        "y",
        "",
        "existing-real-token",
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
    assert parsed["node"]["listen"] == ["tcp://0.0.0.0:8710"]
    assert parsed["swarm"]["join"] == ""
    assert parsed["swarm"]["token"] == "existing-real-token"


def test_nodectl_resolves_same_socket_as_the_running_node(tmp_path):
    config_path = tmp_path / "config-dir" / "config.toml"
    data_dir = tmp_path / "data-dir"
    args = _parser_args([
        "init", "--non-interactive",
        "--config", str(config_path),
        "--data-dir", str(data_dir),
        "--token", "t",
        "--no-systemd-unit",
    ])
    assert setup_wizard.run_init(args) == 0

    from sa_home_bot.nodectl import _resolve_endpoint

    class _Args:
        config = str(config_path)
        socket = None

    endpoint, _token = _resolve_endpoint(_Args())
    assert str(endpoint.path) == str(data_dir / "data" / "node.sock")


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


def test_render_systemd_unit_uses_absolute_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "SMARTCTL_WRAPPER_PATH", Path("/home/x/.local/bin/smartctl"))
    config_path = tmp_path / "config.toml"
    data_dir = tmp_path / "data-dir"

    content = setup_wizard._render_systemd_unit(
        exec_path="/home/x/.local/share/pipx/venvs/sa-home-bot/bin/sa-home-bot",
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
        "init", "--non-interactive", "--no-start",
        "--config", str(tmp_path / "config.toml"),
        "--data-dir", str(tmp_path / "data-dir"),
        "--node-id", "mycraft",
        "--token", "t",
    ])

    assert setup_wizard.run_init(args) == 0
    unit_path = tmp_path / ".config" / "systemd" / "user" / "sa-home-node.service"
    assert unit_path.exists()
    assert str(tmp_path / "data-dir") in unit_path.read_text(encoding="utf-8")


@pytest.mark.skipif(
    not setup_wizard.sys.platform.startswith("linux"), reason="юнит пишется только на Linux"
)
def test_no_start_never_touches_subprocess(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    def _boom(*_a, **_kw):
        raise AssertionError("subprocess.run не должен звонить с --no-start")

    monkeypatch.setattr(setup_wizard.subprocess, "run", _boom)
    args = _parser_args([
        "init", "--non-interactive", "--no-start",
        "--config", str(tmp_path / "config.toml"),
        "--data-dir", str(tmp_path / "data-dir"),
        "--token", "t",
    ])

    assert setup_wizard.run_init(args) == 0


@pytest.mark.skipif(
    not setup_wizard.sys.platform.startswith("linux"), reason="юнит пишется только на Linux"
)
def test_start_runs_systemctl_but_skips_sudo_when_non_interactive(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        setup_wizard.subprocess, "run",
        lambda cmd, **_kw: calls.append(cmd) or setup_wizard.subprocess.CompletedProcess(cmd, 0),
    )
    args = _parser_args([
        "init", "--non-interactive",
        "--config", str(tmp_path / "config.toml"),
        "--data-dir", str(tmp_path / "data-dir"),
        "--token", "t",
    ])

    assert setup_wizard.run_init(args) == 0
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", "sa-home-node"] in calls
    assert not any("sudo" in cmd for cmd in calls)


@pytest.mark.skipif(
    not setup_wizard.sys.platform.startswith("linux"), reason="юнит пишется только на Linux"
)
def test_start_asks_sudo_for_linger_when_interactive(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(setup_wizard.sys.stdin, "isatty", lambda: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        setup_wizard.subprocess, "run",
        lambda cmd, **_kw: calls.append(cmd) or setup_wizard.subprocess.CompletedProcess(cmd, 0),
    )
    args = _parser_args([
        "init",
        "--config", str(tmp_path / "config.toml"),
        "--data-dir", str(tmp_path / "data-dir"),
        "--node-id", "mycraft",
        "--kind", "server",
        "--assignments", "",
        "--listen", "",
        "--join", "",
        "--token", "t",
    ])

    assert setup_wizard.run_init(args) == 0
    assert any(cmd[:2] == ["sudo", "loginctl"] for cmd in calls)
