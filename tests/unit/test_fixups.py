"""node/fixups.py — только чистая логика (needed(), генерация sudoers-снипетов,
build_fixups). apply()/check() реальный sudo/файлы не трогаются в тестах."""

import json
import stat

from sa_home_bot.config import AppConfig, AppsConfig, NodeConfig, Settings, TelegramConfig
from sa_home_bot.node import fixups as fixups_module
from sa_home_bot.node.fixups import (
    INSTALL_SMARTMONTOOLS,
    JOURNALCTL_GROUP,
    NODE_UNIT_SMARTCTL_PATH,
    POWER_CONTROL_POLKIT,
    SMARTCTL_SUDOERS,
    WOL_ENABLE,
    apps_unit_sudoers_content,
    awg_sudoers_content,
    build_fixups,
    make_apps_unit_fixup,
    make_awg_sudoers_fixup,
    make_vpn_probe_forwarding_fixup,
    make_vpn_probe_forwarding_persist_fixup,
    make_vpn_probe_sudoers_fixup,
    make_vpn_probe_tunnel_fixup,
    microsocks_unit_content,
    mtg_unit_content,
    power_polkit_rule_content,
    proxy_firewall_sudoers_content,
    rewrite_unit_path_line,
    smartctl_sudoers_content,
    smartctl_wrapper_content,
    vpn_probe_forward_unit_content,
    vpn_probe_sudoers_content,
    vpn_probe_unit_content,
    wol_unit_content,
)


