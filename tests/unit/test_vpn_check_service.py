"""Служба vpn_check: describe, запуск проверки, пуш результата в vpn,
обработка HTTP-ошибок и сбоев самого curl."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sa_home_bot.config import Settings, VpnCheckConfig
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError
from sa_home_bot.vpn_check.service import VpnCheckService


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


class _FakeNodeLink:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append({"action": action, "args": args, "dst": dst, "timeout": timeout})
        return {"accepted": True}


def _settings(**kwargs) -> Settings:
    return Settings(vpn_check=VpnCheckConfig(probe_iface="awg-probe0", **kwargs))


def _patch_curl(monkeypatch, results: dict[str, tuple[bytes, bytes, int]]) -> None:
    """``results``: последний аргумент cmd (target url) -> (stdout, stderr, code)."""
    calls: list[tuple] = []

    async def fake_create_subprocess_exec(*cmd, stdout=None, stderr=None):
        calls.append(cmd)
        target = cmd[-1]
        out, err, code = results[target]
        return _FakeProc(out, err, code)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    return calls


def test_describe_declares_check_action():
    desc = VpnCheckService(_settings(), _FakeNodeLink()).describe()
    assert desc.info.service == "vpn_check"
    assert desc.find_action("check") is not None


async def test_check_accepts_and_returns_immediately(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    service = VpnCheckService(_settings(), _FakeNodeLink())
    result = await service.run_command("check", {"targets": ["https://1.1.1.1"]})
    assert result == {"accepted": True, "targets": ["https://1.1.1.1"]}


async def test_check_without_targets_is_bad_request():
    service = VpnCheckService(_settings(), _FakeNodeLink())
    with pytest.raises(ProtoError) as excinfo:
        await service.run_command("check", {})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_unknown_action_raises_value_error():
    with pytest.raises(ValueError):
        await VpnCheckService(_settings(), _FakeNodeLink()).run_command("fetch", {})


async def test_run_and_report_pushes_ok_result(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    service = VpnCheckService(_settings(), node_link)
    await service._run_and_report(["https://1.1.1.1"])
    assert len(node_link.calls) == 1
    call = node_link.calls[0]
    assert call["action"] == "report_check"
    assert call["args"]["node"]
    results = call["args"]["results"]
    assert results["https://1.1.1.1"]["ok"] is True
    assert results["https://1.1.1.1"]["error"] is None
    assert isinstance(results["https://1.1.1.1"]["ms"], int)


async def test_run_and_report_marks_http_error_as_failed(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"503", b"", 0)})
    node_link = _FakeNodeLink()
    service = VpnCheckService(_settings(), node_link)
    await service._run_and_report(["https://1.1.1.1"])
    results = node_link.calls[0]["args"]["results"]
    assert results["https://1.1.1.1"]["ok"] is False
    assert "503" in results["https://1.1.1.1"]["error"]


async def test_run_and_report_marks_curl_failure(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"", b"connection refused", 7)})
    node_link = _FakeNodeLink()
    service = VpnCheckService(_settings(), node_link)
    await service._run_and_report(["https://1.1.1.1"])
    results = node_link.calls[0]["args"]["results"]
    assert results["https://1.1.1.1"]["ok"] is False
    assert "connection refused" in results["https://1.1.1.1"]["error"]


async def test_check_binds_curl_to_probe_interface_not_netns(monkeypatch):
    calls = _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    service = VpnCheckService(_settings(), _FakeNodeLink())
    await service._run_and_report(["https://1.1.1.1"])
    cmd = calls[0]
    assert "--interface" in cmd
    assert cmd[cmd.index("--interface") + 1] == "awg-probe0"
    assert "netns" not in cmd  # живая находка 2026-08-17: netns без пути в интернет


async def test_run_and_report_multiple_targets(monkeypatch):
    _patch_curl(
        monkeypatch,
        {
            "https://1.1.1.1": (b"200", b"", 0),
            "https://api.telegram.org": (b"", b"timed out", 28),
        },
    )
    node_link = _FakeNodeLink()
    service = VpnCheckService(_settings(), node_link)
    await service._run_and_report(["https://1.1.1.1", "https://api.telegram.org"])
    results = node_link.calls[0]["args"]["results"]
    assert results["https://1.1.1.1"]["ok"] is True
    assert results["https://api.telegram.org"]["ok"] is False
