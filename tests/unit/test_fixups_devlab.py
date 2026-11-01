import pytest

from sa_home_bot.node.fixups import FixupError, build_fixups, devlab
from sa_home_bot.node.fixups.privileged import SUDOERS_DIR
from tests.unit.fixups_world import FakeRoot, make_settings

MYCRAFT_SUBIDS = "sevboa:100000:65536\ndavidr:165536:65536\n"
DEVLAB_IDS = {
    "install-podman",
    "devlab-user",
    "devlab-subids",
    "devlab-linger",
    "devlab-cpu-weight",
    "devlab-sudoers",
}


@pytest.fixture
def root(monkeypatch):
    return FakeRoot(monkeypatch)


@pytest.fixture
def settings():
    return make_settings(["devlab"])


@pytest.mark.parametrize(
    ("assignments", "expected"),
    [(["devlab"], DEVLAB_IDS), (["devlab_models"], DEVLAB_IDS), (["llm"], set())],
)
def test_devlab_fixups_follow_assignments(root, assignments, expected):
    ids = {f.id for f in build_fixups(make_settings(assignments))}
    assert ids & DEVLAB_IDS == expected


def test_subid_parsing_on_mycraft_content():
    assert devlab.subid_owners(MYCRAFT_SUBIDS) == {"sevboa", "davidr"}
    assert devlab.next_free_subid(MYCRAFT_SUBIDS, "") == 231072
    assert devlab.next_free_subid("", "") == devlab.SUBID_FIRST


@pytest.mark.parametrize(
    ("subuid", "subgid", "ok"),
    [
        (MYCRAFT_SUBIDS, MYCRAFT_SUBIDS, False),
        (MYCRAFT_SUBIDS + "devlab:231072:65536\n", MYCRAFT_SUBIDS, False),
        (MYCRAFT_SUBIDS + "devlab:231072:65536\n", MYCRAFT_SUBIDS + "devlab:231072:65536\n", True),
    ],
)
def test_subids_check_needs_both_files(root, settings, subuid, subgid, ok):
    root.files[str(devlab.SUBUID_FILE)] = subuid
    root.files[str(devlab.SUBGID_FILE)] = subgid
    assert devlab.make_devlab_subids_fixup(settings).check() is ok


def test_subids_apply_adds_range_after_highest_taken(root, settings):
    root.files[str(devlab.SUBUID_FILE)] = MYCRAFT_SUBIDS
    root.files[str(devlab.SUBGID_FILE)] = MYCRAFT_SUBIDS
    devlab.make_devlab_subids_fixup(settings).apply()
    span = "231072-296607"
    assert root.sudo_commands("usermod") == [
        ["usermod", "--add-subuids", span, "--add-subgids", span, "devlab"]
    ]


def test_subids_apply_refuses_without_user(root, settings):
    root.outputs["getent passwd devlab"] = None
    with pytest.raises(FixupError):
        devlab.make_devlab_subids_fixup(settings).apply()


def test_user_fixup_creates_system_user_with_home_under_root(root, settings):
    fixup = devlab.make_devlab_user_fixup(settings)
    root.outputs["getent passwd devlab"] = None
    assert fixup.check() is False
    fixup.apply()
    assert root.sudo_commands("install") == [
        ["install", "-d", "-m", "0755", "/models/sa-home/devlab"]
    ]
    assert root.sudo_commands("useradd") == [
        [
            "useradd",
            "--system",
            "--user-group",
            "--create-home",
            "--home-dir",
            "/models/sa-home/devlab/home",
            "--shell",
            "/usr/sbin/nologin",
            "devlab",
        ]
    ]


def test_linger_check_reads_linger_dir(root, settings):
    fixup = devlab.make_devlab_linger_fixup(settings)
    assert fixup.check() is False
    fixup.apply()
    assert root.sudo_commands("loginctl") == [["loginctl", "enable-linger", "devlab"]]
    root.files["/var/lib/systemd/linger/devlab"] = ""
    assert fixup.check() is True


def test_sudoers_fixup_installs_exact_wrapper_and_snippet(root, settings, monkeypatch):
    monkeypatch.setattr(devlab, "getuser", lambda: "sevboa")
    fixup = devlab.make_devlab_sudoers_fixup(settings)
    assert fixup.check() is False
    fixup.apply()
    assert fixup.check() is True
    wrapper = root.installed[str(devlab.DEVLAB_WRAPPER_PATH)]
    assert wrapper.startswith("#!/bin/sh\nset -eu\n")
    assert "export HOME=/models/sa-home/devlab/home\n" in wrapper
    assert "--network none" in wrapper
    assert root.installed[str(SUDOERS_DIR / devlab.DEVLAB_SUDOERS_FILE)] == (
        "sevboa ALL=(devlab) NOPASSWD: /usr/local/libexec/sa-home-devlab *\n"
    )


@pytest.mark.parametrize(("missing", "ok"), [(set(), True), ({"podman"}, False)])
def test_podman_check_needs_podman_and_newuidmap(root, missing, ok):
    root.missing_tools = missing
    assert devlab.INSTALL_PODMAN.check() is ok


def test_cpu_weight_fixup_writes_dropin_for_the_devlab_user_slice(root, settings):
    root.outputs["id -u devlab"] = "991\n"
    fixup = devlab.make_devlab_cpu_weight_fixup(settings)
    dropin = "/etc/systemd/system/user-991.slice.d/50-sa-home-devlab.conf"
    assert fixup.check() is False
    fixup.apply()
    assert root.installed[dropin] == "[Slice]\nCPUWeight=10\n"
    assert root.sudo_commands("systemctl") == [["systemctl", "daemon-reload"]]
    assert fixup.check() is True
    root.files[dropin] = "[Slice]\nCPUWeight=100\n"
    assert fixup.check() is False


def test_cpu_weight_fixup_needs_the_user(root, settings):
    root.outputs["id -u devlab"] = None
    fixup = devlab.make_devlab_cpu_weight_fixup(settings)
    assert fixup.check() is False
    with pytest.raises(FixupError):
        fixup.apply()


def test_wrapper_content_has_pause_subcommands(settings):
    content = devlab.devlab_wrapper_content(settings)
    assert "pause|unpause)" in content and "interrupt)" in content