def _make_executable(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _settings(
    assignments: list[str], apps: list[AppConfig] | None = None, kind: str = "server"
) -> Settings:
    # kind="server" по умолчанию — нейтрально относительно power-control/WoL
    # фиксов (нужны только workstation, см. NodeTraits.power_controllable);
    # так существующие тесты про assignments не задевает несвязанная ось.
    return Settings(
        telegram=TelegramConfig(token="x"),
        subscriptions=[],
        node=NodeConfig(assignments=assignments, kind=kind),
        apps=AppsConfig(items=apps or []),
    )


# --- needed(): какие фиксы актуальны для назначений ноды ---


def test_smartmontools_needed_only_when_monitor_assigned_and_disks_enabled():
    assert INSTALL_SMARTMONTOOLS.needed(_settings(["monitor"]))
    assert not INSTALL_SMARTMONTOOLS.needed(_settings(["apps"]))


def test_smartctl_sudoers_shares_needed_with_install():
    assert SMARTCTL_SUDOERS.needed(_settings(["monitor"])) == INSTALL_SMARTMONTOOLS.needed(
        _settings(["monitor"])
    )


def test_node_unit_smartctl_path_shares_needed_with_install():
    assert NODE_UNIT_SMARTCTL_PATH.needed(_settings(["monitor"])) == INSTALL_SMARTMONTOOLS.needed(
        _settings(["monitor"])
    )


def test_journalctl_needed_when_monitor_assigned():
    assert JOURNALCTL_GROUP.needed(_settings(["monitor"]))
    assert not JOURNALCTL_GROUP.needed(_settings(["apps"]))


def test_apps_unit_fixup_needed_only_when_apps_assigned():
    app = AppConfig(id="qbittorrent", title="qB", unit="qbittorrent-nox.service")
    fixup = make_apps_unit_fixup(app)
    assert fixup.needed(_settings(["apps"], [app]))
    assert not fixup.needed(_settings(["monitor"], [app]))


def test_awg_sudoers_needed_only_when_vpn_assigned():
    fixup = make_awg_sudoers_fixup(_settings(["vpn"]))
    assert fixup.needed(_settings(["vpn"]))
    assert not fixup.needed(_settings(["apps"]))


# --- Генерация содержимого sudoers-снипетов ---


def test_smartctl_sudoers_content_pins_absolute_path_and_wildcard_args():
    content = smartctl_sudoers_content("/usr/sbin/smartctl", "sevboa")
    assert content == "sevboa ALL=(root) NOPASSWD: /usr/sbin/smartctl *\n"


def test_smartctl_wrapper_content_execs_real_binary_via_sudo():
    content = smartctl_wrapper_content("/usr/sbin/smartctl")
    assert content == '#!/bin/sh\nexec sudo -n /usr/sbin/smartctl "$@"\n'


# --- rewrite_unit_path_line(): чинит уже развёрнутые юниты (живой баг на
# mycraft — PATH резолвился до pipx-venv вместо ~/.local/bin, см. setup_wizard).


def test_rewrite_unit_path_line_moves_wrapper_dir_first():
    content = (
        "[Service]\n"
        "Environment=PATH=/home/sevboa/.local/share/pipx/venvs/sa-home-bot/bin"
        ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\n"
        "TimeoutStopSec=150\n"
    )
    new_content = rewrite_unit_path_line(content, "/home/sevboa/.local/bin")
    assert (
        "Environment=PATH=/home/sevboa/.local/bin"
        ":/home/sevboa/.local/share/pipx/venvs/sa-home-bot/bin"
        ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\n" in new_content
    )
    # Остальные строки юнита не тронуты.
    assert "[Service]\n" in new_content
    assert "TimeoutStopSec=150\n" in new_content


def test_rewrite_unit_path_line_returns_none_without_path_line():
    content = "[Service]\nExecStart=/bin/true\n"
    assert rewrite_unit_path_line(content, "/home/sevboa/.local/bin") is None


def test_rewrite_unit_path_line_dedupes_wrapper_dir_already_present():
    content = "Environment=PATH=/usr/local/bin:/home/sevboa/.local/bin:/usr/bin\n"
    new_content = rewrite_unit_path_line(content, "/home/sevboa/.local/bin")
    assert new_content == "Environment=PATH=/home/sevboa/.local/bin:/usr/local/bin:/usr/bin\n"


# --- _which(): фолбэк на sbin-каталоги, которых обычно нет в PATH обычного
# пользователя по SSH (см. deploy/sa-home-node.service — там PATH их содержит,
# а интерактивный логин-шелл — обычно нет). Баг живьём поймали на alfred:
# smartctl/visudo стоят в /usr/sbin, а nodectl fix их не находил.


def test_which_falls_back_to_sbin_dirs_when_not_in_path(tmp_path, monkeypatch):
    monkeypatch.setattr(fixups_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(fixups_module, "_SBIN_FALLBACK_DIRS", (str(tmp_path),))
    _make_executable(tmp_path / "visudo")
    assert fixups_module._which("visudo") == str(tmp_path / "visudo")


def test_which_returns_none_when_nowhere_found(tmp_path, monkeypatch):
    monkeypatch.setattr(fixups_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(fixups_module, "_SBIN_FALLBACK_DIRS", (str(tmp_path),))
    assert fixups_module._which("does-not-exist") is None


def test_smartmontools_check_falls_back_to_sbin_dirs(tmp_path, monkeypatch):
    # Живой баг 2026-08-01 на mycraft: apt честно ставит smartctl в
    # /usr/sbin, а check() голым shutil.which (без фолбэка на sbin, в
    # отличие от _real_smartctl_path) считал его отсутствующим — фикс вечно
    # висел "команда прошла, но проверка отрицательна".
    monkeypatch.setattr(fixups_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(fixups_module, "_SBIN_FALLBACK_DIRS", (str(tmp_path),))
    _make_executable(tmp_path / "smartctl")
    assert INSTALL_SMARTMONTOOLS.check()


def test_install_sudoers_snippet_raises_fixup_error_without_visudo(monkeypatch):
    monkeypatch.setattr(fixups_module, "_which", lambda name: None)
    try:
        fixups_module._install_sudoers_snippet("x", "content")
    except fixups_module.FixupError as exc:
        assert "visudo" in str(exc)
    else:
        raise AssertionError("ожидался FixupError")


def test_real_smartctl_path_falls_back_to_sbin(tmp_path, monkeypatch):
    monkeypatch.setattr(fixups_module.shutil, "which", lambda name, path=None: None)
    monkeypatch.setattr(fixups_module, "_SBIN_FALLBACK_DIRS", (str(tmp_path),))
    _make_executable(tmp_path / "smartctl")
    assert fixups_module._real_smartctl_path() == str(tmp_path / "smartctl")


def test_awg_sudoers_content_pins_absolute_path_and_interface():
    content = awg_sudoers_content("/usr/local/bin/awg", "awg0", "sevboa")
    assert "/usr/local/bin/awg show *" in content
    assert "/usr/local/bin/awg set awg0 peer *" in content
    # Не разрешаем изменение приватного ключа/порта интерфейса — только пиров.
    assert "private-key" not in content
    assert "awg-quick" not in content


def test_mtg_unit_content_embeds_port_and_secret():
    content = mtg_unit_content(443, "sekret")
    assert "simple-run 0.0.0.0:443 sekret" in content
    assert "/usr/local/bin/mtg" in content


def test_microsocks_unit_content_binds_given_host_and_port():
    content = microsocks_unit_content("100.111.4.42", 1080, "/usr/bin/microsocks")
    assert "/usr/bin/microsocks -i 100.111.4.42 -p 1080" in content


def test_proxy_firewall_sudoers_content_only_two_named_counters():
    content = proxy_firewall_sudoers_content("/usr/sbin/nft", "sevboa")
    assert "/usr/sbin/nft -j list counter inet filter mtg_bytes" in content
    assert "/usr/sbin/nft -j list counter inet filter socks_bytes" in content
    # Не весь nft — иначе это было бы равносильно root на firewall.
    assert content.count("NOPASSWD:") == 1


def test_build_fixups_includes_proxy_fixups_when_vpn_assigned():
    ids = {f.id for f in build_fixups(_settings(["vpn"]))}
    assert {"proxy-units", "proxy-firewall"} <= ids


# Живой баг 2026-08-17/найден 2026-08-21: check() раньше считал фикс
# применённым, как только СУЩЕСТВОВАЛИ счётчики-объекты (`nft add counter`),
# не проверяя, ссылается ли на них хоть одно правило — из-за этого
# `_apply_proxy_firewall_live` создавал счётчики и сразу решал, что готово,
# ни разу не выполнив `nft insert rule`. Трафик молча не считался неделю.


def _nft_json(*, counters_only: bool) -> str:
    counter_objects = [
        {"counter": {"family": "inet", "table": "filter", "name": n, "packets": 0, "bytes": 0}}
        for n in ("mtg_bytes", "socks_bytes")
    ]
    if counters_only:
        return json.dumps({"nftables": counter_objects})
    rules = [
        {
            "rule": {
                "family": "inet", "table": "filter", "chain": "input",
                # Ссылка на именованный счётчик в expr — голая строка, не
                # объект (проверено вживую на jeeves, см. _rule_counter_names).
                "expr": [{"counter": n}, {"accept": None}],
            }
        }
        for n in ("mtg_bytes", "socks_bytes")
    ]
    return json.dumps({"nftables": counter_objects + rules})


def test_proxy_firewall_check_false_when_counters_exist_without_rules(monkeypatch):
    monkeypatch.setattr(fixups_module, "_nft_output", lambda argv: _nft_json(counters_only=True))
    assert not fixups_module._proxy_firewall_check()


def test_proxy_firewall_check_true_when_rules_reference_counters(monkeypatch):
    monkeypatch.setattr(fixups_module, "_nft_output", lambda argv: _nft_json(counters_only=False))
    assert fixups_module._proxy_firewall_check()


# Инцидент 2026-08-31 (vpn-jeeves-nftables-conf-broken): _append_nftables_conf_
# counters писал `counter mtg_bytes { }` ВНУТРЬ `chain input` и правило со
# счётчиком ДО строки `type filter hook input ...`. Оба — синтаксическая
# ошибка nft: `nft -f /etc/nftables.conf` падал, nftables.service не стартовал
# на следующем ребуте, awg0 поднимался без NAT (`ip saddr 10.9.0.0/24 ...
# masquerade` из этого же файла), VPN хендшейкался, но не роутил наружу.
# Ни один check() и ни один тест этого не ловили — проверялся только ЖИВОЙ
# ruleset, не валидность персистентного файла.
_WORKING_NFTABLES_CONF = (
    "#!/usr/sbin/nft -f\n"
    "flush ruleset\n"
    "table inet filter {\n"
    "  chain input {\n"
    "    type filter hook input priority 0; policy drop;\n"
    "    ct state established,related accept\n"
    "    iifname \"tailscale0\" accept\n"
    "    tcp dport 443 accept\n"
    "  }\n"
    "  chain forward {\n"
    "    type filter hook forward priority 0; policy drop;\n"
    "    iifname \"awg0\" accept\n"
    "  }\n"
    "}\n"
    "table ip nat {\n"
    "  chain postrouting {\n"
    "    type nat hook postrouting priority 100;\n"
    "    ip saddr 10.9.0.0/24 oifname \"eth0\" masquerade\n"
    "  }\n"
    "}\n"
)


def test_append_nftables_conf_counters_keeps_file_loadable(monkeypatch):
    monkeypatch.setattr(
        fixups_module.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": _WORKING_NFTABLES_CONF})(),
    )
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        fixups_module,
        "_nft_check_file",
        lambda p: seen.__setitem__("checked", fixups_module.Path(p).read_text()),
    )
    monkeypatch.setattr(
        fixups_module,
        "_sudo",
        lambda argv: seen.__setitem__("installed", fixups_module.Path(argv[-2]).read_text()),
    )

    fixups_module._append_nftables_conf_counters(_settings(["vpn"]))

    out = seen["installed"]
    assert seen["checked"] == out  # прогнали nft -c ровно по тому, что ставим
    body = out.splitlines()
    table_i = body.index("table inet filter {")
    chain_i = body.index("  chain input {")
    hook_i = next(i for i, s in enumerate(body) if s.lstrip().startswith("type filter hook input"))
    obj_i = next(i for i, s in enumerate(body) if "counter mtg_bytes { }" in s)
    rule_i = next(i for i, s in enumerate(body) if "counter name mtg_bytes accept" in s)
    # counter-объект — на уровне таблицы (между 'table {' и 'chain input {')
    assert table_i < obj_i < chain_i
    assert "counter socks_bytes { }" in out
    # правило со счётчиком — ПОСЛЕ hook-строки, внутри chain input
    assert hook_i < rule_i
    assert 'iifname "tailscale0" tcp dport 1080 counter name socks_bytes accept' in out


def test_append_nftables_conf_counters_idempotent(monkeypatch):
    already = _WORKING_NFTABLES_CONF.replace(
        "  chain input {\n", "  counter mtg_bytes { }\n  chain input {\n"
    )
    monkeypatch.setattr(
        fixups_module.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": already})(),
    )
    called: list[str] = []
    monkeypatch.setattr(fixups_module, "_nft_check_file", lambda p: called.append("check"))
    monkeypatch.setattr(fixups_module, "_sudo", lambda argv: called.append("sudo"))

    fixups_module._append_nftables_conf_counters(_settings(["vpn"]))

    assert called == []  # уже дописано — ничего не трогаем


def test_apps_unit_sudoers_content_only_start_stop_restart_of_this_unit():
    app = AppConfig(id="jellyfin", title="Jellyfin", unit="jellyfin.service")
    content = apps_unit_sudoers_content(app, "/usr/bin/systemctl", "sevboa")
    assert content == (
        "sevboa ALL=(root) NOPASSWD: "
        "/usr/bin/systemctl start jellyfin.service, "
        "/usr/bin/systemctl stop jellyfin.service, "
        "/usr/bin/systemctl restart jellyfin.service\n"
    )
    # Ни другого юнита, ни произвольных systemctl-команд снипет не разрешает.
    assert "qbittorrent" not in content
    assert " reload " not in content


# --- build_fixups(): фильтрация по needed() ---


def test_build_fixups_empty_for_bare_node():
    assert build_fixups(_settings([])) == []


def test_build_fixups_includes_apps_unit_fixup_per_app():
    apps = [
        AppConfig(id="qbittorrent", title="qB", unit="qbittorrent-nox.service"),
        AppConfig(id="jellyfin", title="Jellyfin", unit="jellyfin.service"),
    ]
    fixups = build_fixups(_settings(["apps"], apps))
    ids = {f.id for f in fixups}
    assert "apps-unit-sudoers-qbittorrent" in ids
    assert "apps-unit-sudoers-jellyfin" in ids
    assert "install-smartmontools" not in ids  # monitor не назначен


def test_build_fixups_includes_awg_sudoers_when_vpn_assigned():
    ids = {f.id for f in build_fixups(_settings(["vpn"]))}
    assert "awg-sudoers" in ids


def test_build_fixups_monitor_and_apps_together():
    app = AppConfig(id="jellyfin", title="Jellyfin", unit="jellyfin.service")
    fixups = build_fixups(_settings(["monitor", "apps"], [app]))
    ids = {f.id for f in fixups}
    assert ids == {
        "install-smartmontools",
        "smartctl-sudoers",
        "node-unit-smartctl-path",
        "journalctl-group",
        "apps-unit-sudoers-jellyfin",
    }


# --- power-control-polkit / wol-enable: только workstation (always_on=False) —
# server/vps недоступность которых уже авария, добровольно уводить в офлайн
# (даже своей же кнопкой в боте) не предлагается вообще, см. node/kind.py.


def test_power_control_and_wol_needed_only_for_workstation():
    for kind in ("server", "vps"):
        settings = _settings([], kind=kind)
        assert not POWER_CONTROL_POLKIT.needed(settings)
        assert not WOL_ENABLE.needed(settings)
    settings = _settings([], kind="workstation")
    assert POWER_CONTROL_POLKIT.needed(settings)
    assert WOL_ENABLE.needed(settings)


def test_build_fixups_includes_power_and_wol_only_for_workstation():
    ids = {f.id for f in build_fixups(_settings([], kind="workstation"))}
    assert {"power-control-polkit", "wol-enable"} <= ids
    for kind in ("server", "vps"):
        ids = {f.id for f in build_fixups(_settings([], kind=kind))}
        assert "power-control-polkit" not in ids
        assert "wol-enable" not in ids


def test_power_polkit_rule_content_lists_login1_actions_for_user():
    content = power_polkit_rule_content("sevboa")
    assert 'subject.user == "sevboa"' in content
    for action in ("power-off", "reboot", "suspend"):
        assert f'"org.freedesktop.login1.{action}"' in content
    assert "polkit.Result.YES" in content


def test_wol_unit_content_runs_ethtool_wol_g_on_given_iface():
    content = wol_unit_content("/usr/sbin/ethtool", "enp1s0")
    assert "ExecStart=/usr/sbin/ethtool -s enp1s0 wol g\n" in content
    assert "Type=oneshot" in content


def test_vpn_probe_needed_only_when_vpn_check_assigned():
    fixup = make_vpn_probe_tunnel_fixup(_settings(["vpn_check"]))
    assert fixup.needed(_settings(["vpn_check"]))
    assert not fixup.needed(_settings(["vpn"]))


def test_vpn_probe_sudoers_shares_needed_with_tunnel():
    settings = _settings(["vpn_check"])
    assert make_vpn_probe_sudoers_fixup(settings).needed(
        settings
    ) == make_vpn_probe_tunnel_fixup(settings).needed(settings)


def test_build_fixups_includes_vpn_probe_fixups_when_vpn_check_assigned():
    ids = {f.id for f in build_fixups(_settings(["vpn_check"]))}
    assert {
        "vpn-check-probe-tunnel",
        "vpn-check-probe-sudoers",
        "vpn-check-probe-forwarding",
        "vpn-check-probe-forwarding-persist",
    } <= ids


def test_build_fixups_excludes_vpn_probe_fixups_without_vpn_check():
    ids = {f.id for f in build_fixups(_settings(["vpn"]))}
    assert "vpn-check-probe-tunnel" not in ids
    assert "vpn-check-probe-sudoers" not in ids
    assert "vpn-check-probe-forwarding" not in ids
    assert "vpn-check-probe-forwarding-persist" not in ids


def test_vpn_probe_tunnel_check_false_when_unit_content_is_stale(monkeypatch):
    """Живой застрявший апгрейд 2026-08-17: юнит существует и active, но с
    ДРУГИМ (старым, netns-версии) содержимым — check() обязан это заметить,
    иначе apply() с новым содержимым не позовётся вовсе."""
    monkeypatch.setattr(fixups_module, "_privileged_exists", lambda path: True)
    monkeypatch.setattr(fixups_module, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(fixups_module, "_read_privileged", lambda path: "stale old content\n")
    called_is_active = []
    monkeypatch.setattr(
        fixups_module.subprocess,
        "run",
        lambda *a, **k: called_is_active.append(True) or type("R", (), {"returncode": 0})(),
    )
    settings = _settings(["vpn_check"])
    assert fixups_module._vpn_probe_tunnel_check(settings) is False
    assert not called_is_active  # содержимое не совпало — до is-active дело не дошло


def test_vpn_probe_tunnel_check_true_when_content_matches_and_active(monkeypatch):
    monkeypatch.setattr(fixups_module, "_privileged_exists", lambda path: True)
    monkeypatch.setattr(fixups_module, "_which", lambda name: f"/usr/bin/{name}")

    def fake_read(path):
        return fixups_module.vpn_probe_unit_content(
            "vpn-probe", fixups_module.VPN_PROBE_IFACE, "/usr/bin/ip", "/usr/bin/awg-quick"
        )

    monkeypatch.setattr(fixups_module, "_read_privileged", fake_read)
    monkeypatch.setattr(
        fixups_module.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})()
    )
    settings = _settings(["vpn_check"])
    assert fixups_module._vpn_probe_tunnel_check(settings) is True


def test_vpn_probe_unit_content_runs_awg_quick_inside_netns():
    content = vpn_probe_unit_content(
        "vpn-probe", "awg-probe0", "/usr/sbin/ip", "/usr/bin/awg-quick"
    )
    assert (
        "ExecStart=/usr/sbin/ip netns exec vpn-probe /usr/bin/awg-quick up awg-probe0\n" in content
    )
    assert (
        "ExecStop=/usr/sbin/ip netns exec vpn-probe /usr/bin/awg-quick down awg-probe0\n"
        in content
    )
    assert "RemainAfterExit=yes" in content


def test_vpn_probe_unit_content_pre_steps_setup_netns_and_veth_idempotently():
    content = vpn_probe_unit_content(
        "vpn-probe", "awg-probe0", "/usr/sbin/ip", "/usr/bin/awg-quick"
    )
    # Ведущий "-" — не валить юнит, если шаг уже применён (повторный `add`
    # уже существующего netns/veth просто вернёт ошибку, которую игнорируем).
    assert "ExecStartPre=-/usr/sbin/ip netns add vpn-probe\n" in content
    assert (
        "ExecStartPre=-/usr/sbin/ip link add vprobe-veth0 type veth peer name vprobe-veth1\n"
        in content
    )
    assert "ExecStartPre=-/usr/sbin/ip link set vprobe-veth1 netns vpn-probe\n" in content
    # "set ... up" идемпотентно само по себе — без ведущего "-".
    assert "ExecStartPre=/usr/sbin/ip link set vprobe-veth0 up\n" in content
    assert (
        "ExecStartPre=-/usr/sbin/ip netns exec vpn-probe /usr/sbin/ip route add default "
        "via 10.200.200.1\n" in content
    )


def test_vpn_probe_sudoers_content_pins_ip_path_and_netns_curl_wildcard():
    content = vpn_probe_sudoers_content("/usr/sbin/ip", "vpn-probe", "sevboa")
    assert content == "sevboa ALL=(root) NOPASSWD: /usr/sbin/ip netns exec vpn-probe curl *\n"


def test_vpn_probe_forwarding_needed_same_as_tunnel():
    settings = _settings(["vpn_check"])
    forwarding = make_vpn_probe_forwarding_fixup(settings)
    tunnel = make_vpn_probe_tunnel_fixup(settings)
    assert forwarding.needed(settings) == tunnel.needed(settings)


def test_find_base_chain_returns_none_without_matching_hook(monkeypatch):
    monkeypatch.setattr(
        fixups_module,
        "_nft_json_ruleset",
        lambda: [
            {
                "chain": {
                    "hook": "input", "type": "filter", "family": "inet",
                    "table": "filter", "name": "input",
                }
            }
        ],
    )
    assert fixups_module._find_base_chain("forward", "filter") is None


def test_find_base_chain_finds_existing_forward_chain(monkeypatch):
    ruleset = [
        {
            "chain": {
                "hook": "input", "type": "filter", "family": "inet",
                "table": "filter", "name": "input",
            }
        },
        {
            "chain": {
                "hook": "forward", "type": "filter", "family": "inet",
                "table": "filter", "name": "forward",
            }
        },
    ]
    monkeypatch.setattr(fixups_module, "_nft_json_ruleset", lambda: ruleset)
    assert fixups_module._find_base_chain("forward", "filter") == ("inet", "filter", "forward")


def test_probe_forwarding_check_looks_for_veth_name_and_subnet(monkeypatch):
    monkeypatch.setattr(fixups_module, "_ip_forward_enabled", lambda: True)
    monkeypatch.setattr(
        fixups_module,
        "_nft_ruleset_text",
        lambda: 'iifname "vprobe-veth0" accept\nip saddr 10.200.200.0/30 masquerade\n',
    )
    assert fixups_module._probe_forwarding_check() is True


def test_probe_forwarding_check_false_when_absent(monkeypatch):
    monkeypatch.setattr(fixups_module, "_ip_forward_enabled", lambda: True)
    monkeypatch.setattr(fixups_module, "_nft_ruleset_text", lambda: "")
    assert fixups_module._probe_forwarding_check() is False


def test_probe_forwarding_check_false_when_ip_forward_disabled(monkeypatch):
    """Живая находка 2026-08-21: на alfred net.ipv4.ip_forward=0 — veth
    работал (ping до хоста проходил), а до реального интернета пакеты не
    доходили вообще, ядро дропало их ДО netfilter независимо от nft-правил.
    check() обязан замечать это, даже если nft-правила уже на месте."""
    monkeypatch.setattr(fixups_module, "_ip_forward_enabled", lambda: False)
    monkeypatch.setattr(
        fixups_module,
        "_nft_ruleset_text",
        lambda: 'iifname "vprobe-veth0" accept\nip saddr 10.200.200.0/30 masquerade\n',
    )
    assert fixups_module._probe_forwarding_check() is False


def test_ensure_ip_forward_noop_when_already_enabled(monkeypatch):
    monkeypatch.setattr(fixups_module, "_ip_forward_enabled", lambda: True)
    sudo_calls = []
    monkeypatch.setattr(fixups_module, "_sudo", lambda argv: sudo_calls.append(argv))
    fixups_module._ensure_ip_forward()
    assert sudo_calls == []


def test_ensure_ip_forward_writes_sysctl_and_applies_live(monkeypatch):
    monkeypatch.setattr(fixups_module, "_ip_forward_enabled", lambda: False)
    monkeypatch.setattr(fixups_module, "_privileged_exists", lambda path: False)
    sudo_calls = []
    monkeypatch.setattr(fixups_module, "_sudo", lambda argv: sudo_calls.append(argv))
    fixups_module._ensure_ip_forward()
    assert any(call[:2] == ["install", "-m"] for call in sudo_calls)
    assert ["sysctl", "-w", "net.ipv4.ip_forward=1"] in sudo_calls


def test_probe_forwarding_apply_installs_nft_when_missing(monkeypatch):
    """Живая находка 2026-08-21: на alfred nftables не был установлен вообще
    (в отличие от jeeves) — без него ЛЮБОЙ ``nft`` тихо проваливается,
    forward/NAT для veth-подсети не появляется, и симптом неотличим от
    таймаута соединения."""
    monkeypatch.setattr(fixups_module, "_ensure_ip_forward", lambda: None)
    monkeypatch.setattr(fixups_module, "_probe_forwarding_check", lambda: False)
    monkeypatch.setattr(fixups_module, "_which", lambda name: None)
    sudo_calls = []
    monkeypatch.setattr(
        fixups_module, "install_argv", lambda pkg: ["apt-get", "install", "-y", pkg]
    )
    monkeypatch.setattr(fixups_module, "_sudo", lambda argv: sudo_calls.append(argv))
    monkeypatch.setattr(fixups_module, "_find_base_chain", lambda hook, kind: None)
    fixups_module._probe_forwarding_apply()
    assert sudo_calls[0] == ["apt-get", "install", "-y", "nftables"]
    assert any(call[:3] == ["nft", "add", "table"] for call in sudo_calls[1:])


def test_probe_forwarding_apply_skips_install_when_nft_present(monkeypatch):
    monkeypatch.setattr(fixups_module, "_ensure_ip_forward", lambda: None)
    monkeypatch.setattr(fixups_module, "_probe_forwarding_check", lambda: False)
    monkeypatch.setattr(fixups_module, "_which", lambda name: f"/usr/sbin/{name}")
    sudo_calls = []
    monkeypatch.setattr(fixups_module, "_sudo", lambda argv: sudo_calls.append(argv))
    monkeypatch.setattr(fixups_module, "_find_base_chain", lambda hook, kind: None)
    fixups_module._probe_forwarding_apply()
    assert all("apt-get" not in call for call in sudo_calls)


def test_vpn_probe_forwarding_persist_needed_same_as_tunnel():
    settings = _settings(["vpn_check"])
    persist = make_vpn_probe_forwarding_persist_fixup(settings)
    tunnel = make_vpn_probe_tunnel_fixup(settings)
    assert persist.needed(settings) == tunnel.needed(settings)


def test_forward_script_content_creates_own_table_when_missing(monkeypatch):
    """На alfred форвардинг-фикс заводит СВОЮ таблицу ``sa_vpn_probe``, если
    подходящей чужой не нашлось — эта таблица тоже не переживает ребут,
    поэтому скрипт обязан уметь досоздать её саму, не только правила."""
    forward_target = ("inet", fixups_module.VPN_PROBE_NFT_TABLE, "forward")
    nat_target = ("ip", fixups_module.VPN_PROBE_NFT_TABLE, "postrouting")
    content = fixups_module._vpn_probe_forward_script_content(forward_target, nat_target)
    assert content.startswith("#!/bin/sh\n")
    assert "set -e" in content
    assert f"nft list table inet {fixups_module.VPN_PROBE_NFT_TABLE}" in content
    assert "type filter hook forward" in content
    assert f"nft list table ip {fixups_module.VPN_PROBE_NFT_TABLE}" in content
    assert "type nat hook postrouting" in content
    assert 'iifname "vprobe-veth0" accept' in content
    assert 'oifname "vprobe-veth0" accept' in content
    assert "ip saddr 10.200.200.0/30 masquerade" in content


def test_forward_script_content_skips_table_creation_for_foreign_chain():
    """На jeeves форвардинг-фикс переиспользует уже существующие цепочки
    firewall (не наши) — скрипт не должен пытаться их пересоздавать, только
    idempotent-добавлять в них правила."""
    forward_target = ("inet", "filter", "forward")
    nat_target = ("ip", "nat", "postrouting")
    content = fixups_module._vpn_probe_forward_script_content(forward_target, nat_target)
    assert "nft list table" not in content
    assert "nft add table" not in content
    assert "nft list chain inet filter forward" in content
    assert "nft list chain ip nat postrouting" in content


def test_forward_unit_content_runs_after_tunnel_and_executes_script():
    content = vpn_probe_forward_unit_content(fixups_module.VPN_PROBE_FORWARD_SCRIPT_PATH)
    assert fixups_module.VPN_PROBE_UNIT_FILE.name in content
    assert f"ExecStart=/bin/sh {fixups_module.VPN_PROBE_FORWARD_SCRIPT_PATH}\n" in content
    assert "Type=oneshot" in content
    assert "RemainAfterExit=yes" in content
    assert "WantedBy=multi-user.target" in content


def test_forwarding_persist_check_false_when_files_missing(monkeypatch):
    monkeypatch.setattr(fixups_module, "_privileged_exists", lambda path: False)
    assert fixups_module._vpn_probe_forwarding_persist_check() is False


def test_forwarding_persist_check_false_when_unit_content_stale(monkeypatch):
    monkeypatch.setattr(fixups_module, "_privileged_exists", lambda path: True)
    monkeypatch.setattr(fixups_module, "_read_privileged", lambda path: "old content\n")
    assert fixups_module._vpn_probe_forwarding_persist_check() is False


def test_forwarding_persist_check_true_when_matching_and_active(monkeypatch):
    expected = vpn_probe_forward_unit_content(fixups_module.VPN_PROBE_FORWARD_SCRIPT_PATH)
    monkeypatch.setattr(fixups_module, "_privileged_exists", lambda path: True)
    monkeypatch.setattr(fixups_module, "_read_privileged", lambda path: expected)
    monkeypatch.setattr(
        fixups_module.subprocess,
        "run",
        lambda argv, **kwargs: type("R", (), {"returncode": 0})(),
    )
    assert fixups_module._vpn_probe_forwarding_persist_check() is True


def test_forwarding_persist_apply_reuses_resolved_targets_and_enables_unit(monkeypatch, tmp_path):
    forward_target = ("inet", "filter", "forward")
    nat_target = ("ip", "nat", "postrouting")
    monkeypatch.setattr(fixups_module, "_ensure_ip_forward", lambda: None)
    monkeypatch.setattr(
        fixups_module, "_resolve_probe_forward_targets", lambda: (forward_target, nat_target)
    )
    monkeypatch.setattr(fixups_module, "_privileged_exists", lambda path: False)
    sudo_calls = []
    monkeypatch.setattr(fixups_module, "_sudo", lambda argv: sudo_calls.append(argv))
    fixups_module._vpn_probe_forwarding_persist_apply()
    assert any(call[:2] == ["install", "-D"] for call in sudo_calls)
    assert ["systemctl", "daemon-reload"] in sudo_calls
    assert ["systemctl", "enable", "--now", fixups_module.VPN_PROBE_FORWARD_UNIT_FILE.name] in (
        sudo_calls
    )


def test_prepare_probe_conf_strips_dns_line():
    raw = (
        "[Interface]\n"
        "PrivateKey = SECRET\n"
        "Address = 10.9.0.15/32\n"
        "DNS = 1.1.1.1\n"
        "\n"
        "[Peer]\n"
        "PublicKey = PUB\n"
    )
    prepared = fixups_module._prepare_probe_conf(raw)
    assert "DNS" not in prepared  # иначе awg-quick зовёт отсутствующий resolvconf
    assert "PrivateKey = SECRET" in prepared
    assert "[Peer]" in prepared


def test_prepare_probe_conf_strips_stale_table_line():
    """Живая находка 2026-08-21: на уже развёрнутых нодах строка ``Table =
    off`` (заведённая версиями v0.92.2-v0.92.4, до возврата к netns)
    переживала апгрейд, потому что apply() читает и переписывает УЖЕ
    установленный файл — awg-quick продолжал не заводить маршрут через сам
    туннель, и curl внутри netns уходил бы мимо VPN обычным путём."""
    raw = (
        "[Interface]\n"
        "PrivateKey = SECRET\n"
        "Table = off\n"
        "Address = 10.9.0.15/32\n"
        "\n"
        "[Peer]\n"
        "PublicKey = PUB\n"
    )
    prepared = fixups_module._prepare_probe_conf(raw)
    assert "Table" not in prepared
    assert "PrivateKey = SECRET" in prepared


def test_run_fixups_survives_check_raising_and_still_applies_it():
    """Живой краш 2026-08-17: check() кинул FixupError (sudo без TTY/кэша)
    и уронил весь run_fixups, оставив ВСЕ фиксы после него непроверенными —
    не только тот, что упал."""
    applied = []
    calls = {"n": 0}

    def flaky_check() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise fixups_module.FixupError("sudo без TTY")
        return True  # после apply() проверка снова доступна и подтверждает успех

    flaky = fixups_module.Fixup(
        id="flaky",
        title="Проверка иногда падает",
        needed=lambda settings: True,
        check=flaky_check,
        apply=lambda: applied.append("flaky"),
    )
    after = fixups_module.Fixup(
        id="after",
        title="Идёт следующим",
        needed=lambda settings: True,
        check=lambda: True,
        apply=lambda: applied.append("after"),
    )
    failed = fixups_module.run_fixups([flaky, after])
    assert failed == []
    assert applied == ["flaky"]  # apply() всё же вызван, несмотря на упавшую первую проверку


def test_run_fixups_survives_check_raising_after_apply_too():
    """check() падает и до, и после apply() — фикс уходит в failed, но
    следующий за ним фикс в списке всё равно проверяется (не крашится весь run)."""
    after_checked = []

    def always_raises() -> bool:
        raise fixups_module.FixupError("недоступно")

    flaky = fixups_module.Fixup(
        id="flaky",
        title="Всегда падает",
        needed=lambda settings: True,
        check=always_raises,
        apply=lambda: None,
    )
    after = fixups_module.Fixup(
        id="after",
        title="Идёт следующим",
        needed=lambda settings: True,
        check=lambda: after_checked.append(True) or True,
        apply=lambda: None,
    )
    failed = fixups_module.run_fixups([flaky, after])
    assert failed == ["flaky"]
    assert after_checked  # run не упал целиком — до "after" дошли
