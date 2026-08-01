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


def test_service_sockets_are_absolute_not_relative_to_config_dir(tmp_path):
    """nodectl резолвит `[node].socket` от каталога config.toml, а сам сервис —
    от своего WorkingDirectory (data_dir); если это разные каталоги (обычная
    установка: config в ~/.config, данные в ~/.local/share), относительный
    путь ломает `nodectl status` («No such file or directory») — живой баг
    2026-08-01. Значит все сокеты/БД обязаны быть абсолютными путями внутри
    data_dir, а не значением по умолчанию из config.py."""
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
    """Живой баг 2026-08-01 на mycraft: пустой listen проходит валидацию
    config.toml, но node/service.py::_swarm_join требует наш endpoint —
    с пустым listen у ноды его нет, и join падает всегда с bad_request.
    Дефолт больше не пустой (кроме vps, см. следующий тест)."""
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
    """vps — публичный IP (см. node-jeeves): 0.0.0.0 туда светить нельзя,
    нужен явный приватный/tailscale-адрес. Раз его не дали — предупреждаем,
    а не молча ломаем join, как это было на mycraft."""
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
    """kind=vps — единственный случай, где дефолт listen сам по себе пуст,
    так что ответ "" на первую попытку реально даёт пустой список (для
    server/workstation дефолт непустой, и "" вернул бы его же)."""
    monkeypatch.setattr(setup_wizard.sys.stdin, "isatty", lambda: True)
    answers = iter([
        "",                      # listen попытка 1: пусто (дефолт для vps тоже пуст)
        "n",                     # "точно оставить пустым?" — нет
        "tcp://100.64.0.5:8710", # listen попытка 2: реальный адрес
        "n",                     # рой ещё не существует
        "y",                     # сгенерировать токен
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
        "mycraft",       # node id
        "workstation",   # kind
        "monitor",       # assignments (listen для non-vps больше не спрашивается)
        "n",             # рой в этой LAN ещё не существует — эта нода первая
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
    assert parsed["node"]["listen"] == ["tcp://0.0.0.0:8710"]
    assert parsed["swarm"]["join"] == ""
    assert parsed["swarm"]["token"] == "pasted-token"


def test_interactive_existing_swarm_without_known_address_requires_token(tmp_path, monkeypatch):
    """Идея пользователя 2026-08-01: не заставлять вводить IP — LAN-маячок сам
    найдёт соседа по общему токену (node/discovery.py уже это умеет). Значит
    «есть рой, но адрес не знаю» обязана требовать вставить токен, а не
    предлагать сгенерировать новый (это сломало бы автообнаружение — токены
    не совпали бы)."""
    monkeypatch.setattr(setup_wizard.sys.stdin, "isatty", lambda: True)
    answers = iter([
        "mycraft",              # node id
        "server",               # kind
        "",                     # assignments (listen для non-vps больше не спрашивается)
        "y",                    # да, рой в этой LAN уже есть
        "",                     # IP соседа не знаю — понадеемся на маячок
        "existing-real-token",  # токен обязателен, не предлагали генерировать
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
    """Воспроизводит живой баг 2026-08-01: конфиг лежит не там же, где данные
    (обычная установка — ~/.config vs ~/.local/share), поэтому `nodectl`
    (резолвит от каталога конфига) обязан прийти к тому же сокету, что и сам
    процесс ноды (резолвит от WorkingDirectory=data_dir)."""
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
