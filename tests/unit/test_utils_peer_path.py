from __future__ import annotations

import pytest

from sa_home_bot.utils.peer_path import format_peer_path


@pytest.mark.parametrize(
    ("peer", "expected"),
    [
        ({"id": "a", "alive": True}, ""),
        ({"rtt_ms": None, "loss": 0.0, "via": None, "via_rtt_ms": None}, ""),
        ({"rtt_ms": 12.6, "loss": 0.0}, "13 мс"),
        ({"rtt_ms": 254.4, "loss": 0.13}, "254 мс, потери 13%"),
        (
            {"rtt_ms": 254.0, "loss": 0.13, "via": "jeeves", "via_rtt_ms": 185.4},
            "254 мс, потери 13% · через jeeves ~185 мс",
        ),
        ({"rtt_ms": 90.0, "via": "jeeves"}, "90 мс · через jeeves"),
        ({"rtt_ms": None, "via": "jeeves", "via_rtt_ms": 70.0}, "через jeeves ~70 мс"),
    ],
)
def test_format_peer_path(peer, expected):
    assert format_peer_path(peer) == expected
