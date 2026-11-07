from __future__ import annotations

import pytest

from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.reality.xray import DualXrayBackend, reconcile_clients


class FakeBackend:
    def __init__(self, name: str) -> None:
        self.name = name
        self.clients: dict[str, str] = {}
        self.traffic: dict[str, tuple[int, int]] = {}
        self.add_calls: list[tuple[str, str, str]] = []
        self.remove_calls: list[str] = []
        self.list_calls: int = 0
        self.stats_calls: int = 0
        self.should_fail_add: set[str] = set()
        self.should_fail_remove: set[str] = set()
        self.should_fail_list: bool = False
        self.should_fail_stats: bool = False

    async def add_client(self, client_uuid: str, email: str, flow: str) -> None:
        self.add_calls.append((client_uuid, email, flow))
        if email in self.should_fail_add:
            raise ProtoError("INTERNAL", f"{self.name} add_client failed")
        self.clients[email] = client_uuid
        self.traffic.setdefault(email, (0, 0))

    async def remove_client(self, email: str) -> None:
        self.remove_calls.append(email)
        if email in self.should_fail_remove:
            raise ProtoError("INTERNAL", f"{self.name} remove_client failed")
        self.clients.pop(email, None)

    async def list_clients(self) -> set[str]:
        self.list_calls += 1
        if self.should_fail_list:
            raise ProtoError("INTERNAL", f"{self.name} list_clients failed")
        return set(self.clients)

    async def stats(self) -> dict[str, tuple[int, int]]:
        self.stats_calls += 1
        if self.should_fail_stats:
            raise ProtoError("INTERNAL", f"{self.name} stats failed")
        return {email: self.traffic.get(email, (0, 0)) for email in self.clients}


async def test_dual_add_client_on_both() -> None:
    primary = FakeBackend("primary")
    alt = FakeBackend("alt")
    dual = DualXrayBackend(primary, alt)

    await dual.add_client("uuid-1", "c1-phone", "xtls-rprx-vision")

    assert primary.clients == {"c1-phone": "uuid-1"}
    assert alt.clients == {"c1-phone": "uuid-1"}
    assert primary.add_calls == [("uuid-1", "c1-phone", "xtls-rprx-vision")]
    assert alt.add_calls == [("uuid-1", "c1-phone", "xtls-rprx-vision")]


async def test_dual_add_client_skips_alt_if_exists() -> None:
    primary = FakeBackend("primary")
    alt = FakeBackend("alt")
    alt.clients["c1-phone"] = "uuid-1"
    dual = DualXrayBackend(primary, alt)

    await dual.add_client("uuid-1", "c1-phone", "xtls-rprx-vision")

    assert primary.clients == {"c1-phone": "uuid-1"}
    assert alt.clients == {"c1-phone": "uuid-1"}
    assert primary.add_calls == [("uuid-1", "c1-phone", "xtls-rprx-vision")]
    assert alt.add_calls == []


async def test_dual_remove_client_on_both() -> None:
    primary = FakeBackend("primary")
    alt = FakeBackend("alt")
    primary.clients["c1-phone"] = "uuid-1"
    alt.clients["c1-phone"] = "uuid-1"
    dual = DualXrayBackend(primary, alt)

    await dual.remove_client("c1-phone")

    assert primary.clients == {}
    assert alt.clients == {}
    assert primary.remove_calls == ["c1-phone"]
    assert alt.remove_calls == ["c1-phone"]


async def test_dual_stats_calls_only_primary() -> None:
    primary = FakeBackend("primary")
    alt = FakeBackend("alt")
    primary.clients["c1-phone"] = "uuid-1"
    primary.traffic["c1-phone"] = (10, 20)
    alt.clients["c1-phone"] = "uuid-1"
    alt.traffic["c1-phone"] = (100, 200)
    dual = DualXrayBackend(primary, alt)

    result = await dual.stats()

    assert result == {"c1-phone": (10, 20)}
    assert primary.stats_calls == 1
    assert alt.stats_calls == 0


async def test_dual_alt_add_failure_logged_primary_succeeds(caplog) -> None:
    primary = FakeBackend("primary")
    alt = FakeBackend("alt")
    alt.should_fail_add.add("c1-phone")
    dual = DualXrayBackend(primary, alt)

    await dual.add_client("uuid-1", "c1-phone", "xtls-rprx-vision")

    assert primary.clients == {"c1-phone": "uuid-1"}
    assert alt.clients == {}
    assert "alt add_client" in caplog.text or "alt" in caplog.text


