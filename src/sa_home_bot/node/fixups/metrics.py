from __future__ import annotations

import subprocess
from collections.abc import Collection, Sequence
from pathlib import Path

from sa_home_bot.bot.vpn_nodes import known_server_nodes
from sa_home_bot.config import Settings, SwarmNodeConfig
from sa_home_bot.devlab.connect import local_tailscale_address
from sa_home_bot.metrics import protocol
from sa_home_bot.metrics.dashboard import CATALOG, render_dashboard
from sa_home_bot.metrics.scrape import (
    ScrapeJob,
    scrape_config_text,
    scrape_targets,
    vpn_scrape_targets,
)
from sa_home_bot.node.fixups import needs
from sa_home_bot.node.fixups.core import Fixup, FixupError
from sa_home_bot.node.fixups.privileged import (
    daemon_reload,
    download,
    enable_now,
    file_matches,
    find_executable,
    install_from,
    install_package,
    install_root_file,
    path_exists,
    read_text_if_exists,
    run_sudo,
    temp_file,
    unit_is_active,
)
from sa_home_bot.node.state import NodeState

VM_PACKAGE = "victoria-metrics"
VM_UNIT = "victoria-metrics.service"
VM_USER = "_victoria-metrics"
VM_DEFAULTS_FILE = Path("/etc/default/victoria-metrics")
VM_SCRAPE_FILE = Path("/etc/victoria-metrics/scrape.yml")
GRAFANA_PACKAGE = "grafana"
GRAFANA_UNIT = "grafana-server.service"
GRAFANA_GROUP = "grafana"
GRAFANA_KEY_URL = "https://apt.grafana.com/gpg.key"
GRAFANA_KEY_FILE = Path("/etc/apt/keyrings/grafana.asc")
GRAFANA_SOURCE_FILE = Path("/etc/apt/sources.list.d/grafana.list")
GRAFANA_SOURCE_LINE = f"deb [signed-by={GRAFANA_KEY_FILE}] https://apt.grafana.com stable main\n"
GRAFANA_INI = Path("/etc/grafana/grafana.ini")
GRAFANA_DATASOURCE_FILE = Path("/etc/grafana/provisioning/datasources/sa-home.yaml")
GRAFANA_DASHBOARD_PROVIDER_FILE = Path("/etc/grafana/provisioning/dashboards/sa-home.yaml")
GRAFANA_DASHBOARD_DIR = Path("/etc/grafana/sa-home-dashboards")
SYSTEMD_DIR = Path("/etc/systemd/system")
DROPIN_NAME = "50-sa-home-tailscale.conf"
KEY_FETCH_TIMEOUT_S = 60


def vm_args(settings: Settings) -> str:
    cfg = settings.metrics
    return " ".join(
        [
            f"-storageDataPath={cfg.data_dir}",
            f"-retentionPeriod={cfg.retention}",
            f"-httpListenAddr=127.0.0.1:{cfg.vm_port}",
            f"-promscrape.config={VM_SCRAPE_FILE}",
            "-promscrape.configCheckInterval=1m",
        ]
    )


def vm_defaults_content(settings: Settings) -> str:
    return f'ARGS="{vm_args(settings)}"\n'


def vm_scrape_content(
    settings: Settings,
    own_host: str | None,
    known_vpn_servers: Collection[str] = (),
    peers: Sequence[SwarmNodeConfig] = (),
) -> str:
    cfg = settings.metrics
    targets = scrape_targets(settings, own_host, peers)
    jobs = [
        ScrapeJob(protocol.JOB_NAME, cfg.exporter_port, targets),
        ScrapeJob(protocol.JOB_VPN, cfg.vpn_port, vpn_scrape_targets(targets, known_vpn_servers)),
    ]
    return scrape_config_text(jobs, cfg.scrape_interval)


def _own_host() -> str | None:
    return local_tailscale_address()


def _swarm_peers(settings: Settings) -> list[SwarmNodeConfig]:
    return NodeState.load(settings.node.state_path).peers


def tailscale_dropin_content() -> str:
    return (
        "[Unit]\n"
        "After=tailscaled.service\n"
        "Wants=tailscaled.service\n"
        "StartLimitIntervalSec=0\n"
        "\n"
        "[Service]\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
    )


def set_ini_values(text: str, values: dict[str, dict[str, str]]) -> str:
    lines = text.splitlines()
    for section, pairs in values.items():
        for key, value in pairs.items():
            lines = _set_ini_key(lines, section, key, value)
    return "\n".join(lines) + "\n"


