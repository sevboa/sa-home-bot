from __future__ import annotations

from sa_home_bot.bot.vpn_check_view import series_problem, status_text

STATE = {
    "node": "alfred",
    "server": "jeeves",
    "transport": "awg",
    "target": "https://1.1.1.1",
    "status": "ok",
    "last_latency_ms": 40,
}


def _row(transport: str, status: str, verdict: str) -> dict:
    return {
        "node": "zakhar",
        "server": "jeeves",
        "transport": transport,
        "status": status,
        "last_verdict": verdict,
    }


def test_without_series_output_is_unchanged():
    assert status_text([STATE]) == status_text([STATE], series=[])
    assert "Из РФ" not in status_text([STATE])


def test_series_block_lists_each_pair():
    text = status_text(
        [STATE],
        series=[
            _row("awg", "ok", "ok"),
            _row("reality", "alerting", "tcp_blocked"),
            _row("xhttp", "ok", "cut"),
        ],
    )
    block = text.split("🇷🇺 <b>Из РФ</b> (серийная проверка)\n", 1)[1].splitlines()
    assert block[0].startswith("  🟢 <code>zakhar</code> → <b>jeeves</b>")
    assert block[1].endswith("— не работает: TCP до сервера не проходит")
    assert block[2].endswith("— последний замер: обрыв")


def test_series_problem_bytes_only_for_freeze_and_cut():
    assert series_problem("cut", 20480) == "обрыв после 20 КБ"
    assert series_problem("tcp_blocked", 20480) == "TCP до сервера не проходит"
    assert series_problem("freeze", None) == "заморозка"
