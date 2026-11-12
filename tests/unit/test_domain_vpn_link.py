import pytest

from sa_home_bot.domain import vpn_link
from sa_home_bot.domain.vpn_link import (
    LEVEL_DOWN,
    LEVEL_OK,
    LEVEL_PARTIAL,
    SCOPE_GLOBAL,
    SCOPE_RUSSIA,
    LinkState,
    link_level,
    reconcile_link,
    scope_level,
    scope_levels,
    scope_of,
)

THRESHOLD = 3


def _drive(levels: list[str], threshold: int = THRESHOLD):
    known: LinkState | None = None
    transitions = []
    final_state = None
    for level in levels:
        state, transition = reconcile_link(known, level, threshold)
        final_state = state
        known = state
        if transition is not None:
            transitions.append((transition.from_announced, transition.to_announced))
    return final_state, transitions


def test_link_level_by_bad_count():
    assert link_level(6, 0) == LEVEL_OK
    assert link_level(6, 6) == LEVEL_DOWN
    assert link_level(6, 2) == LEVEL_PARTIAL
    assert link_level(0, 0) == LEVEL_DOWN


def test_first_down_report_announces_down_without_transition():
    state, transitions = _drive([LEVEL_DOWN])
    assert transitions == []
    assert state.announced == LEVEL_DOWN


def test_first_partial_report_announces_ok():
    state, transitions = _drive([LEVEL_PARTIAL])
    assert transitions == []
    assert state.announced == LEVEL_OK


def test_down_below_threshold_does_not_announce():
    state, transitions = _drive([LEVEL_OK, LEVEL_DOWN, LEVEL_DOWN])
    assert transitions == []
    assert state.level == LEVEL_OK
    assert state.candidate == LEVEL_DOWN
    assert state.candidate_count == 2


def test_down_reaching_threshold_announces_once():
    state, transitions = _drive([LEVEL_OK, LEVEL_DOWN, LEVEL_DOWN, LEVEL_DOWN])
    assert transitions == [(LEVEL_OK, LEVEL_DOWN)]
    assert state.level == LEVEL_DOWN
    assert state.announced == LEVEL_DOWN


def test_debounce_never_announces():
    state, transitions = _drive(
        [LEVEL_OK, LEVEL_DOWN, LEVEL_OK, LEVEL_DOWN, LEVEL_OK, LEVEL_DOWN]
    )
    assert transitions == []
    assert state.level == LEVEL_OK
    assert state.announced == LEVEL_OK


def test_partial_never_announced():
    state, transitions = _drive([LEVEL_OK, LEVEL_PARTIAL, LEVEL_PARTIAL, LEVEL_PARTIAL])
    assert transitions == []
    assert state.level == LEVEL_PARTIAL
    assert state.announced == LEVEL_OK


def test_gradual_drop_announces_only_at_the_edge():
    state, transitions = _drive(
        [LEVEL_OK, LEVEL_PARTIAL, LEVEL_PARTIAL, LEVEL_PARTIAL, LEVEL_DOWN, LEVEL_DOWN, LEVEL_DOWN]
    )
    assert transitions == [(LEVEL_OK, LEVEL_DOWN)]
    assert state.level == LEVEL_DOWN
    assert state.announced == LEVEL_DOWN


def test_recovery_announces_edge():
    state, transitions = _drive([LEVEL_DOWN, LEVEL_OK, LEVEL_OK, LEVEL_OK])
    assert transitions == [(LEVEL_DOWN, LEVEL_OK)]
    assert state.level == LEVEL_OK
    assert state.announced == LEVEL_OK


def test_recovery_through_partial_announces_once():
    state, transitions = _drive(
        [LEVEL_DOWN, LEVEL_PARTIAL, LEVEL_PARTIAL, LEVEL_PARTIAL, LEVEL_OK, LEVEL_OK, LEVEL_OK]
    )
    assert transitions == [(LEVEL_DOWN, LEVEL_OK)]
    assert state.level == LEVEL_OK
    assert state.announced == LEVEL_OK


