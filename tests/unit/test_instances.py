"""Пакеты настроек инстансов: ревизии, приём реплик, слой поверх config.toml."""

from __future__ import annotations

import json

import pytest

from sa_home_bot.config import Settings
from sa_home_bot.node.instances import (
    InstanceMeta,
    InstanceStore,
    content_hash,
    slot_key,
)

PACKAGE = b'[telegram]\ntoken = "111:aaa"\n'


def _store(tmp_path, node="alfred") -> InstanceStore:
    return InstanceStore(tmp_path / "instances", node)


def _write(store: InstanceStore, data: bytes = PACKAGE) -> None:
    path = store.package_path("telegram-bot", "alfred")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_slot_key():
    assert slot_key("telegram-bot", "alfred") == "telegram-bot@alfred"
    assert slot_key("monitor", "") == "monitor"


# --- локальные правки -------------------------------------------------------


def test_new_package_gets_revision_one(tmp_path):
    store = _store(tmp_path)
    _write(store)
    meta = store.refresh("telegram-bot", "alfred")
    assert meta is not None
    assert (meta.rev, meta.origin_node) == (1, "alfred")
    assert meta.hash == content_hash(PACKAGE)


def test_unchanged_package_produces_no_new_revision(tmp_path):
    store = _store(tmp_path)
    _write(store)
    store.refresh("telegram-bot", "alfred")
    assert store.refresh("telegram-bot", "alfred") is None


def test_editing_the_package_bumps_the_revision(tmp_path):
    store = _store(tmp_path)
    _write(store)
    store.refresh("telegram-bot", "alfred")
    _write(store, PACKAGE + '\n[weather]\ncity = "Бухарест"\n'.encode())
    meta = store.refresh("telegram-bot", "alfred")
    assert meta is not None and meta.rev == 2


def test_missing_package_is_not_an_error(tmp_path):
    assert _store(tmp_path).refresh("telegram-bot", "nope") is None


def test_refresh_all_finds_every_package(tmp_path):
    store = _store(tmp_path)
    _write(store)
    other = store.package_path("telegram-bot", "work")
    other.write_bytes(b'[telegram]\ntoken = "222:bbb"\n')
    changed = {m.instance for m in store.refresh_all()}
    assert changed == {"alfred", "work"}
    assert set(store.instances_of("telegram-bot")) == {"alfred", "work"}


def test_corrupt_sidecar_is_treated_as_missing(tmp_path):
    store = _store(tmp_path)
    _write(store)
    store.refresh("telegram-bot", "alfred")
    store.meta_path("telegram-bot", "alfred").write_text("{не json")
    assert store.read_meta("telegram-bot", "alfred") is None
    # Пакет цел — просто заново получает ревизию, а не теряется.
    assert store.refresh("telegram-bot", "alfred").rev == 1


# --- приём реплики ----------------------------------------------------------


def _meta(rev: int, data: bytes = PACKAGE, *, node="alfred", at="2026-07-26T10:00:00+00:00"):
    return InstanceMeta(
        service="telegram-bot",
        instance="alfred",
        rev=rev,
        hash=content_hash(data),
        updated_at=at,
        origin_node=node,
    )


def test_applying_a_newer_revision_writes_the_package(tmp_path):
    store = _store(tmp_path, node="jeeves")
    assert store.apply(_meta(5), PACKAGE) is True
    assert store.read_package("telegram-bot", "alfred") == PACKAGE
    assert store.read_meta("telegram-bot", "alfred").rev == 5


def test_older_revision_is_ignored(tmp_path):
    store = _store(tmp_path, node="jeeves")
    store.apply(_meta(5), PACKAGE)
    newer = b'[telegram]\ntoken = "999:zzz"\n'
    assert store.apply(_meta(4, newer), newer) is False
    assert store.read_package("telegram-bot", "alfred") == PACKAGE


def test_same_revision_same_content_changes_nothing(tmp_path):
    store = _store(tmp_path, node="jeeves")
    store.apply(_meta(5), PACKAGE)
    assert store.apply(_meta(5), PACKAGE) is False


def test_hash_mismatch_is_rejected(tmp_path):
    """Лучше остаться на прежних настройках, чем поднять службу на битых."""
    store = _store(tmp_path, node="jeeves")
    assert store.apply(_meta(5), b"other content entirely") is False
    assert store.read_package("telegram-bot", "alfred") is None


