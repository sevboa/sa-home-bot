"""Requirement — проверка внешних программ, ограничений по ОС и прав."""

import sys

import pytest

from sa_home_bot.utils.requirements import (
    Requirement,
    RequirementRegistry,
    RequirementStatus,
    _install_command,
    install_argv,
    looks_like_permission_error,
)


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


# --- diagnose(): типизированный аналог available() ---


def test_diagnose_ok(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/smartctl")
    req = Requirement(program="smartctl", package="smartmontools")
    assert req.diagnose() is RequirementStatus.OK


def test_diagnose_missing_program(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    req = Requirement(program="smartctl", package="smartmontools")
    assert req.diagnose() is RequirementStatus.MISSING_PROGRAM


def test_diagnose_unsupported_platform(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/whatever")
    req = Requirement(program="whatever", platforms=("win32",))
    assert req.diagnose() is RequirementStatus.UNSUPPORTED_PLATFORM


# --- looks_like_permission_error(): классификация stderr ---


@pytest.mark.parametrize(
    "stderr",
    [
        "Permission denied",
        "smartctl: Operation not permitted",
        "You must be root to run this program",
        "open device: /dev/sda failed: Permission denied",
        "PATH ERROR: Access is denied.",
    ],
)
def test_looks_like_permission_error_true(stderr):
    assert looks_like_permission_error(stderr)


@pytest.mark.parametrize(
    "stderr",
    ["", "smartctl: command not found", "No such file or directory", "timeout"],
)
def test_looks_like_permission_error_false(stderr):
    assert not looks_like_permission_error(stderr)


# --- install_argv(): argv для реальной установки (nodectl fix) ---


def test_install_argv_picks_found_manager(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/apt" if name == "apt" else None)
    assert install_argv("smartmontools") == ["apt-get", "install", "-y", "smartmontools"]


def test_install_argv_none_when_no_manager_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert install_argv("smartmontools") is None


# --- RequirementRegistry: живой реестр диагнозов ---


def test_registry_status_for_prefers_live_static_over_stale_registry(monkeypatch):
    registry = RequirementRegistry()
    req = Requirement(program="smartctl", package="smartmontools")
    registry.report(req, RequirementStatus.NEEDS_PRIVILEGE)
    # Программа реально отсутствует прямо сейчас — статика важнее устаревшего
    # NEEDS_PRIVILEGE в реестре (например, программу снесли между вызовами).
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert registry.status_for(req) is RequirementStatus.MISSING_PROGRAM


def test_registry_status_for_surfaces_needs_privilege_when_program_present(monkeypatch):
    registry = RequirementRegistry()
    req = Requirement(program="smartctl", package="smartmontools")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/sbin/smartctl")
    assert registry.status_for(req) is RequirementStatus.OK
    registry.report(req, RequirementStatus.NEEDS_PRIVILEGE)
    assert registry.status_for(req) is RequirementStatus.NEEDS_PRIVILEGE


def test_registry_problem_for_none_when_ok(monkeypatch):
    registry = RequirementRegistry()
    req = Requirement(program="smartctl", package="smartmontools")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/sbin/smartctl")
    assert registry.problem_for(req) is None


def test_registry_problem_for_needs_privilege_uses_privilege_hint(monkeypatch):
    registry = RequirementRegistry()
    req = Requirement(program="smartctl", package="smartmontools", note="температура дисков")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/sbin/smartctl")
    registry.report(req, RequirementStatus.NEEDS_PRIVILEGE)
    problem = registry.problem_for(req)
    assert problem == {
        "id": "smartctl",
        "status": "needs_privilege",
        "hint": req.privilege_hint(),
    }
    assert "nodectl fix" in problem["hint"]


def test_registry_reset_clears_entries(monkeypatch):
    registry = RequirementRegistry()
    req = Requirement(program="smartctl", package="smartmontools")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/sbin/smartctl")
    registry.report(req, RequirementStatus.NEEDS_PRIVILEGE)
    registry.reset()
    assert registry.status_for(req) is RequirementStatus.OK