def test_candidate_change_resets_count():
    state, transitions = _drive(
        [LEVEL_OK, LEVEL_DOWN, LEVEL_DOWN, LEVEL_PARTIAL, LEVEL_PARTIAL, LEVEL_PARTIAL]
    )
    assert transitions == []
    assert state.level == LEVEL_PARTIAL
    assert state.announced == LEVEL_OK


def test_scope_of_ignored_observer():
    assert scope_of("zakhar", ["zakhar"], ["zakhar"]) is None
    assert scope_of("zakhar", [], ["zakhar"]) is None


def test_scope_of_russia_observer():
    assert scope_of("osip", ["osip"], []) == SCOPE_RUSSIA


def test_scope_of_other_observer():
    assert scope_of("alfred", ["osip"], []) == SCOPE_GLOBAL


@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        ([], None),
        ([LEVEL_OK], LEVEL_OK),
        ([LEVEL_DOWN, LEVEL_OK], LEVEL_OK),
        ([LEVEL_PARTIAL, LEVEL_OK], LEVEL_OK),
        ([LEVEL_DOWN, LEVEL_DOWN], LEVEL_DOWN),
        ([LEVEL_PARTIAL, LEVEL_DOWN], LEVEL_PARTIAL),
        ([LEVEL_PARTIAL], LEVEL_PARTIAL),
    ],
)
def test_scope_level(levels, expected):
    assert scope_level(levels) == expected


def test_scope_levels_groups_by_scope():
    links = [
        ("alfred", "jeeves", LEVEL_OK),
        ("wooster", "jeeves", LEVEL_DOWN),
        ("osip", "jeeves", LEVEL_DOWN),
        ("osip", "wooster", LEVEL_PARTIAL),
        ("zakhar", "jeeves", LEVEL_OK),
        ("zakhar", "wooster", LEVEL_OK),
    ]
    assert scope_levels(links, ["osip"], ["zakhar"]) == {
        SCOPE_GLOBAL: {"jeeves": LEVEL_OK},
        SCOPE_RUSSIA: {"jeeves": LEVEL_DOWN, "wooster": LEVEL_PARTIAL},
    }


def test_scope_levels_empty():
    assert scope_levels([], [], []) == {SCOPE_GLOBAL: {}, SCOPE_RUSSIA: {}}


def test_resource_bad_target_needs_two_working_links():
    link_a, link_b, link_c = ("a", "s", "awg"), ("b", "s", "awg"), ("c", "s", "awg")
    rows = [
        (link_a, "one", True),
        (link_a, "two", False),
        (link_b, "one", True),
        (link_b, "two", False),
        (link_c, "one", False),
        (link_c, "two", False),
    ]
    assert vpn_link.resource_bad_targets(rows) == {"two"}
    assert vpn_link.resource_bad_targets(rows[:2] + rows[4:]) == set()
    assert vpn_link.resource_bad_targets([*rows[:3], (link_b, "two", True)]) == set()


def test_effective_scopes_put_outer_layers_over_vpn():
    vpn = {"global": {"jeeves": "ok", "wooster": "partial"}, "russia": {"jeeves": "ok"}}
    net = {"global": {"jeeves": "ok"}, "russia": {"jeeves": "down", "wooster": "down"}}
    assert vpn_link.effective_scopes(vpn, net, {"wooster": "down"}) == {
        "global": {"jeeves": "ok", "wooster": "down"},
        "russia": {"jeeves": "down", "wooster": "down"},
    }


def test_layer_table_lists_three_layers_per_server():
    table = vpn_link.layer_table(
        {"global": {"jeeves": "ok"}, "russia": {}},
        {"global": {}, "russia": {"jeeves": "down"}},
        {"jeeves": "ok"},
    )
    assert table["global"]["jeeves"] == {"net": None, "node": "ok", "vpn": "ok"}
    assert table["russia"]["jeeves"] == {"net": "down", "node": "ok", "vpn": None}
