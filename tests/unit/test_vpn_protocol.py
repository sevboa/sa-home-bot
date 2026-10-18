from __future__ import annotations

import pytest

from sa_home_bot.vpn import protocol as vpn_protocol

NL = "🇳🇱 Нидерланды"
US = "🇺🇸 США"


@pytest.mark.parametrize(
    ("location", "code", "flag"),
    [(NL, "NL", "🇳🇱"), (US, "US", "🇺🇸"), ("Нидерланды", "", ""), ("", "", "")],
)
def test_country_code_and_flag_come_from_flag_emoji(location, code, flag):
    assert vpn_protocol.country_code(location) == code
    assert vpn_protocol.country_flag(location) == flag


def test_awg_filename_carries_type_and_country():
    name = vpn_protocol.secret_filename(vpn_protocol.TRANSPORT_AWG, "Rose", NL)
    assert name.startswith("awg_nl_rose_")
    assert name.endswith(".conf")


def test_reality_filename_carries_type_and_country():
    assert (
        vpn_protocol.secret_filename(vpn_protocol.TRANSPORT_REALITY, "Orchid", US)
        == "vls_us_orchid.json"
    )


def test_awg_tunnel_name_fits_wireguard_limit():
    for label in ("Rose", "Orchid", "Dahlia", "Chrysanthemum"):
        for location in (NL, US, ""):
            base = vpn_protocol.secret_filename(
                vpn_protocol.TRANSPORT_AWG, label, location
            ).removesuffix(".conf")
            assert len(base) <= 15, (label, location, base)


def test_same_device_on_two_servers_gets_distinct_names():
    nl = vpn_protocol.secret_filename(vpn_protocol.TRANSPORT_REALITY, "Rose", NL)
    us = vpn_protocol.secret_filename(vpn_protocol.TRANSPORT_REALITY, "Rose", US)
    assert nl != us


def test_filename_without_location_keeps_type_only():
    name = vpn_protocol.secret_filename(vpn_protocol.TRANSPORT_REALITY, "Rose")
    assert name == "vls_rose.json"
