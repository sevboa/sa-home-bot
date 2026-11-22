import json

from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.config import NodeConfig, Settings, SwarmConfig, SwarmNodeConfig, TelegramConfig
from sa_home_bot.domain.host import HostMetricState
from sa_home_bot.domain.models import HealthState
from sa_home_bot.metrics import protocol
from sa_home_bot.metrics.dashboard import CATALOG, nodes_dashboard, render_dashboard
from sa_home_bot.metrics.exposition import build_metrics_app
from sa_home_bot.metrics.scrape import (
    ScrapeJob,
    ScrapeTarget,
    peer_host,
    scrape_config_text,
    scrape_targets,
)
from sa_home_bot.monitor.exposition import render_metrics, store_renderer
from sa_home_bot.node.fixups import build_fixups, metrics, needs
from tests.unit.fixups_world import FakeRoot, make_settings

HUB_IDS = {
    "install-victoria-metrics",
    "victoria-metrics-config",
    "grafana-repo",
    "install-grafana",
    "grafana-config",
    "grafana-provisioning",
}


def hub_settings(**overrides) -> Settings:
    settings = make_settings(["monitor"])
    settings.metrics.hub = True
    settings.node = NodeConfig(id="alfred", assignments=["monitor"], kind="server")
    settings.swarm = SwarmConfig(
        nodes=[
            SwarmNodeConfig(id="jeeves", endpoint="tcp://203.0.113.5:8710", endpoints=["tcp://100.64.0.2:8710"]),
            SwarmNodeConfig(id="mycraft", endpoint="unix:/tmp/x.sock"),
            SwarmNodeConfig(id="wooster", endpoint="tcp://100.64.0.3:8710"),
            SwarmNodeConfig(id="alfred", endpoint="tcp://100.64.0.1:8710"),
        ]
    )
    return settings


def test_needs_follow_hub_flag():
    assert not needs.metrics_hub(make_settings(["monitor"]))
    assert needs.metrics_hub(hub_settings())


def test_build_fixups_hub_only(monkeypatch):
    FakeRoot(monkeypatch)
    ids = {f.id for f in build_fixups(hub_settings())}
    assert HUB_IDS <= ids
    assert not HUB_IDS & {f.id for f in build_fixups(make_settings(["monitor"]))}


def test_peer_host_prefers_overlay_and_skips_unix():
    mixed = ["tcp://203.0.113.5:1", "tcp://100.64.0.2:1", "tcp://192.168.0.5:1"]
    assert peer_host(mixed) == "100.64.0.2"
    assert peer_host(["unix:/tmp/x.sock", "tcp://127.0.0.1:1", "tcp://0.0.0.0:1"]) is None


def test_scrape_targets_use_own_address_for_self():
    targets = scrape_targets(hub_settings(), "100.64.0.9")
    assert targets == [
        ScrapeTarget("alfred", "100.64.0.9"),
        ScrapeTarget("jeeves", "100.64.0.2"),
        ScrapeTarget("wooster", "100.64.0.3"),
    ]


def test_scrape_config_text():
    text = scrape_config_text(
        [ScrapeJob("sa-home-nodes", 9100, [ScrapeTarget("jeeves", "100.64.0.2")])], "30s"
    )
    assert text == (
        "global:\n"
        '  scrape_interval: "30s"\n'
        "scrape_configs:\n"
        '  - job_name: "sa-home-nodes"\n'
        '    metrics_path: "/metrics"\n'
        "    static_configs:\n"
        '      - targets: ["100.64.0.2:9100"]\n'
        "        labels:\n"
        '          node: "jeeves"\n'
    )
    assert "static_configs: []" in scrape_config_text([ScrapeJob("sa-home-nodes", 9100, [])], "30s")


def test_vm_args():
    assert metrics.vm_args(hub_settings()) == (
        "-storageDataPath=/mnt/data/metrics -retentionPeriod=1y "
        "-httpListenAddr=127.0.0.1:8428 -promscrape.config=/etc/victoria-metrics/scrape.yml "
        "-promscrape.configCheckInterval=1m"
    )


GRAFANA_INI_SAMPLE = (
    "[server]\n;http_addr =\n;http_port = 3000\n[auth.anonymous]\n;enabled = true\n"
    "[analytics]\nreporting_enabled = true\n"
)


