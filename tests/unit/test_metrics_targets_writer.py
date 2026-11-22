import json

from sa_home_bot.config import SwarmNodeConfig
from sa_home_bot.node import metrics_targets
from sa_home_bot.node.state import NodeState
from tests.unit.test_metrics_stack import hub_settings


async def test_refresh_writes_live_peers_then_idles(monkeypatch, tmp_path):
    settings = hub_settings()
    settings.metrics.runtime_dir = str(tmp_path)
    monkeypatch.setattr(metrics_targets, "local_tailscale_address", lambda: "100.64.0.1")
    monkeypatch.setattr(metrics_targets, "known_server_nodes", lambda: [])
    state = NodeState()
    writer = metrics_targets.MetricsTargetsWriter(settings, state)
    assert await writer.refresh()
    assert await writer.refresh() == []
    state.peers.append(SwarmNodeConfig(id="ion", endpoint="tcp://100.64.0.4:8710"))
    changed = await writer.refresh()
    assert changed == [tmp_path / "targets" / "nodes.json", tmp_path / "targets" / "vpn.json"]
    nodes = json.loads((tmp_path / "targets" / "nodes.json").read_text())
    assert "ion" in {group["labels"]["node"] for group in nodes}
    assert (tmp_path / "dashboards" / "vpn.json").exists()
