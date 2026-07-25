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


# --- persona_prompt из отдельного llm-prompt.toml (живая находка
# 2026-07-25: текст персонажа убран из репозитория — слишком личный/объёмный
# для config.toml, живёт рядом отдельным gitignored файлом, см.
# config.py::_load_persona_prompt) ---


def test_load_reads_persona_prompt_from_sibling_file(tmp_path, caplog):
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    (tmp_path / "llm-prompt.toml").write_text(
        'persona_prompt = "Ты — тестовый персонаж."\n', encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="sa_home_bot.config"):
        settings = Settings.load(config)
    assert settings.llm.persona_prompt == "Ты — тестовый персонаж."
    assert not caplog.records


def test_load_leaves_persona_prompt_empty_without_sibling_file(tmp_path, caplog):
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sa_home_bot.config"):
        settings = Settings.load(config)
    assert settings.llm.persona_prompt == ""
    assert not caplog.records  # файла нет вовсе — не опечатка, тихо пропускаем


def test_load_warns_on_empty_persona_prompt_key(tmp_path, caplog):
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    (tmp_path / "llm-prompt.toml").write_text('persona_prompt = "   "\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sa_home_bot.config"):
        settings = Settings.load(config)
    assert settings.llm.persona_prompt == ""
    assert any("persona_prompt" in r.message for r in caplog.records)


def test_load_inline_persona_prompt_still_works_without_sibling_file(tmp_path, caplog):
    # Поле в config.toml остаётся рабочим само по себе (см. докстринг
    # LlmConfig.persona_prompt) — llm-prompt.toml лишь удобный отдельный файл.
    config = tmp_path / "config.toml"
    config.write_text('[llm]\npersona_prompt = "Инлайновый текст"\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sa_home_bot.config"):
        settings = Settings.load(config)
    assert settings.llm.persona_prompt == "Инлайновый текст"
    assert not caplog.records