def _section_name(line: str) -> str | None:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        return stripped[1:-1]
    return None


def _is_key_line(line: str, key: str) -> bool:
    stripped = line.strip().lstrip(";#").strip()
    name, sep, _rest = stripped.partition("=")
    return bool(sep) and name.strip() == key


def _set_ini_key(lines: list[str], section: str, key: str, value: str) -> list[str]:
    new_line = f"{key} = {value}"
    start = next((i for i, line in enumerate(lines) if _section_name(line) == section), None)
    if start is None:
        return [*lines, "", f"[{section}]", new_line]
    end = next(
        (i for i in range(start + 1, len(lines)) if _section_name(lines[i]) is not None),
        len(lines),
    )
    active = [i for i in range(start + 1, end) if lines[i].strip() == new_line]
    if active:
        return lines
    live = [
        i
        for i in range(start + 1, end)
        if _is_key_line(lines[i], key) and not lines[i].strip().startswith((";", "#"))
    ]
    commented = [i for i in range(start + 1, end) if _is_key_line(lines[i], key)]
    target = (live or commented or [None])[0]
    if target is None:
        return [*lines[: start + 1], new_line, *lines[start + 1 :]]
    return [*lines[:target], new_line, *lines[target + 1 :]]


def grafana_ini_values(settings: Settings, host: str) -> dict[str, dict[str, str]]:
    return {
        "server": {"http_addr": host, "http_port": str(settings.metrics.grafana_port)},
        "auth.anonymous": {"enabled": "false"},
        "analytics": {
            "reporting_enabled": "false",
            "check_for_updates": "false",
            "check_for_plugin_updates": "false",
        },
        "plugins": {"preinstall_disabled": "true", "preinstall_auto_update": "false"},
    }


def grafana_datasource_content(settings: Settings) -> str:
    return (
        "apiVersion: 1\n"
        "datasources:\n"
        "  - name: VictoriaMetrics\n"
        f"    uid: {protocol.DATASOURCE_UID}\n"
        f"    type: {protocol.DATASOURCE_TYPE}\n"
        "    access: proxy\n"
        f"    url: http://127.0.0.1:{settings.metrics.vm_port}\n"
        "    isDefault: true\n"
    )


def grafana_provider_content() -> str:
    return (
        "apiVersion: 1\n"
        "providers:\n"
        "  - name: sa-home\n"
        "    type: file\n"
        "    allowUiUpdates: false\n"
        "    updateIntervalSeconds: 30\n"
        "    options:\n"
        f"      path: {GRAFANA_DASHBOARD_DIR}\n"
        "      foldersFromFilesStructure: true\n"
    )


def grafana_provisioning_files(settings: Settings) -> dict[Path, str]:
    files = {
        GRAFANA_DATASOURCE_FILE: grafana_datasource_content(settings),
        GRAFANA_DASHBOARD_PROVIDER_FILE: grafana_provider_content(),
    }
    for name, build in CATALOG.items():
        files[GRAFANA_DASHBOARD_DIR / name] = render_dashboard(build())
    return files


def _service_user(unit: str, default: str) -> str:
    result = subprocess.run(
        ["systemctl", "show", "-p", "User", "--value", unit], capture_output=True, text=True
    )
    name = result.stdout.strip() if result.returncode == 0 else ""
    return name or default


def _require_tailscale() -> str:
    host = _own_host()
    if host is None:
        raise FixupError("у ноды нет tailscale-адреса (tailscale ip -4) — поднимите tailscaled")
    return host


def _all_match(files: dict[Path, str]) -> bool:
    return all(file_matches(path, content) for path, content in files.items())


def _install_with_group(path: Path, content: str, group: str, mode: str) -> None:
    with temp_file(content) as tmp:
        run_sudo(["install", "-D", "-m", mode, "-o", "root", "-g", group, str(tmp), str(path)])


INSTALL_VICTORIA_METRICS = Fixup(
    id="install-victoria-metrics",
    title="Установить victoria-metrics (хранилище метрик хаба)",
    needed=needs.metrics_hub,
    check=lambda: find_executable(VM_PACKAGE) is not None,
    apply=lambda: install_package(VM_PACKAGE, "неизвестен пакетный менеджер для victoria-metrics"),
)


