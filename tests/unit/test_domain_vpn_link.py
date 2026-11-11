from sa_home_bot.domain.vpn_link import (
    LEVEL_DOWN,
    LEVEL_OK,
    LEVEL_PARTIAL,
    LinkState,
    link_level,
    reconcile_link,
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
