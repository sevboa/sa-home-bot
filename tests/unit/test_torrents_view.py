"""Панель /torrents: список закачек (кружки статусов, обрезка имён, пагинация,
пресеты лимита скорости) и карточка раздачи (bot/torrents_view.py)."""

import pytest

from sa_home_bot.bot.torrents_view import (
    _LIST_LINE_WIDTH_BUDGET,
    TORRENTS_PAGE_SIZE,
    _text_width,
    build_app_view,
    build_card_view,
    build_list_view,
)


def _torrent(**overrides):
    base = {
        "name": "Foo.S01",
        "state": "downloading",
        "progress_pct": 42,
        "dlspeed_bytes_s": 1048576,
        "upspeed_bytes_s": 0,
        "paused": False,
        "eta_s": 3600,
        "seeds": 5,
        "peers": 2,
        "hash": "abc123",
    }
    base.update(overrides)
    return base


# --- Список -----------------------------------------------------------------


def test_list_view_renders_lamp_progress_and_speed():
    result = {"torrents": [_torrent()], "speed_limit_mbps": 0}
    text, kb = build_list_view(result, 0)
    assert "🧲 <b>Закачки</b> (1)" in text
    # downloading — активная скачка, лампа жёлтая, скорость показана.
    # Прогресс в строке списка — луной (33-66% → 🌓), не числом; точный
    # процент остаётся на карточке (см. test_card_view_shows_moon_and_percent).
    assert "🟡" in text and "🌓" in text and "1.0 МБ/с" in text
    assert "Лимит скорости: без ограничения" in text


# --- Кружки статусов ---------------------------------------------------------


def test_lamp_downloading_is_yellow():
    text, _ = build_list_view({"torrents": [_torrent(state="downloading")]}, 0)
    assert "🟡" in text


def test_lamp_metadata_is_orange_not_yellow():
    """Получение метаданных magnet-ссылки — не передача данных, ожидание."""
    text, _ = build_list_view({"torrents": [_torrent(state="metaDL")]}, 0)
    assert "🟠" in text and "🟡" not in text


def test_lamp_seeding_is_green():
    text, _ = build_list_view({"torrents": [_torrent(state="uploading")]}, 0)
    assert "🟢" in text


def test_lamp_waiting_is_orange():
    text, _ = build_list_view({"torrents": [_torrent(state="stalledDL")]}, 0)
    assert "🟠" in text


def test_lamp_error_is_red():
    text, _ = build_list_view({"torrents": [_torrent(state="error")]}, 0)
    assert "🔴" in text


def test_lamp_stopped_incomplete_is_white():
    text, _ = build_list_view(
        {"torrents": [_torrent(state="pausedDL", progress_pct=50)]}, 0
    )
    assert "⚪" in text


def test_lamp_stopped_complete_is_brown():
    text, _ = build_list_view(
        {"torrents": [_torrent(state="pausedUP", progress_pct=100)]}, 0
    )
    assert "🟤" in text


def test_speed_hidden_when_not_actively_transferring():
    text, _ = build_list_view({"torrents": [_torrent(state="queuedDL")]}, 0)
    assert "↓" not in text and "↑" not in text


def test_downloading_shows_download_speed_not_upload():
    torrent = _torrent(
        state="downloading", dlspeed_bytes_s=2 * 1024**2, upspeed_bytes_s=999 * 1024**2
    )
    text, _ = build_list_view({"torrents": [torrent]}, 0)
    assert "↓2.0 МБ/с" in text
    assert "↑" not in text


def test_seeding_shows_upload_speed_not_download():
    torrent = _torrent(
        state="uploading", dlspeed_bytes_s=999 * 1024**2, upspeed_bytes_s=3 * 1024**2
    )
    text, _ = build_list_view({"torrents": [torrent]}, 0)
    assert "↑3.0 МБ/с" in text
    assert "↓" not in text


def test_card_speed_hidden_when_stopped():
    text, _ = build_card_view(_torrent(state="pausedDL", progress_pct=50))
    assert "Скорость" not in text


def test_card_speed_shown_when_seeding():
    text, _ = build_card_view(_torrent(state="uploading", upspeed_bytes_s=3 * 1024**2))
    assert "Скорость: ↑3.0 МБ/с" in text


def test_card_speed_shown_when_downloading():
    text, _ = build_card_view(_torrent(state="downloading", dlspeed_bytes_s=2 * 1024**2))
    assert "Скорость: ↓2.0 МБ/с" in text


