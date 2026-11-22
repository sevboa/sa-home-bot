from __future__ import annotations

import json
from datetime import timedelta

import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.bot import vpn_check_view
from sa_home_bot.config import Settings, TelegramConfig, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.metrics import protocol
from sa_home_bot.metrics.dashboard import CATALOG, render_dashboard
from sa_home_bot.metrics.exposition import build_metrics_app
from sa_home_bot.metrics.scrape import (
    ScrapeJob,
    ScrapeTarget,
    scrape_config_text,
    vpn_scrape_targets,
)
from sa_home_bot.metrics.vpn import CheckRow, LinkRow, render_vpn_metrics
from sa_home_bot.metrics.vpn_dashboard import VPN_DASHBOARD_UID, vpn_dashboard
from sa_home_bot.node.fixups import metrics as fixup_metrics
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.vpn.service import VpnService
from tests.unit.test_metrics_stack import hub_settings
from tests.unit.test_vpn_service_link import DummyAwg


def _check(
    observer, transport="reality", target="https://a", ok=True, latency=120, server="jeeves"
):
    return CheckRow(observer, server, transport, target, ok, latency)


def test_render_vpn_metrics_labels_ratio_and_scope():
    text = render_vpn_metrics(
        [
            _check("osip", target="https://a"),
            _check("osip", target="https://b", ok=False, latency=None),
            _check("alfred"),
            _check("zakhar"),
        ],
        [LinkRow("osip", "jeeves", "reality", "partial"), LinkRow("zakhar", "jeeves", "awg", "ok")],
        russia=["osip"],
        ignored=["zakhar"],
    )
    assert (
        'sa_vpn_check_ok{observer="osip",server="jeeves",transport="reality",scope="russia",'
        'target="https://b"} 0\n'
    ) in text
    assert (
        'sa_vpn_check_ok_ratio{observer="osip",server="jeeves",transport="reality",'
        'scope="russia"} 0.5\n'
    ) in text
    assert (
        'sa_vpn_check_ok_ratio{observer="alfred",server="jeeves",transport="reality",'
        'scope="global"} 1\n'
    ) in text
    assert 'target="https://b"} 120' not in text
    assert text.count("sa_vpn_check_latency_ms{") == 2
    assert (
        'sa_vpn_link_level{observer="osip",server="jeeves",transport="reality",scope="russia"} 1\n'
        in text
    )
    assert "zakhar" not in text


def test_render_vpn_metrics_empty_has_only_headers():
    text = render_vpn_metrics([], [], [], [])
    assert text.count("# TYPE") == 4
    assert "sa_vpn_check_ok{" not in text


@pytest_asyncio.fixture
async def svc(tmp_path):
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)
    cfg = VpnConfig(russia_observers=["osip"], ignored_observers=["zakhar"])
    service = VpnService(Settings(vpn=cfg), db, DummyAwg(), None)
    yield service, db, cfg
    await db.close()


async def _insert_check(db, node, transport, seen_at, *, ok=1):
    await db.conn.execute(
        "INSERT INTO vpn_check_states (node, server, transport, target, status, last_ok, "
        "last_latency_ms, first_seen_at, last_seen_at) VALUES (?, 'jeeves', ?, 'https://a', "
        "'ok', ?, 90, ?, ?)",
        (node, transport, ok, seen_at.isoformat(), seen_at.isoformat()),
    )


async def test_service_metrics_drop_stale_rows_per_transport(svc):
    service, db, cfg = svc
    now = utc_now()
    fresh_awg = now - timedelta(seconds=cfg.awg_check_max_s * 2)
    stale_reality = now - timedelta(seconds=(cfg.check_interval_s + cfg.check_jitter_s) * 4)
    await _insert_check(db, "osip", "awg", fresh_awg)
    await _insert_check(db, "osip", "reality", stale_reality)
    await _insert_check(db, "alfred", "reality", now)
    await db.conn.execute(
        "INSERT INTO vpn_link_states (node, server, transport, level, candidate, announced, "
        "updated_at) VALUES ('osip', 'jeeves', 'awg', 'ok', 'ok', 'ok', ?)",
        (fresh_awg.isoformat(),),
    )
    await db.conn.commit()
    text = await service.render_metrics()
    assert 'observer="osip",server="jeeves",transport="awg"' in text
    assert 'observer="osip",server="jeeves",transport="reality"' not in text
    assert 'observer="alfred",server="jeeves",transport="reality"' in text
    assert (
        'sa_vpn_link_level{observer="osip",server="jeeves",transport="awg",scope="russia"} 2'
        in text
    )


async def test_vpn_endpoint_serves_rendered_text(svc):
    service, db, _cfg = svc
    await _insert_check(db, "alfred", "reality", utc_now())
    await db.conn.commit()
    async with TestClient(TestServer(build_metrics_app(service.render_metrics))) as client:
        resp = await client.get(protocol.METRICS_PATH)
        assert resp.status == 200
        assert "sa_vpn_check_ok_ratio{" in await resp.text()


def test_vpn_scrape_targets_filter_known_or_fall_back_to_all():
    targets = [ScrapeTarget("alfred", "100.64.0.1"), ScrapeTarget("jeeves", "100.64.0.2")]
    assert vpn_scrape_targets(targets, ["jeeves"]) == [targets[1]]
    assert vpn_scrape_targets(targets, []) == targets


def test_scrape_text_has_vpn_job_on_vpn_port():
    text = fixup_metrics.vm_scrape_content(hub_settings())
    assert '  - job_name: "sa-home-nodes"' in text
    vpn_part = text.split('  - job_name: "sa-home-vpn"')[1]
    assert "targets/vpn.json" in vpn_part
    assert "file_sd_configs:" in scrape_config_text([ScrapeJob("x", "/a.json")], "30s")


def test_vpn_dashboard_json_is_registered_and_complete():
    assert CATALOG["vpn.json"] is vpn_dashboard
    dashboard = json.loads(render_dashboard(vpn_dashboard()))
    assert dashboard["uid"] == VPN_DASHBOARD_UID == "sa-home-vpn"
    names = {v["name"] for v in dashboard["templating"]["list"]}
    assert names == {"observer", "server", "transport"}
    assert all(v["multi"] and v["includeAll"] for v in dashboard["templating"]["list"])
    types = [p["type"] for p in dashboard["panels"]]
    assert types.count("status-history") == 2
    assert "table" in types
    exprs = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]
    assert any('scope="russia"' in e for e in exprs)
    assert any("avg_over_time" in e and "[15m]" in e for e in exprs)
    assert any(e.endswith("== 0") for e in exprs)
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    datasources = {
        t["datasource"]["uid"] for p in dashboard["panels"] for t in p.get("targets", [])
    }
    assert datasources == {protocol.DATASOURCE_UID}


def _buttons(keyboard):
    return [b for row in keyboard.inline_keyboard for b in row]


def test_grafana_button_present_only_with_public_url():
    with_url = _buttons(vpn_check_view.status_keyboard("https://alfred.tail61d88b.ts.net/"))
    link = [b for b in with_url if b.url]
    assert [b.url for b in link] == ["https://alfred.tail61d88b.ts.net/d/sa-home-vpn"]
    assert not [b for b in _buttons(vpn_check_view.status_keyboard()) if b.url]


def test_metrics_config_vpn_defaults():
    cfg = Settings(telegram=TelegramConfig(token="x")).metrics
    assert (cfg.vpn_port, cfg.grafana_public_url) == (9101, "")
