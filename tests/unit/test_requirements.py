"""Requirement — проверка внешних программ и ограничений по ОС."""

import sys

from sa_home_bot.utils.requirements import Requirement, _install_command


def test_program_available(monkeypatch):
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/smartctl" if name == "smartctl" else None
    )
    req = Requirement(program="smartctl", package="smartmontools")
    assert req.available()


def test_program_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    req = Requirement(program="smartctl", package="smartmontools")
    assert not req.available()


def test_platform_mismatch_unavailable_regardless_of_program(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/whatever")
    req = Requirement(program="whatever", platforms=("win32",))
    assert not req.available()  # sys.platform у тестов не win32


def test_platform_match_is_available():
    req = Requirement(platforms=(sys.platform,))
    assert req.available()


def test_install_hint_picks_first_found_manager(monkeypatch):
    def fake_which(name):
        return "/usr/bin/pacman" if name == "pacman" else None

    monkeypatch.setattr("shutil.which", fake_which)
    assert _install_command("smartmontools") == "sudo pacman -S smartmontools"


def test_install_hint_no_manager_found_still_useful(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    hint = _install_command("smartmontools")
    assert "smartmontools" in hint


def test_install_hint_includes_program_and_note(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/apt" if name == "apt" else None)
    req = Requirement(
        program="smartctl", package="smartmontools", note="температура дисков"
    )
    hint = req.install_hint()
    assert "sudo apt install smartmontools" in hint
    assert "smartctl" in hint
    assert "температура дисков" in hint


def test_install_hint_unsupported_platform_mentions_future():
    req = Requirement(platforms=("win32",), note="служба Windows")
    hint = req.install_hint()
    assert "не поддерживается на этой ОС" in hint
    assert "win32" in hint
    assert "служба Windows" in hint
