"""vpn/protocol.py: имена выдаваемых гостю файлов и код страны из [vpn].location.

Гость держит конфиги с нескольких серверов сразу, поэтому имя файла начинается
с типа настроек и страны («awg_nl_rose_123.conf») — решение владельца
2026-09-18.
"""

from __future__ import annotations

from sa_home_bot.vpn import protocol as vpn_protocol

NL = "🇳🇱 Нидерланды"
US = "🇺🇸 США"


def test_country_code_from_flag_emoji():
    assert vpn_protocol.country_code(NL) == "NL"
    assert vpn_protocol.country_code(US) == "US"


def test_country_code_empty_without_flag():
    # Нода со старым конфигом (location не задан) или без флага — не падаем,
    # просто не показываем страну.
    assert vpn_protocol.country_code("Нидерланды") == ""
    assert vpn_protocol.country_code("") == ""


def test_country_flag_extracted_for_profile_name():
    assert vpn_protocol.country_flag(NL) == "🇳🇱"
    assert vpn_protocol.country_flag("Нидерланды") == ""


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
    """Имя файла без расширения = имя тоннеля, а его wireguard-android
    валидирует по [a-zA-Z0-9_=+.-]{1,15} — длиннее приложение не примет."""
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
