from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.domain.vpn_check import (
    ALERTING as CHECK_ALERTING,
)
from sa_home_bot.domain.vpn_check import (
    OK as CHECK_OK,
)
from sa_home_bot.domain.vpn_check import (
    CheckResult,
    KnownCheckState,
    reconcile_vpn_check,
    rollup_status,
)
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    Address,
    ProtoError,
)
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.vpn.protocol import (
    EVENT_VPN_CHECK_FAILED,
    EVENT_VPN_CHECK_RECOVERED,
    TELEGRAM_EGRESS_TARGET,
    TELEGRAM_EGRESS_TRANSPORT,
)
from sa_home_bot.vpn.service_common import (
    ACTION_TRIGGER_PEERS,
    CHECK_STALE_FACTOR,
    NODE_SERVICE,
    log,
)
from sa_home_bot.vpn_check import protocol as vpn_check_protocol


class ChecksMixin:
    async def _dispatch_checks(self) -> dict[str, Any]:
        if self._node_link is None:
            log.warning("vpn: node_link не настроен — проверки доступности не разосланы")
            return {"dispatched_to": [], "unreachable": [], "skipped": []}
        try:
            result = await self._node_link.command(
                ACTION_TRIGGER_PEERS,
                {
                    "service": vpn_check_protocol.SERVICE_NAME,
                    "action": vpn_check_protocol.ACTION_CHECK,
                    "args": {"server": self._node, "targets": list(self._cfg.check_targets)},
                    "timeout_s": self._cfg.check_dispatch_timeout_s,
                },
                dst=Address(node=self._node, service=NODE_SERVICE),
                timeout=self._cfg.check_dispatch_timeout_s + 3.0,
            )
        except (ServiceUnavailableError, ProtoError, TimeoutError) as exc:
            log.warning("vpn: не удалось разослать проверки доступности: %s", exc)
            return {"dispatched_to": [], "unreachable": [], "skipped": [], "error": str(exc)}
        return {
            "dispatched_to": result.get("dispatched", []),
            "unreachable": result.get("unreachable", []),
            "skipped": result.get("skipped", []),
        }

    async def check_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.check_interval_s)
            try:
                await self._dispatch_checks()
            except Exception:
                log.exception("vpn: сбой цикла проверок доступности")

    async def _report_check(self, args: dict[str, Any]) -> dict[str, Any]:
        node = str(args.get("node", "")).strip()
        results = args.get("results")
        if not node or not isinstance(results, list) or not results:
            raise ProtoError(ERR_BAD_REQUEST, "нужны node и непустой список results")
        now = utc_now()
        runs: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in results:
            if not isinstance(raw, dict):
                continue
            server = str(raw.get("server", "")).strip()
            transport = str(raw.get("transport", "")).strip()
            target = raw.get("target")
            if not server or not transport or not target:
                continue
            ms_raw = raw.get("ms")
            ms = int(ms_raw) if isinstance(ms_raw, int | float) else None
            error_raw = raw.get("error")
            ok = bool(raw.get("ok"))
            error = str(error_raw) if error_raw else None
            run = runs.setdefault(
                (server, transport), {"failed": [], "recovered": [], "total": 0, "bad": 0}
            )
            run["total"] += 1
            run["bad"] += not ok
            to_status = await self._apply_check_result(
                node, server, transport, str(target), ok, ms, error, now
            )
            if to_status == CHECK_ALERTING:
                run["failed"].append({"target": str(target), "error": error})
            elif to_status == CHECK_OK:
                run["recovered"].append(str(target))
        for (server, transport), run in runs.items():
            base = {"node": node, "server": server, "transport": transport, "total": run["total"]}
            if run["failed"]:
                await self._emit(
                    EVENT_VPN_CHECK_FAILED,
                    {
                        **base,
                        "targets": run["failed"],
                        "all_failed": run["bad"] == run["total"],
                        "consecutive": self._cfg.check_fail_threshold,
                    },
                )
            if run["recovered"]:
                await self._emit(
                    EVENT_VPN_CHECK_RECOVERED,
                    {**base, "targets": run["recovered"], "all_ok": run["bad"] == 0},
                )
        return {"accepted": True}

    async def _apply_check_result(
        self,
        node: str,
        server: str,
        transport: str,
        target: str,
        ok: bool,
        latency_ms: int | None,
        error: str | None,
        now: datetime,
    ) -> str | None:
        cur = await self._db.conn.execute(
            "SELECT status, consecutive_count, alerting_since, first_seen_at, "
            "notified_alert_at, notified_cleared_at FROM vpn_check_states "
            "WHERE node = ? AND server = ? AND transport = ? AND target = ?",
            (node, server, transport, target),
        )
        row = await cur.fetchone()
        known = None
        notified_alert_at = None
        notified_cleared_at = None
        first_seen_at = now.isoformat()
        if row is not None:
            alerting_since = (
                datetime.fromisoformat(row["alerting_since"]) if row["alerting_since"] else None
            )
            known = KnownCheckState(
                status=row["status"],
                consecutive_count=row["consecutive_count"],
                alerting_since=alerting_since,
            )
            notified_alert_at = row["notified_alert_at"]
            notified_cleared_at = row["notified_cleared_at"]
            first_seen_at = row["first_seen_at"]

        result = CheckResult(
            node=node,
            server=server,
            transport=transport,
            target=target,
            ok=ok,
            latency_ms=latency_ms,
            error=error,
        )
        state, transition = reconcile_vpn_check(
            result,
            known,
            now,
            fail_threshold=self._cfg.check_fail_threshold,
            clear_threshold=self._cfg.check_clear_threshold,
        )
        if transition is not None and transition.to_status == CHECK_ALERTING:
            notified_alert_at, notified_cleared_at = now.isoformat(), None
        elif transition is not None and transition.to_status == CHECK_OK:
            notified_cleared_at = now.isoformat()

        await self._db.conn.execute(
            "INSERT INTO vpn_check_states ("
            "node, server, transport, target, status, last_ok, last_latency_ms, last_error, "
            "consecutive_count, alerting_since, first_seen_at, last_seen_at, "
            "notified_alert_at, notified_cleared_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(node, server, transport, target) DO UPDATE SET "
            "status = excluded.status, last_ok = excluded.last_ok, "
            "last_latency_ms = excluded.last_latency_ms, last_error = excluded.last_error, "
            "consecutive_count = excluded.consecutive_count, "
            "alerting_since = excluded.alerting_since, last_seen_at = excluded.last_seen_at, "
            "notified_alert_at = excluded.notified_alert_at, "
            "notified_cleared_at = excluded.notified_cleared_at",
            (
                node,
                server,
                transport,
                target,
                state.status,
                int(state.last_ok),
                state.last_latency_ms,
                state.last_error,
                state.consecutive_count,
                state.alerting_since.isoformat() if state.alerting_since else None,
                first_seen_at,
                now.isoformat(),
                notified_alert_at,
                notified_cleared_at,
            ),
        )
        await self._db.conn.commit()
        return transition.to_status if transition is not None else None

    async def _check_status(self) -> dict[str, Any]:
        cur = await self._db.conn.execute(
            "SELECT node, server, transport, target, status, last_ok, last_latency_ms, "
            "last_error, consecutive_count, alerting_since, last_seen_at FROM vpn_check_states "
            "ORDER BY server, transport, node, target"
        )
        rows = await cur.fetchall()
        states = [
            {
                "node": r["node"],
                "server": r["server"],
                "transport": r["transport"],
                "target": r["target"],
                "status": r["status"],
                "last_ok": bool(r["last_ok"]),
                "last_latency_ms": r["last_latency_ms"],
                "last_error": r["last_error"],
                "consecutive_count": r["consecutive_count"],
                "alerting_since": r["alerting_since"],
                "last_seen_at": r["last_seen_at"],
            }
            for r in rows
        ]
        return {"states": states, "rollup": await self._check_rollup()}

    async def _telegram_egress(self, args: dict[str, Any]) -> dict[str, Any]:
        observer = str(args.get("observer") or "").strip()
        if not observer:
            raise ProtoError(ERR_BAD_REQUEST, "не передан observer")
        cur = await self._db.conn.execute(
            "SELECT last_ok, last_latency_ms, last_seen_at FROM vpn_check_states "
            "WHERE node = ? AND server = ? AND transport = ? AND target = ?",
            (observer, self._node, TELEGRAM_EGRESS_TRANSPORT, TELEGRAM_EGRESS_TARGET),
        )
        row = await cur.fetchone()
        check: dict[str, Any] | None = None
        if row is not None:
            stale_before = (
                utc_now() - timedelta(seconds=self._cfg.check_interval_s * CHECK_STALE_FACTOR)
            ).isoformat()
            check = {
                "ok": bool(row["last_ok"]),
                "ms": row["last_latency_ms"],
                "seen_at": row["last_seen_at"],
                "stale": row["last_seen_at"] < stale_before,
            }
        socks_host = self._cfg.socks_host
        return {
            "node": self._node,
            "label": self._cfg.location,
            "socks": f"{socks_host}:{self._cfg.socks_port}" if socks_host else None,
            "check": check,
        }

    async def _check_rollup(self, *, server: str | None = None) -> list[dict[str, Any]]:
        stale_before = (
            utc_now() - timedelta(seconds=self._cfg.check_interval_s * CHECK_STALE_FACTOR)
        ).isoformat()
        sql = "SELECT node, server, transport, status FROM vpn_check_states WHERE last_seen_at >= ?"
        params: list[Any] = [stale_before]
        if server is not None:
            sql += " AND server = ?"
            params.append(server)
        cur = await self._db.conn.execute(sql, params)
        by_pair: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for row in await cur.fetchall():
            by_pair.setdefault((row["server"], row["transport"]), []).append(
                (row["node"], row["status"])
            )
        return [
            {
                "server": pair_server,
                "transport": transport,
                "status": rollup_status([status for _node, status in rows]),
                "observers": len({node for node, _status in rows}),
            }
            for (pair_server, transport), rows in sorted(by_pair.items())
        ]
