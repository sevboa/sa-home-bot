from __future__ import annotations

from collections.abc import Collection, Sequence
from pathlib import Path

from sa_home_bot.config import Settings, SwarmNodeConfig
from sa_home_bot.metrics import protocol
from sa_home_bot.metrics.dashboard import CATALOG, render_dashboard
from sa_home_bot.metrics.scrape import (
    ScrapeJob,
    file_sd_text,
    scrape_targets,
    vpn_scrape_targets,
)
from sa_home_bot.utils.files import atomic_write

TARGETS_SUBDIR = "targets"
DASHBOARDS_SUBDIR = "dashboards"
NODES_TARGETS_FILE = "nodes.json"
VPN_TARGETS_FILE = "vpn.json"
PUBLIC_FILE_MODE = 0o644


def targets_dir(settings: Settings) -> Path:
    return Path(settings.metrics.runtime_dir) / TARGETS_SUBDIR


def dashboards_dir(settings: Settings) -> Path:
    return Path(settings.metrics.runtime_dir) / DASHBOARDS_SUBDIR


def scrape_jobs(settings: Settings) -> list[ScrapeJob]:
    directory = targets_dir(settings)
    return [
        ScrapeJob(protocol.JOB_NAME, str(directory / NODES_TARGETS_FILE)),
        ScrapeJob(protocol.JOB_VPN, str(directory / VPN_TARGETS_FILE)),
    ]


def runtime_files(
    settings: Settings,
    own_host: str | None,
    known_vpn_servers: Collection[str] = (),
    peers: Sequence[SwarmNodeConfig] = (),
) -> dict[Path, str]:
    cfg = settings.metrics
    targets = scrape_targets(settings, own_host, peers)
    directory = targets_dir(settings)
    files = {
        directory / NODES_TARGETS_FILE: file_sd_text(targets, cfg.exporter_port),
        directory / VPN_TARGETS_FILE: file_sd_text(
            vpn_scrape_targets(targets, known_vpn_servers), cfg.vpn_port
        ),
    }
    for name, build in CATALOG.items():
        files[dashboards_dir(settings) / name] = render_dashboard(build())
    return files


def write_changed(files: dict[Path, str]) -> list[Path]:
    written = []
    for path, content in files.items():
        try:
            if path.read_text(encoding="utf-8") == content:
                continue
        except OSError:
            pass
        atomic_write(path, content, mode=PUBLIC_FILE_MODE)
        written.append(path)
    return written