def make_victoria_metrics_config_fixup(settings: Settings) -> Fixup:
    data_dir = settings.metrics.data_dir

    def check() -> bool:
        return (
            path_exists(Path(data_dir))
            and file_matches(VM_DEFAULTS_FILE, vm_defaults_content(settings))
            and file_matches(
                VM_SCRAPE_FILE,
                vm_scrape_content(
                    settings, _own_host(), known_server_nodes(), _swarm_peers(settings)
                ),
            )
            and unit_is_active(VM_UNIT)
        )

    def apply() -> None:
        scrape = vm_scrape_content(
            settings, _own_host(), known_server_nodes(), _swarm_peers(settings)
        )
        run_sudo(["install", "-d", "-m", "0750", "-o", VM_USER, "-g", VM_USER, data_dir])
        install_root_file(VM_SCRAPE_FILE, scrape, mode="0644", make_parents=True)
        previous = read_text_if_exists(VM_DEFAULTS_FILE)
        defaults = vm_defaults_content(settings)
        if previous != defaults:
            install_root_file(VM_DEFAULTS_FILE, defaults, mode="0644")
        enable_now(VM_UNIT)
        if previous != defaults:
            run_sudo(["systemctl", "restart", VM_UNIT])

    return Fixup(
        id="victoria-metrics-config",
        title=f"Настроить victoria-metrics (данные {data_dir})",
        needed=needs.metrics_hub,
        check=check,
        apply=apply,
    )


def _grafana_repo_check() -> bool:
    return path_exists(GRAFANA_KEY_FILE) and file_matches(GRAFANA_SOURCE_FILE, GRAFANA_SOURCE_LINE)


def _grafana_repo_apply() -> None:
    with temp_file("") as tmp:
        download(GRAFANA_KEY_URL, tmp, KEY_FETCH_TIMEOUT_S)
        install_from(tmp, GRAFANA_KEY_FILE, mode="0644", make_parents=True)
    install_root_file(GRAFANA_SOURCE_FILE, GRAFANA_SOURCE_LINE, mode="0644")
    run_sudo(["apt-get", "update"])


GRAFANA_REPO = Fixup(
    id="grafana-repo",
    title="Подключить репозиторий apt.grafana.com",
    needed=needs.metrics_hub,
    check=_grafana_repo_check,
    apply=_grafana_repo_apply,
)

INSTALL_GRAFANA = Fixup(
    id="install-grafana",
    title="Установить grafana",
    needed=needs.metrics_hub,
    check=lambda: find_executable("grafana-server") is not None,
    apply=lambda: install_package(GRAFANA_PACKAGE, "неизвестен пакетный менеджер для grafana"),
)


def make_grafana_config_fixup(settings: Settings) -> Fixup:
    dropin = SYSTEMD_DIR / f"{GRAFANA_UNIT}.d" / DROPIN_NAME

    def expected_ini(host: str) -> str:
        current = read_text_if_exists(GRAFANA_INI)
        if current is None:
            raise FixupError(f"{GRAFANA_INI} нет — сначала install-grafana")
        return set_ini_values(current, grafana_ini_values(settings, host))

    def check() -> bool:
        host = _require_tailscale()
        return (
            file_matches(dropin, tailscale_dropin_content())
            and file_matches(GRAFANA_INI, expected_ini(host))
            and unit_is_active(GRAFANA_UNIT)
        )

    def apply() -> None:
        host = _require_tailscale()
        _install_with_group(GRAFANA_INI, expected_ini(host), GRAFANA_GROUP, "0640")
        install_root_file(dropin, tailscale_dropin_content(), mode="0644", make_parents=True)
        daemon_reload()
        enable_now(GRAFANA_UNIT)
        run_sudo(["systemctl", "restart", GRAFANA_UNIT])

    return Fixup(
        id="grafana-config",
        title="Настроить grafana (слушает только tailscale-адрес, анонимный вход выключен)",
        needed=needs.metrics_hub,
        check=check,
        apply=apply,
    )


def make_grafana_provisioning_fixup(settings: Settings) -> Fixup:
    def apply() -> None:
        for path, content in grafana_provisioning_files(settings).items():
            install_root_file(path, content, mode="0644", make_parents=True)
        run_sudo(["systemctl", "restart", GRAFANA_UNIT])

    return Fixup(
        id="grafana-provisioning",
        title="Выложить источник данных и дашборды grafana",
        needed=needs.metrics_hub,
        check=lambda: _all_match(grafana_provisioning_files(settings)),
        apply=apply,
    )


def metrics_fixups(settings: Settings) -> list[Fixup]:
    return [
        INSTALL_VICTORIA_METRICS,
        make_victoria_metrics_config_fixup(settings),
        GRAFANA_REPO,
        INSTALL_GRAFANA,
        make_grafana_config_fixup(settings),
        make_grafana_provisioning_fixup(settings),
    ]