def test_split_edit_on_the_same_revision_resolves_deterministically(tmp_path):
    """Пакет правили на двух нодах, пока они не видели друг друга. Спорить
    не о чем — важно лишь, чтобы все выбрали одно и то же."""
    mine = b'[telegram]\ntoken = "111:aaa"\n'
    theirs = b'[telegram]\ntoken = "222:bbb"\n'
    later = _meta(3, theirs, node="winpc", at="2026-07-26T12:00:00+00:00")
    earlier = _meta(3, mine, node="alfred", at="2026-07-26T10:00:00+00:00")

    a = _store(tmp_path / "a", node="a")
    a.apply(earlier, mine)
    assert a.apply(later, theirs) is True

    b = _store(tmp_path / "b", node="b")
    b.apply(later, theirs)
    assert b.apply(earlier, mine) is False

    # Обе ноды пришли к одному содержимому, независимо от порядка получения.
    assert a.read_package("telegram-bot", "alfred") == b.read_package("telegram-bot", "alfred")


def test_meta_survives_as_plain_json(tmp_path):
    store = _store(tmp_path)
    _write(store)
    store.refresh("telegram-bot", "alfred")
    data = json.loads(store.meta_path("telegram-bot", "alfred").read_text(encoding="utf-8"))
    assert data["service"] == "telegram-bot"
    assert data["instance"] == "alfred"


def test_replication_preserves_comments_and_formatting(tmp_path):
    """Реплицируются байты файла, а не разобранная структура: владелец
    открывает пакет на резервной ноде и видит ровно то, что писал."""
    commented = '# токен рабочего бота\n[telegram]\ntoken   =   "111:aaa"   # не трогать\n'.encode()
    store = _store(tmp_path, node="jeeves")
    store.apply(_meta(1, commented), commented)
    assert store.read_package("telegram-bot", "alfred") == commented


# --- слой поверх config.toml ------------------------------------------------


def _config(tmp_path, body: str) -> str:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_package_supplies_settings(tmp_path):
    config = _config(tmp_path, '[database]\npath = "./x.sqlite"\n')
    pkg = tmp_path / "instances" / "telegram-bot.alfred.toml"
    pkg.parent.mkdir()
    pkg.write_text('[telegram]\ntoken = "111:aaa"\n[weather]\ncity = "Бухарест"\n')

    settings = Settings.load(config, instance="alfred")
    assert settings.telegram.token == "111:aaa"
    assert settings.weather.city == "Бухарест"
    assert str(settings.database.path).endswith("x.sqlite")  # общий конфиг на месте


def test_package_wins_over_the_general_config(tmp_path):
    """Оставшаяся в config.toml копия — след прошлой раскладки; она не должна
    переигрывать то, что рой только что синхронизировал."""
    config = _config(tmp_path, '[telegram]\ntoken = "old:token"\n')
    pkg = tmp_path / "instances" / "telegram-bot.alfred.toml"
    pkg.parent.mkdir()
    pkg.write_text('[telegram]\ntoken = "new:token"\n')

    assert Settings.load(config, instance="alfred").telegram.token == "new:token"


def test_env_still_wins_over_the_package(tmp_path, monkeypatch):
    config = _config(tmp_path, "")
    pkg = tmp_path / "instances" / "telegram-bot.alfred.toml"
    pkg.parent.mkdir()
    pkg.write_text('[telegram]\ntoken = "from:package"\n')
    monkeypatch.setenv("SENTINEL__TELEGRAM__TOKEN", "from:env")

    assert Settings.load(config, instance="alfred").telegram.token == "from:env"


def test_shadowed_sections_are_reported(tmp_path, caplog):
    config = _config(tmp_path, '[telegram]\ntoken = "old:token"\n')
    pkg = tmp_path / "instances" / "telegram-bot.alfred.toml"
    pkg.parent.mkdir()
    pkg.write_text('[telegram]\ntoken = "new:token"\n')

    with caplog.at_level("WARNING"):
        Settings.load(config, instance="alfred")
    assert any("[telegram]" in r.getMessage() for r in caplog.records)


def test_missing_package_is_a_clear_error(tmp_path):
    config = _config(tmp_path, "")
    with pytest.raises(FileNotFoundError, match="Пакет настроек инстанса"):
        Settings.load(config, instance="nope")


def test_without_instance_nothing_changes(tmp_path):
    config = _config(tmp_path, '[telegram]\ntoken = "plain"\n')
    assert Settings.load(config).telegram.token == "plain"