def test_set_ini_values_uncomments_replaces_and_is_idempotent():
    values = metrics.grafana_ini_values(hub_settings(), "100.64.0.1")
    once = metrics.set_ini_values(GRAFANA_INI_SAMPLE, values)
    assert "http_addr = 100.64.0.1\n" in once
    assert "http_port = 3000\n" in once
    assert "[auth.anonymous]\nenabled = false\n" in once
    assert "reporting_enabled = false" in once
    assert "check_for_updates = false" in once
    assert metrics.set_ini_values(once, values) == once


def test_set_ini_values_appends_missing_section():
    text = metrics.set_ini_values("", {"server": {"http_port": "1"}})
    assert text == "\n[server]\nhttp_port = 1\n"


def test_dropin_waits_for_tailscaled():
    content = metrics.tailscale_dropin_content()
    assert "After=tailscaled.service" in content
    assert "Restart=on-failure" in content


def test_dashboard_is_valid_json_with_vm_datasource():
    dashboard = json.loads(render_dashboard(nodes_dashboard()))
    assert dashboard["uid"]
    assert set(CATALOG) == {"nodes.json", "vpn.json"}
    datasources = [
        target["datasource"]["uid"] for panel in dashboard["panels"] for target in panel["targets"]
    ]
    assert datasources and set(datasources) == {protocol.DATASOURCE_UID}
    exprs = " ".join(t["expr"] for p in dashboard["panels"] for t in p["targets"])
    for metric in ("steal_pct", "iowait_pct", "psi_cpu", "conntrack_pct", "net_err_rate"):
        assert protocol.HOST_METRIC_PREFIX + metric in exprs
    assert protocol.METRIC_TEMPERATURE in exprs
    variable = dashboard["templating"]["list"][0]
    assert variable["multi"] and variable["includeAll"]
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids))


def test_provisioning_files_cover_datasource_provider_and_dashboards():
    files = metrics.grafana_provisioning_files(hub_settings())
    assert "uid: vm" in files[metrics.GRAFANA_DATASOURCE_FILE]
    assert "http://127.0.0.1:8428" in files[metrics.GRAFANA_DATASOURCE_FILE]
    assert str(metrics.GRAFANA_DASHBOARD_DIR) in files[metrics.GRAFANA_DASHBOARD_PROVIDER_FILE]
    assert metrics.GRAFANA_DASHBOARD_DIR / "nodes.json" in files
    assert metrics.GRAFANA_DASHBOARD_DIR / "vpn.json" in files


def _health(component: str, temp: float) -> HealthState:
    return HealthState(component, "cpu", "CPU", "ok", temp, 0, None)


def _host(metric: str, value: float) -> HostMetricState:
    return HostMetricState(f"host:{metric}", metric, "x", value, "%", "ok", 0, None)


def test_render_metrics_text():
    text = render_metrics([_health('cpu:"0"', 41.5)], [_host("steal_pct", 2.5), _host("psi_io", 0)])
    assert "sa_up 1\n" in text
    assert "# TYPE sa_host_steal_pct gauge\nsa_host_steal_pct 2.5\n" in text
    assert "sa_host_psi_io 0\n" in text
    assert 'sa_temperature_celsius{kind="cpu",component="cpu:\\"0\\""} 41.5\n' in text


def test_render_metrics_without_data_has_only_up():
    assert render_metrics([], []).count("sa_") == 3


async def test_metrics_endpoint_serves_store_values():
    class StoreStub:
        async def get_all_states(self):
            return [_health("cpu:0", 40.0)]

        async def get_all_host_states(self):
            return [_host("steal_pct", 1.0)]

    async with TestClient(TestServer(build_metrics_app(store_renderer(StoreStub())))) as client:
        resp = await client.get(protocol.METRICS_PATH)
        body = await resp.text()
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/plain")
        assert "sa_host_steal_pct 1\n" in body


def test_metrics_config_defaults():
    cfg = Settings(telegram=TelegramConfig(token="x")).metrics
    assert (cfg.export, cfg.hub, cfg.exporter_port, cfg.vm_port, cfg.grafana_port) == (
        False, False, 9100, 8428, 3000,
    )
    assert cfg.data_dir == "/mnt/data/metrics"
