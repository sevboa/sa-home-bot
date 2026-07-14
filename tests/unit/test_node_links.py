"""node_links: генерация и разбор ссылок-команд /node_* и /svc_*."""

from sa_home_bot.bot.node_links import (
    match_service,
    node_command,
    normalize,
    resolve_node,
    resolve_svc_candidates,
    svc_command,
)

KNOWN = ["alfred", "arch-t480", "winpc"]


# --- Нормализация и генерация ---


def test_normalize_replaces_dashes_and_lowercases():
    assert normalize("arch-t480") == "arch_t480"
    assert normalize("Telegram-Bot") == "telegram_bot"


def test_normalize_rejects_unrepresentable():
    assert normalize("нода") is None  # кириллица не влезает в команду Telegram
    assert normalize("node with space") is None
    assert normalize("") is None


def test_node_command():
    assert node_command("alfred") == "/node_alfred"
    assert node_command("arch-t480") == "/node_arch_t480"
    assert node_command("нода") is None


def test_svc_command():
    assert svc_command("alfred", "telegram-bot") == "/svc_alfred_telegram_bot"
    assert svc_command("нода", "monitor") is None


def test_command_length_limit():
    long_id = "x" * 40
    assert node_command(long_id) is None  # >32 символов — команду не собрать


# --- Разбор ---


def test_resolve_node_exact_normalized_match():
    assert resolve_node("arch_t480", KNOWN) == "arch-t480"
    assert resolve_node("alfred", KNOWN) == "alfred"
    assert resolve_node("toaster", KNOWN) is None


def test_resolve_svc_candidates_longest_prefix_first():
    # /svc_arch_t480_telegram_bot: длиннейший префикс-нода — arch-t480,
    # хвост — telegram_bot. Нода "arch" (будь она в рое) шла бы вторым
    # кандидатом с хвостом t480_telegram_bot.
    known = [*KNOWN, "arch"]
    candidates = resolve_svc_candidates("arch_t480_telegram_bot", known)
    assert candidates[0] == ("arch-t480", "telegram_bot")
    assert ("arch", "t480_telegram_bot") in candidates


def test_resolve_svc_candidates_no_match():
    assert resolve_svc_candidates("toaster_monitor", KNOWN) == []


def test_match_service_normalized():
    assert match_service("telegram_bot", ["monitor", "telegram-bot"]) == "telegram-bot"
    assert match_service("monitor", ["monitor"]) == "monitor"
    assert match_service("nope", ["monitor"]) is None
