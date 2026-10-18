"""Реестр служб — единственный источник знания о службе.

Тесты фиксируют то, что раньше жило в трёх местах: чем службу спавнить
(ASSIGNMENT_ARGS), как её запустить руками (--service choices) и куда к ней
стучаться (service_endpoints).
"""

from __future__ import annotations

from sa_home_bot.config import Settings
from sa_home_bot.services import registry


def test_known_services():
    assert set(registry.SERVICES) == {
        "monitor",
        "telegram-bot",
        "apps",
        "torrents",
        "tasks",
        "memory",
        "graph_memory",
        "net",
        "llm",
        "vpn",
        "vpn_check",
    }


def test_cli_args_map_assignment_name_to_service_flag():
    # Имя в рое и имя в CLI различаются только у бота — это знание живёт здесь.
    assert registry.SERVICES["telegram-bot"].cli_args == ["--service", "bot"]
    assert registry.SERVICES["monitor"].cli_args == ["--service", "monitor"]


def test_cli_choices_include_every_service_and_the_node_itself():
    choices = registry.cli_service_choices()
    for svc in registry.SERVICES.values():
        assert svc.cli_name in choices
    assert registry.NODE_CLI_NAME in choices


def test_llm_is_externally_managed_and_not_assignable():
    """Живая находка 2026-07-23: WSL2 не стартует из Session-0, поэтому llm
    поднимает не супервизор. Маршрут к ней при этом есть."""
    llm = registry.SERVICES["llm"]
    assert llm.externally_managed
    assert not llm.assignable
    assert llm.name not in registry.supervised_names()
    assert llm.name not in registry.assignable_names()
    assert llm.endpoint_attr == "llm.socket"


def test_service_endpoints_cover_services_with_own_server():
    endpoints = registry.service_endpoints(Settings())
    # Бот — клиент, своего proto-сервера у него нет: маршрутизировать нечего.
    assert "telegram-bot" not in endpoints
    assert set(endpoints) == {
        name for name, svc in registry.SERVICES.items() if svc.endpoint_attr
    }
    assert endpoints["monitor"] == Settings().monitor.socket


def test_telegram_bot_is_the_singleton_with_its_own_config_package():
    bot = registry.SERVICES["telegram-bot"]
    assert bot.singleton and bot.instanced
    assert [name for name, svc in registry.SERVICES.items() if svc.singleton] == ["telegram-bot"]
