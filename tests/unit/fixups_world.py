from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from sa_home_bot.config import AppConfig, AppsConfig, NodeConfig, Settings, TelegramConfig
from sa_home_bot.node import vpn_probe_state

ROOT_PREFIXES = ("/etc/", "/usr/", "/proc/", "/sbin/", "/bin/", "/var/")


def make_settings(
    assignments: list[str], apps: list[AppConfig] | None = None, kind: str = "server"
) -> Settings:
    return Settings(
        telegram=TelegramConfig(token="x"),
        subscriptions=[],
        node=NodeConfig(assignments=assignments, kind=kind),
        apps=AppsConfig(items=apps or []),
    )


def make_slot(**overrides) -> vpn_probe_state.ProbeSlot:
    base = dict(
        server="jeeves",
        transport="awg",
        netns="vpn-probe-jeeves-awg",
        veth_host="vprobe0h0",
        veth_ns="vprobe0n0",
        veth_host_addr="10.200.200.1/30",
        veth_ns_addr="10.200.200.2/30",
        subnet="10.200.200.0/30",
        iface="awg-probe0",
    )
    base.update(overrides)
    return vpn_probe_state.ProbeSlot(**base)


def make_reality_slot() -> vpn_probe_state.ProbeSlot:
    return make_slot(
        server="wooster",
        transport="reality",
        netns="vpn-probe-wooster-reality",
        veth_host="vprobe1h0",
        veth_ns="vprobe1n0",
        veth_host_addr="10.200.200.5/30",
        veth_ns_addr="10.200.200.6/30",
        subnet="10.200.200.4/30",
        iface=None,
        socks_port=11081,
    )


class FakeRoot:
    def __init__(self, monkeypatch):
        self.files: dict[str, str] = {}
        self.dirs: set[str] = set()
        self.missing_tools: set[str] = set()
        self.active_units: set[str] = set()
        self.outputs: dict[str, str | None] = {}
        self.calls: list[list[str]] = []
        self.installed: dict[str, str] = {}
        real = {
            "exists": Path.exists,
            "is_file": Path.is_file,
            "is_dir": Path.is_dir,
            "read_text": Path.read_text,
        }
        monkeypatch.setattr(subprocess, "run", self._run)
        monkeypatch.setattr(shutil, "which", self._which)
        monkeypatch.setattr(
            Path, "exists", lambda p: self._in_root(p, lambda: self._known(p), real["exists"])
        )
        monkeypatch.setattr(
            Path,
            "is_file",
            lambda p: self._in_root(p, lambda: str(p) in self.files, real["is_file"]),
        )
        monkeypatch.setattr(
            Path, "is_dir", lambda p: self._in_root(p, lambda: str(p) in self.dirs, real["is_dir"])
        )
        monkeypatch.setattr(Path, "read_text", self._read_text(real["read_text"]))

    def _in_root(self, path, fake, real):
        return fake() if str(path).startswith(ROOT_PREFIXES) else real(path)

    def _known(self, path) -> bool:
        return str(path) in self.files or str(path) in self.dirs

    def _read_text(self, real):
        def read(path, *args, **kwargs):
            if not str(path).startswith(ROOT_PREFIXES):
                return real(path, *args, **kwargs)
            if str(path) not in self.files:
                raise FileNotFoundError(str(path))
            return self.files[str(path)]

        return read

    def _which(self, name, mode=None, path=None):
        return None if name in self.missing_tools else f"/usr/bin/{name}"

    def _run(self, argv, **kwargs):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        stripped = self.strip_sudo(argv)
        key = " ".join(stripped)
        if key in self.outputs:
            output = self.outputs[key]
            return SimpleNamespace(
                returncode=0 if output is not None else 1, stdout=output or "", stderr=""
            )
        handler = getattr(self, f"_cmd_{stripped[0].rsplit('/', 1)[-1]}", None)
        if handler is not None:
            return handler(stripped)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def _cmd_install(self, argv):
        if "-d" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        source, dest = argv[-2], argv[-1]
        self.installed[dest] = Path(source).read_text()
        self.files[dest] = self.installed[dest]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def _cmd_cat(self, argv):
        content = self.files.get(argv[1])
        return SimpleNamespace(
            returncode=0 if content is not None else 1, stdout=content or "", stderr=""
        )

    def _cmd_test(self, argv):
        return SimpleNamespace(returncode=0 if self._known(argv[2]) else 1, stdout="", stderr="")

    def _cmd_systemctl(self, argv):
        if "is-active" in argv:
            return SimpleNamespace(
                returncode=0 if argv[-1] in self.active_units else 3, stdout="", stderr=""
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @staticmethod
    def strip_sudo(argv: list[str]) -> list[str]:
        if argv[0] != "sudo":
            return argv
        rest = argv[1:]
        return rest[1:] if rest and rest[0] == "-n" else rest

    @property
    def sudo_calls(self) -> list[list[str]]:
        return [self.strip_sudo(c) for c in self.calls if c[0] == "sudo"]

    def sudo_commands(self, command: str) -> list[list[str]]:
        return [c for c in self.sudo_calls if c[0] == command]
