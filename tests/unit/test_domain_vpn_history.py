from __future__ import annotations

from datetime import UTC, datetime

from sa_home_bot.domain.vpn_history import Sample, aggregate, bucket_start, median_ms

FIRST = datetime(2026, 11, 12, 12, 7, 30, tzinfo=UTC)
SECOND = datetime(2026, 11, 12, 12, 20, 0, tzinfo=UTC)
FIRST_BUCKET = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)
SECOND_BUCKET = datetime(2026, 11, 12, 12, 15, 0, tzinfo=UTC)


def test_bucket_start_rounds_down_to_interval():
    assert bucket_start(FIRST, 900) == FIRST_BUCKET


def test_bucket_start_does_not_move_on_boundary():
    assert bucket_start(SECOND_BUCKET, 900) == SECOND_BUCKET


def test_median_of_empty_is_none():
    assert median_ms([]) is None


def test_median_of_two_values():
    assert median_ms([100, 300]) == 200


def test_median_of_three_values():
    assert median_ms([100, 200, 400]) == 200


def test_aggregate_groups_by_link_and_bucket():
    samples = [
        Sample(FIRST, "alfred", "jeeves", "awg", 1, 0, 100),
        Sample(SECOND, "alfred", "jeeves", "awg", 2, 1, 300),
        Sample(SECOND, "alfred", "jeeves", "awg", 1, 1, None),
        Sample(FIRST, "jeeves", "wooster", "reality", 2, 0, 50),
    ]
    series = aggregate(samples, 900)
    assert [(entry["server"], entry["transport"], entry["node"]) for entry in series] == [
        ("jeeves", "awg", "alfred"),
        ("wooster", "reality", "jeeves"),
    ]
    assert series[0]["node"] == "alfred"
    assert series[0]["server"] == "jeeves"
    assert series[0]["transport"] == "awg"
    points = series[0]["points"]
    assert [point["at"] for point in points] == [
        FIRST_BUCKET.isoformat(),
        SECOND_BUCKET.isoformat(),
    ]
    assert points[0] == {
        "at": FIRST_BUCKET.isoformat(),
        "reports": 1,
        "down": 0,
        "partial": 0,
        "median_ms": 100,
    }
    assert points[1] == {
        "at": SECOND_BUCKET.isoformat(),
        "reports": 2,
        "down": 1,
        "partial": 1,
        "median_ms": 300,
    }
    assert series[1]["points"][0]["median_ms"] == 50


def test_aggregate_of_empty_input_is_empty():
    assert aggregate([], 900) == []
