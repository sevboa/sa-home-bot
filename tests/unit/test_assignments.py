"""Строка назначения: роль, инстанс, приоритет — и совместимость со старой формой."""

from __future__ import annotations

import pytest

from sa_home_bot.node.assignments import (
    ROLE_ACTIVE,
    ROLE_STANDBY,
    Assignment,
    AssignmentError,
    has_service,
    parse,
)
from sa_home_bot.node.supervisor import Supervisor


def test_plain_name_still_means_what_it_meant():
    """Существующие config.toml и node-state.json не требуют миграции."""
    assert parse("monitor") == Assignment(service="monitor", role=ROLE_ACTIVE)
    assert parse("monitor").key == "monitor"
    assert not parse("monitor").standby


def test_instance_and_role():
    a = parse("telegram-bot@alfred:standby")
    assert (a.service, a.instance, a.role) == ("telegram-bot", "alfred", ROLE_STANDBY)
    assert a.key == "telegram-bot@alfred"
    assert a.standby


def test_explicit_priority():
    a = parse("telegram-bot@alfred:standby:prio=70")
    assert a.priority == 70
    assert a.role == ROLE_STANDBY


def test_priority_is_none_when_not_given():
    """None значит «спроси у типа машины» — это не то же, что приоритет 0."""
    assert parse("telegram-bot@alfred").priority is None


def test_round_trip():
    for text in ("monitor", "telegram-bot@alfred", "telegram-bot@alfred:standby",
                 "telegram-bot@alfred:standby:prio=70"):
        assert parse(text).to_str() == text


@pytest.mark.parametrize("bad", ["", "   ", "@alfred", "monitor:нечто", "bot@a:prio=много"])
def test_malformed_assignments_are_rejected(bad):
    with pytest.raises(AssignmentError):
        parse(bad)


def test_has_service_ignores_role_and_instance():
    items = ["monitor", "telegram-bot@alfred:standby"]
    assert has_service(items, "monitor")
    assert has_service(items, "telegram-bot")
    assert not has_service(items, "apps")


# --- супервизор ------------------------------------------------------------


def test_supervisor_keys_slots_by_service_and_instance():
    sup = Supervisor(["telegram-bot@alfred", "telegram-bot@work"], None, emit=_noop)
    assert set(sup.services) == {"telegram-bot@alfred", "telegram-bot@work"}


def test_instanced_service_is_spawned_with_its_package():
    sup = Supervisor(["telegram-bot@alfred"], "/etc/sa/config.toml", emit=_noop)
    args = sup.services["telegram-bot@alfred"]._cli_args
    assert args == ["--service", "bot", "--instance", "alfred",
                    "--config", "/etc/sa/config.toml"]


async def test_standby_is_not_started_by_node_startup():
    """Резерв поднимает аренда лидерства, а не старт ноды — иначе два поллера
    одного токена стартовали бы одновременно."""
    sup = Supervisor(["telegram-bot@alfred:standby"], None, emit=_noop)
    await sup.start_all()
    slot = sup.services["telegram-bot@alfred"]
    assert slot.status == "stopped"
    assert slot.pid is None


def test_malformed_assignment_is_skipped_not_fatal(caplog):
    sup = Supervisor(["monitor", "@broken"], None, emit=_noop)
    assert set(sup.services) == {"monitor"}


def test_slot_dict_exposes_role_and_instance():
    sup = Supervisor(["telegram-bot@alfred:standby"], None, emit=_noop)
    data = sup.services["telegram-bot@alfred"].to_dict()
    assert data["service"] == "telegram-bot"
    assert data["instance"] == "alfred"
    assert data["role"] == ROLE_STANDBY


async def _noop(event_type: str, data: dict) -> None:
    return None
