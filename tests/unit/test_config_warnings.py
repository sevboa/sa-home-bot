"""Опечатки в конфиге не молчат: unknown_config_keys + warning в load()."""

import logging

from sa_home_bot.config import NodeConfig, Settings, unknown_config_keys


def test_node_assignments_default_empty():
    # Назначения только явные: опечатка в имени поля не должна тихо
    # включать «дефолтный набор служб».
    assert NodeConfig().assignments == []


def test_unknown_keys_found_at_all_levels():
    raw = {
        "node": {"id": "x", "assigments": []},          # опечатка во вложенном
        "swarn": {"token": "t"},                        # опечатка в секции
        "apps": {
            "socket": "./a.sock",
            "items": [{"id": "a", "title": "A", "unit": "a.service", "urls": [], "ulr": "x"}],
        },                                              # опечатка в таблице списка
        "logging": {"level": "INFO"},                   # валидное — не трогаем
    }
    unknown = unknown_config_keys(raw, Settings)
    assert unknown == ["node.assigments", "swarn", "apps.items[0].ulr"]


def test_load_warns_about_unknown_keys(tmp_path, caplog):
    config = tmp_path / "config.toml"
    config.write_text('[node]\nassigments = []\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sa_home_bot.config"):
        settings = Settings.load(config)
    assert settings.node.assignments == []  # дефолт, а не опечатка
    assert any("node.assigments" in r.message for r in caplog.records)


def test_load_is_quiet_on_valid_config(tmp_path, caplog):
    config = tmp_path / "config.toml"
    config.write_text('[node]\nassignments = ["monitor"]\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sa_home_bot.config"):
        settings = Settings.load(config)
    assert settings.node.assignments == ["monitor"]
    assert not caplog.records
