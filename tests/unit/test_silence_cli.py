from __future__ import annotations

import argparse

from sa_home_bot.bot.away import AwayService
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from tests.unit.test_away import _settings


def _cli_args(*argv):
    from sa_home_bot.silence_cli import add_silent_subparser

    parser = argparse.ArgumentParser()
    add_silent_subparser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["silent", *argv])


async def _prepare(tmp_path):
    from sa_home_bot.silence_cli import _run

    settings = _settings(tmp_path)
    db = Database(settings.database.path)
    await db.open()
    await apply_migrations(db)
    await db.close()
    return _run, settings


async def test_silence_set_status_off_and_notices(tmp_path, capsys):
    run, settings = await _prepare(tmp_path)

    assert await run(_cli_args("set", "30"), settings) == 0
    assert "🔇 Тишина видеокарты" in capsys.readouterr().out
    assert await run(_cli_args("status"), settings) == 0
    assert "🔇 Тишина видеокарты до" in capsys.readouterr().out
    assert await run(_cli_args("off"), settings) == 0
    assert "🔈 Тишина снята." in capsys.readouterr().out

    db = Database(settings.database.path)
    await db.open()
    try:
        service = AwayService(Store(db), settings)
        assert await service.load() is None
        notices = await service.take_notices()
    finally:
        await db.close()
    assert [n.split(".")[0] for n in notices] == [
        "Claude включил тишину видеокарты",
        "Claude снял тишину видеокарты",
    ]


async def test_silence_set_without_minutes_is_infinite(tmp_path, capsys):
    run, settings = await _prepare(tmp_path)

    assert await run(_cli_args("set"), settings) == 0
    assert "🔇 Тишина видеокарты" in capsys.readouterr().out
    assert await run(_cli_args("status"), settings) == 0
    out = capsys.readouterr().out
    assert "🔇 Тишина видеокарты до /silent off." in out


async def test_silence_set_zero_refuses(tmp_path, capsys):
    run, settings = await _prepare(tmp_path)

    assert await run(_cli_args("set", "0"), settings) == 1
    assert "срок тишины — от 1 минуты" in capsys.readouterr().err


async def test_silence_without_database_refuses(tmp_path, capsys):
    from sa_home_bot.silence_cli import _run

    assert await _run(_cli_args("status"), _settings(tmp_path)) == 2
    assert not (tmp_path / "bot.sqlite").exists()


async def test_silence_admin_change_has_no_notice(tmp_path):
    run, settings = await _prepare(tmp_path)

    assert await run(_cli_args("set", "30", "--by", "admin"), settings) == 0
    db = Database(settings.database.path)
    await db.open()
    try:
        assert await AwayService(Store(db), settings).take_notices() == []
    finally:
        await db.close()
