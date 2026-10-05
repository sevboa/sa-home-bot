"""Тесты sealed box для бэкапов (backup/sealed.py, backup_cli.py)."""

from __future__ import annotations

import argparse
import stat

import pytest

from sa_home_bot.backup import sealed
from sa_home_bot.backup.sealed import SealedError
from sa_home_bot.backup_cli import add_backup_subparser
from sa_home_bot.config import Settings


def test_round_trip():
    priv, pub = sealed.generate_keypair()
    for data in (b"", b"secret key", bytes(range(256)) * 100):
        blob = sealed.seal(pub, data)
        assert blob.startswith(sealed.MAGIC)
        assert sealed.open_sealed(priv, blob) == data


def test_each_seal_differs():
    _, pub = sealed.generate_keypair()
    assert sealed.seal(pub, b"x") != sealed.seal(pub, b"x")


def test_wrong_key():
    _, pub = sealed.generate_keypair()
    other_priv, _ = sealed.generate_keypair()
    with pytest.raises(SealedError):
        sealed.open_sealed(other_priv, sealed.seal(pub, b"x"))


def test_corruption_every_region():
    priv, pub = sealed.generate_keypair()
    blob = sealed.seal(pub, b"payload")
    for i in (len(sealed.MAGIC) + 1, len(sealed.MAGIC) + 33, len(blob) - 1):
        bad = bytearray(blob)
        bad[i] ^= 1
        with pytest.raises(SealedError):
            sealed.open_sealed(priv, bytes(bad))


def test_bad_magic_version_truncated():
    priv, pub = sealed.generate_keypair()
    blob = sealed.seal(pub, b"x")
    with pytest.raises(SealedError, match="магия"):
        sealed.open_sealed(priv, b"XXXXX" + blob[5:])
    with pytest.raises(SealedError, match="версия"):
        sealed.open_sealed(priv, b"SAHB2" + blob[5:])
    with pytest.raises(SealedError):
        sealed.open_sealed(priv, blob[:20])
    with pytest.raises(SealedError):
        sealed.open_sealed(priv, b"")


def test_key_dump_load():
    priv, pub = sealed.generate_keypair()
    assert sealed.load_key(sealed.dump_key(pub) + "\n") == pub
    assert sealed.public_from_private(priv) == pub
    for bad in ("", "!!!", sealed.dump_key(pub)[:-4]):
        with pytest.raises(SealedError):
            sealed.load_key(bad)
    with pytest.raises(SealedError):
        sealed.dump_key(b"short")
    with pytest.raises(SealedError):
        sealed.seal(b"short", b"x")


def test_private_key_file(tmp_path):
    priv, _ = sealed.generate_keypair()
    path = tmp_path / "sub" / "k"
    sealed.write_private_key(path, priv)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sealed.read_private_key(path) == priv
    with pytest.raises(FileExistsError):
        sealed.write_private_key(path, priv)
    with pytest.raises(SealedError):
        sealed.read_private_key(tmp_path / "missing")


def test_keygen_cli(tmp_path, capsys):
    parser = argparse.ArgumentParser()
    add_backup_subparser(parser.add_subparsers(dest="command"))
    out = tmp_path / "backup.key"
    args = parser.parse_args(["backup", "keygen", "--out", str(out)])
    assert args._run(args) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    priv = sealed.read_private_key(out)
    printed = capsys.readouterr().out
    assert sealed.dump_key(sealed.public_from_private(priv)) in printed
    assert sealed.dump_key(priv) not in printed
    assert args._run(args) == 1  # не затирает


def test_config_defaults_and_section(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[telegram]\ntoken = "x"\n')
    s = Settings.load(cfg)
    assert s.backup.recipient_public_key == "" and s.backup.private_key_file == ""
    cfg.write_text('[backup]\nrecipient_public_key = "abc"\n')
    assert Settings.load(cfg).backup.recipient_public_key == "abc"