def test_list_line_never_exceeds_reference_width_budget():
    """Пример пользователя: «🔵 Marvels.Daredevil.S01.1080p.L.N.B.J — 100% ·
    ↓0» задаёт бюджет ширины строки — длинное имя должно ужиматься под него
    вместе с суффиксом (прогресс + скорость), а не просто под число символов."""
    long_name = "Marvels.Daredevil.Born.Again.S01.2160p.WEB-DL.DDP5.1.Atmos.HEVC"
    result = {"torrents": [_torrent(name=long_name, state="downloading", dlspeed_bytes_s=0)]}
    text, _ = build_list_view(result, 0)
    line = next(line for line in text.splitlines() if "🟡" in line)
    assert _text_width(line) <= _LIST_LINE_WIDTH_BUDGET + 0.01


def test_list_view_has_qbittorrent_app_button():
    result = {"torrents": [], "speed_limit_mbps": 0}
    _, kb = build_list_view(result, 0)
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "st:t_app" in codes


def test_list_view_button_opens_card_by_hash():
    result = {"torrents": [_torrent(hash="deadbeef")], "speed_limit_mbps": 0}
    _, kb = build_list_view(result, 0)
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "st:t_card:deadbeef" in codes


def test_list_view_long_name_is_truncated():
    long_name = "A" * 80
    result = {"torrents": [_torrent(name=long_name)], "speed_limit_mbps": 0}
    text, _ = build_list_view(result, 0)
    assert long_name not in text
    assert "…" in text


def test_list_view_empty_says_so():
    text, _ = build_list_view({"torrents": [], "speed_limit_mbps": 0}, 0)
    assert "Пока ничего не качается" in text


def test_list_view_paginates():
    torrents = [_torrent(name=f"T{i}", hash=f"h{i}") for i in range(TORRENTS_PAGE_SIZE + 2)]
    result = {"torrents": torrents, "speed_limit_mbps": 0}
    text, kb = build_list_view(result, 0)
    assert "T0" in text and f"T{TORRENTS_PAGE_SIZE}" not in text
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "st:t_list:8" in codes  # ➡️


def test_list_view_marks_active_speed_preset():
    result = {"torrents": [], "speed_limit_mbps": 2}
    text, kb = build_list_view(result, 0)
    assert "Лимит скорости: 2 МБ/с" in text
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any(label.startswith("✅") and "2 МБ/с" in label for label in labels)


def test_list_view_speed_buttons_carry_offset():
    torrents = [_torrent(name=f"T{i}", hash=f"h{i}") for i in range(TORRENTS_PAGE_SIZE + 2)]
    result = {"torrents": torrents, "speed_limit_mbps": 0}
    _, kb = build_list_view(result, TORRENTS_PAGE_SIZE)
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"st:t_speed:5:{TORRENTS_PAGE_SIZE}" in codes


# --- Карточка -----------------------------------------------------------------


def test_card_view_shows_seeds_and_peers():
    text, _ = build_card_view(_torrent(seeds=9, peers=4))
    assert "9 сидов, 4 личей" in text


def test_card_view_shows_moon_and_exact_percent():
    text, _ = build_card_view(_torrent(progress_pct=42))
    assert "🌓 42%" in text


# --- Фазы луны (прогресс в строке списка) ------------------------------------


@pytest.mark.parametrize(
    ("progress_pct", "phase"),
    [(0, "🌑"), (1, "🌒"), (32, "🌒"), (33, "🌓"), (50, "🌓"), (66, "🌓"),
     (67, "🌔"), (99, "🌔"), (100, "🌕")],
)
def test_moon_phase_buckets_match_progress(progress_pct, phase):
    text, _ = build_list_view({"torrents": [_torrent(progress_pct=progress_pct)]}, 0)
    assert phase in text


def test_card_view_running_offers_stop_button():
    _, kb = build_card_view(_torrent(paused=False, hash="h1"))
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert any("Остановить" in t for t in texts)


def test_card_view_paused_offers_start_button():
    _, kb = build_card_view(_torrent(paused=True, hash="h1"))
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert any("Запустить" in t for t in texts)


def test_card_view_toggle_callback_carries_hash():
    _, kb = build_card_view(_torrent(hash="deadbeef"))
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "st:t_toggle:deadbeef" in codes


def test_card_view_back_goes_to_list_first_page():
    _, kb = build_card_view(_torrent())
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "st:t_list:0" in codes


def test_card_view_unknown_eta_says_so():
    text, _ = build_card_view(_torrent(eta_s=None))
    assert "Осталось: неизвестно" in text


# --- Карточка apps/qbittorrent внутри панели ---------------------------------


def test_app_view_keeps_original_text():
    text, _ = build_app_view("🧲 qBittorrent — ✅ работает")
    assert text == "🧲 qBittorrent — ✅ работает"


def test_app_view_has_only_refresh_and_back_no_manage_buttons():
    _, kb = build_app_view("любой текст")
    texts = [b.text for row in kb.inline_keyboard for b in row]
    codes = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert texts == ["🔄 Обновить", "⬅️ Назад"]
    assert codes == ["st:t_app", "st:t_list:0"]
