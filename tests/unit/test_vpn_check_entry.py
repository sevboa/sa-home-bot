from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sa_home_bot.bot import vpn_check_view
from sa_home_bot.config import RealityAltConfig, RealityTransportConfig, RealityXhttpConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.domain.vpn_history import Sample, aggregate
from sa_home_bot.vpn import protocol as vpn_protocol
from tests.unit import test_vpn_service_report_check as report_check

TARGET = report_check.TARGET
env = report_check.env

AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


async def _report(svc, node: str, entry: object = None, *, transport: str = "reality"):
    row = {
        "server": "jeeves",
        "transport": transport,
        "target": TARGET,
        "ok": True,
        "ms": 12,
        "error": None,
    }
    if entry is not None:
        row["entry"] = entry
    return await svc.run_command(vpn_protocol.ACTION_REPORT_CHECK, {"node": node, "results": [row]})


async def _status(svc) -> dict:
    return await svc.run_command(vpn_protocol.ACTION_CHECK_STATUS, {})


async def test_entry_reaches_status_rollup_and_view(env):
    svc, _, _ = env
    await _report(svc, "alfred", "Reality azgez")
    status = await _status(svc)
    assert status["states"][0]["entry"] == "Reality azgez"
    assert status["rollup"][0]["entry"] == "Reality azgez"
    assert vpn_check_view.health_line({"check": status["rollup"]}) == "🛰 Reality azgez 🟢"
    assert "Reality azgez" in vpn_check_view.status_text(status["states"], rollup=status["rollup"])


async def test_old_node_without_entry_keeps_rows_unchanged(env):
    svc, _, _ = env
    await _report(svc, "alfred")
    status = await _status(svc)
    assert "entry" not in status["states"][0]
    assert "entry" not in status["rollup"][0]
    assert vpn_check_view.health_line({"check": status["rollup"]}) == "🛰 VLESS (Reality) 🟢"


async def test_entry_survives_report_without_it_and_is_replaced_by_new_name(env):
    svc, _, _ = env
    await _report(svc, "alfred", "Reality google")
    await _report(svc, "alfred")
    assert (await _status(svc))["states"][0]["entry"] == "Reality google"
    await _report(svc, "alfred", "Reality azgez")
    assert (await _status(svc))["states"][0]["entry"] == "Reality azgez"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("x" * 80, "x" * 40), ("  AWG 1.0 ", "AWG 1.0"), (123, None), ("   ", None)],
)
async def test_entry_is_trimmed_and_non_strings_ignored(env, raw, expected):
    svc, _, _ = env
    await _report(svc, "alfred", raw, transport="awg")
    assert (await _status(svc))["states"][0].get("entry") == expected


async def test_history_series_carry_entry(env):
    svc, _, _ = env
    await _report(svc, "alfred", "XHTTP feedwonk", transport="reality_xhttp")
    await _report(svc, "alfred", transport="awg")
    history = await svc.run_command(vpn_protocol.ACTION_CHECK_HISTORY, {"hours": 1})
    by_transport = {row["transport"]: row for row in history["series"]}
    assert by_transport["reality_xhttp"]["entry"] == "XHTTP feedwonk"
    assert "entry" not in by_transport["awg"]


def test_aggregate_takes_latest_non_empty_entry():
    samples = [
        Sample(AT, "alfred", "jeeves", "reality", 1, 0, 10, "Reality google"),
        Sample(AT.replace(hour=13), "alfred", "jeeves", "reality", 1, 0, 10, "Reality azgez"),
        Sample(AT.replace(hour=14), "alfred", "jeeves", "reality", 1, 0, 10, None),
    ]
    assert aggregate(samples, 900)[0]["entry"] == "Reality azgez"


async def test_migration_adds_entry_columns_to_old_tables(tmp_path):
    db = Database(tmp_path / "old.sqlite")
    await db.open()
    await db.conn.execute(
        "CREATE TABLE vpn_check_states (node TEXT NOT NULL, server TEXT NOT NULL, "
        "transport TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL, last_ok INTEGER, "
        "last_latency_ms INTEGER, last_error TEXT, consecutive_count INTEGER NOT NULL DEFAULT 0, "
        "alerting_since TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, "
        "notified_alert_at TEXT, notified_cleared_at TEXT, "
        "PRIMARY KEY (node, server, transport, target))"
    )
    await db.conn.execute(
        "CREATE TABLE vpn_check_samples (at TEXT NOT NULL, node TEXT NOT NULL, "
        "server TEXT NOT NULL, transport TEXT NOT NULL, total INTEGER NOT NULL, "
        "bad INTEGER NOT NULL, median_ms INTEGER)"
    )
    await db.conn.execute(
        "INSERT INTO vpn_check_samples VALUES "
        "('2026-11-20T00:00:00+00:00', 'a', 's', 'awg', 1, 0, 5)"
    )
    await db.conn.commit()
    await apply_migrations(db)
    for table in ("vpn_check_states", "vpn_check_samples"):
        cur = await db.conn.execute(f"PRAGMA table_info({table})")
        assert "entry" in {row["name"] for row in await cur.fetchall()}
    cur = await db.conn.execute("SELECT entry FROM vpn_check_samples")
    assert [row["entry"] for row in await cur.fetchall()] == [None]
    await db.close()


def _reality_cfg(*, alt: bool = True, xhttp: bool = True) -> RealityTransportConfig:
    return RealityTransportConfig(
        sni="www.azgez.com",
        alt=RealityAltConfig(port=2083, sni="www.feedwonk.com") if alt else None,
        xhttp=RealityXhttpConfig(port=2087, sni="www.mtg.org", path="/p") if xhttp else None,
    )


@pytest.mark.parametrize(
    ("transport", "expected"),
    [
        ("reality", "Reality azgez"),
        ("reality_alt", "Reality feedwonk"),
        ("reality_xhttp", "XHTTP mtg"),
    ],
)
async def test_server_sni_overrides_observer_entry(env, transport, expected):
    svc, _, _ = env
    svc._node = "jeeves"
    svc._cfg.reality = _reality_cfg()
    await _report(svc, "alfred", "Reality", transport=transport)
    assert (await _status(svc))["states"][0]["entry"] == expected


@pytest.mark.parametrize("transport", ["reality", "reality_alt", "reality_xhttp"])
async def test_observer_entry_kept_without_server_section(env, transport):
    svc, _, _ = env
    svc._node = "jeeves"
    svc._cfg.reality = _reality_cfg(alt=False, xhttp=False) if transport != "reality" else None
    await _report(svc, "alfred", "Reality", transport=transport)
    assert (await _status(svc))["states"][0]["entry"] == "Reality"


@pytest.mark.parametrize("transport", ["awg", "awg_alt"])
async def test_awg_entry_unchanged_by_server_sni(env, transport):
    svc, _, _ = env
    svc._node = "jeeves"
    svc._cfg.reality = _reality_cfg()
    await _report(svc, "alfred", "AWG X", transport=transport)
    assert (await _status(svc))["states"][0]["entry"] == "AWG X"


async def test_other_server_keeps_observer_entry(env):
    svc, _, _ = env
    svc._node = "wooster"
    svc._cfg.reality = _reality_cfg()
    await _report(svc, "alfred", "Reality")
    assert (await _status(svc))["states"][0]["entry"] == "Reality"
