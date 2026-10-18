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


def test_plain_name_still_means_what_it_meant():
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
    assert parse("telegram-bot@alfred").priority is None


@pytest.mark.parametrize("bad", ["", "   ", "@alfred", "monitor:нечто", "bot@a:prio=много"])
def test_malformed_assignments_are_rejected(bad):
    with pytest.raises(AssignmentError):
        parse(bad)


def test_has_service_ignores_role_and_instance():
    items = ["monitor", "telegram-bot@alfred:standby"]
    assert has_service(items, "monitor")
    assert has_service(items, "telegram-bot")
    assert not has_service(items, "apps")