async def test_dual_alt_remove_failure_logged_primary_succeeds(caplog) -> None:
    primary = FakeBackend("primary")
    alt = FakeBackend("alt")
    primary.clients["c1-phone"] = "uuid-1"
    alt.clients["c1-phone"] = "uuid-1"
    alt.should_fail_remove.add("c1-phone")
    dual = DualXrayBackend(primary, alt)

    await dual.remove_client("c1-phone")

    assert primary.clients == {}
    assert alt.clients == {"c1-phone": "uuid-1"}
    assert "alt remove_client" in caplog.text or "alt" in caplog.text


async def test_dual_primary_add_failure_raises() -> None:
    primary = FakeBackend("primary")
    alt = FakeBackend("alt")
    primary.should_fail_add.add("c1-phone")
    dual = DualXrayBackend(primary, alt)

    with pytest.raises(ProtoError):
        await dual.add_client("uuid-1", "c1-phone", "xtls-rprx-vision")

    assert primary.clients == {}
    assert alt.clients == {}


async def test_dual_primary_remove_failure_raises() -> None:
    primary = FakeBackend("primary")
    alt = FakeBackend("alt")
    primary.clients["c1-phone"] = "uuid-1"
    alt.clients["c1-phone"] = "uuid-1"
    primary.should_fail_remove.add("c1-phone")
    dual = DualXrayBackend(primary, alt)

    with pytest.raises(ProtoError):
        await dual.remove_client("c1-phone")

    assert primary.clients == {"c1-phone": "uuid-1"}
    assert alt.clients == {"c1-phone": "uuid-1"}


async def test_reconcile_dual_fixes_missing_on_alt(caplog) -> None:
    primary = FakeBackend("primary")
    alt = FakeBackend("alt")
    primary.clients["c1-phone"] = "uuid-1"
    alt.clients["c1-phone"] = "uuid-1"
    dual = DualXrayBackend(primary, alt)

    desired = {"c1-phone": "uuid-1", "c2-laptop": "uuid-2"}
    await reconcile_clients(dual, desired, "xtls-rprx-vision")

    assert primary.clients == {"c1-phone": "uuid-1", "c2-laptop": "uuid-2"}
    assert alt.clients == {"c1-phone": "uuid-1", "c2-laptop": "uuid-2"}


async def test_reconcile_dual_removes_extra_on_both() -> None:
    primary = FakeBackend("primary")
    alt = FakeBackend("alt")
    primary.clients["c1-phone"] = "uuid-1"
    primary.clients["c2-laptop"] = "uuid-2"
    alt.clients["c1-phone"] = "uuid-1"
    alt.clients["c3-tablet"] = "uuid-3"
    dual = DualXrayBackend(primary, alt)

    desired = {"c1-phone": "uuid-1"}
    await reconcile_clients(dual, desired, "xtls-rprx-vision")

    assert primary.clients == {"c1-phone": "uuid-1"}
    assert alt.clients == {"c1-phone": "uuid-1"}


async def test_reconcile_dual_alt_failure_logged_primary_succeeds(caplog) -> None:
    primary = FakeBackend("primary")
    alt = FakeBackend("alt")
    primary.clients["c1-phone"] = "uuid-1"
    alt.clients["c1-phone"] = "uuid-1"
    alt.should_fail_list = True
    dual = DualXrayBackend(primary, alt)

    desired = {"c1-phone": "uuid-1", "c2-laptop": "uuid-2"}
    await reconcile_clients(dual, desired, "xtls-rprx-vision")

    assert primary.clients == {"c1-phone": "uuid-1", "c2-laptop": "uuid-2"}
    assert alt.clients == {"c1-phone": "uuid-1"}
    assert "alt reconcile" in caplog.text or "alt" in caplog.text


async def test_reconcile_dual_primary_failure_raises() -> None:
    primary = FakeBackend("primary")
    alt = FakeBackend("alt")
    primary.should_fail_list = True
    dual = DualXrayBackend(primary, alt)

    desired = {"c1-phone": "uuid-1"}
    with pytest.raises(ProtoError):
        await reconcile_clients(dual, desired, "xtls-rprx-vision")


async def test_reconcile_single_backend_works() -> None:
    backend = FakeBackend("single")
    backend.clients["c1-phone"] = "uuid-1"
    backend.clients["c2-laptop"] = "uuid-2"

    desired = {"c1-phone": "uuid-1", "c3-tablet": "uuid-3"}
    await reconcile_clients(backend, desired, "xtls-rprx-vision")

    assert backend.clients == {"c1-phone": "uuid-1", "c3-tablet": "uuid-3"}
    assert backend.remove_calls == ["c2-laptop"]
    assert backend.add_calls == [("uuid-3", "c3-tablet", "xtls-rprx-vision")]


async def test_reconcile_single_backend_failure_raises() -> None:
    backend = FakeBackend("single")
    backend.should_fail_list = True

    desired = {"c1-phone": "uuid-1"}
    with pytest.raises(ProtoError):
        await reconcile_clients(backend, desired, "xtls-rprx-vision")
